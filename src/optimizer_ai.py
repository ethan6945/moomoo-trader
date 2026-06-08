"""Autonomous optimizer (DeepSeek v4-flash) — PROPOSES, never decides.

Given the weekly real-fill self-review, it asks DeepSeek for parameter-change
proposals (entry threshold / TP / SL) and writes them to the APPROVAL QUEUE.
The owner approves in the GUI/CLI; only then does a change take effect (runtime
override, no restart). This is the feedback-铁律 implementation of requirement
#8 — the system can optimize itself, but every change passes through the owner.

DeepSeek's API is OpenAI-compatible; we call it over plain HTTPS (requests) so
no extra SDK dependency is needed. With DEEPSEEK_API_KEY blank, this is a no-op
(the rules-based suggestions in self_review still run).
"""
from __future__ import annotations

import json
import logging

from . import approvals, runtime_config
from .config import settings

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a quantitative trading risk optimizer for a $5k CASH day-trading "
    "bot (no leverage, max 5 positions, US semis). You tune ONLY these params: "
    "entry_threshold (55-85), tp_atr_mult (2-12), sl_atr_mult (2-6). The owner "
    "wants higher $/day toward a $50/day target WITHOUT reckless risk, and "
    "dislikes over-conservative gatekeeping. Propose 0-3 SMALL, justified "
    "changes based on the real-fill review. Reply ONLY with a JSON array of "
    '{"key","value","rationale"} objects — rationale ONE short sentence, no prose '
    "outside the array. Empty array [] if no change is warranted."
)


def _current_params() -> dict:
    return {
        "entry_threshold": runtime_config.entry_threshold(),
        "tp_atr_mult": runtime_config.tp_atr_mult(),
        "sl_atr_mult": runtime_config.sl_atr_mult(),
    }


def _call_deepseek(review: dict) -> list[dict]:
    """Call DeepSeek chat completions. Returns parsed proposals or []."""
    if not settings.deepseek_key:
        return []
    import requests
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content":
                "Current params: " + json.dumps(_current_params())
                + "\nReal-fill review (last week): " + json.dumps({
                    k: review.get(k) for k in
                    ("n_trades", "per_day", "win_rate", "avg_r_multiple",
                     "by_strategy", "by_exit", "target_note")
                })},
        ],
        "temperature": 0.2,
        "stream": False,
        # deepseek-v4-flash is a REASONING model: completion tokens cover
        # reasoning_content FIRST, then the answer. Too few tokens and the JSON
        # gets truncated mid-string. 4000 leaves ample room for both.
        "max_tokens": 4000,
    }
    try:
        r = requests.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=60,
        )
        r.raise_for_status()
        content = (r.json()["choices"][0]["message"].get("content") or "").strip()
        # Robust extraction: pull the first [...] JSON array out of the content
        # (tolerates code fences or stray prose around it).
        start, end = content.find("["), content.rfind("]")
        if start == -1 or end == -1 or end <= start:
            log.warning("DeepSeek optimizer: no JSON array in response")
            return []
        proposals = json.loads(content[start:end + 1])
        return proposals if isinstance(proposals, list) else []
    except Exception as e:
        log.warning("DeepSeek optimizer call failed: %s", e)
        return []


# Map a tunable param key → the BacktestConfig field it overrides.
_PARAM_TO_CFG = {
    "entry_threshold": "threshold",
    "tp_atr_mult": "tp_atr_mult",
    "sl_atr_mult": "sl_atr_mult",
    "risk_per_trade": "risk_per_trade",
}
_VALIDATE_WINDOWS = (180, 360)
_PINNED = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]


def _base_cfg(days: int):
    """Live-aligned honest-engine config (mirrors .env). _run_live_engine adds
    VIX sizing + earnings gate + real commissions + momentum on top.

    account_usd is the runtime-effective budget (budget_usd honours the db-state
    'budget_usd' override), NOT the frozen .env value — so the backtest's slot cap
    derive_max_positions(cfg.account_usd) and cash-sizing base track the SAME
    capital the live engine runs at. Using settings.account_usd here would re-break
    the live↔backtest parity that req#1 dynamic capital exists to provide."""
    from src.backtest import BacktestConfig
    from . import risk_manager   # local import avoids any import-time cycle
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=list(_PINNED), account_usd=risk_manager.budget_usd(),
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct, apply_ml_gate=False, apply_mr_strategy=False,
        use_scale_out=settings.use_scale_out, tp1_r=settings.tp1_r, tp2_r=settings.tp2_r)


# A proposal may not worsen MTM drawdown by more than this (percentage points)
# on either window — stops the $/day gate from rubber-stamping reckless sizing.
DD_TOLERANCE_PP = 3.0


def _metrics(cfg, cache, days: int) -> tuple[float, float]:
    """(net $/day, max MTM drawdown %) on the honest engine. rich_metrics=False:
    both fields live on the lean return path (backtest_v3.py net_pnl_usd +
    max_dd_mtm_pct), so we skip the per-proposal Sharpe/Sortino/MonteCarlo compute
    that nothing here consumes."""
    from src.backtest import _run_live_engine
    m = _run_live_engine(cfg, cache, rich_metrics=False)["metrics"]
    return m.get("net_pnl_usd", 0.0) / days, m.get("max_dd_mtm_pct", 0.0)


def _prep_cache():
    """Build base cfgs + prefetch cache for both windows (degenerate-guarded).
    Returns (base, cache) or None on a degenerate/failed prefetch."""
    from src.backtest import prefetch_data
    base = {d: _base_cfg(d) for d in _VALIDATE_WINDOWS}
    cache = {d: prefetch_data(base[d]) for d in _VALIDATE_WINDOWS}
    # Guard against a degenerate prefetch (OpenD hiccup → too few tickers), which
    # would make every Δ ≈ 0 and the validation meaningless.
    need = max(6, len(_PINNED) - 2)
    for d in _VALIDATE_WINDOWS:
        got = len(cache[d].get("per_ticker", {}))
        if got < need:
            log.warning("optimizer: %dd prefetch degenerate (%d/%d tickers) — skipping",
                        d, got, len(_PINNED))
            return None
    return base, cache


def _prep_base():
    """_prep_cache + base (per_day, dd) at the .env baseline — for the yield gate."""
    pc = _prep_cache()
    if pc is None:
        return None
    base, cache = pc
    base_pd, base_dd = {}, {}
    for d in _VALIDATE_WINDOWS:
        base_pd[d], base_dd[d] = _metrics(base[d], cache[d], d)
    return base, cache, base_pd, base_dd


def validate_proposals(proposals: list[dict]) -> list[dict]:
    """YIELD-seeking gate (for DeepSeek's threshold/tp/sl ideas): keep only
    proposals that BEAT baseline $/day on BOTH windows AND don't worsen drawdown
    by more than DD_TOLERANCE_PP — so chasing $/day can't rubber-stamp reckless
    settings. Annotated with deltas + dd. Empty if the backtest can't run."""
    from dataclasses import replace
    try:
        prep = _prep_base()
    except Exception as e:
        log.warning("optimizer: backtest setup failed (%s) — no proposals enqueued", e)
        return []
    if prep is None:
        return []
    base, cache, base_pd, base_dd = prep

    validated = []
    for p in proposals:
        key, value = p.get("key"), p.get("value")
        field = _PARAM_TO_CFG.get(key)
        if not field or not runtime_config.is_valid(key, value):
            continue
        try:
            pd_d, dd_d = {}, {}
            for d in _VALIDATE_WINDOWS:
                cfg = replace(base[d], **{field: float(value)})
                pd_d[d], dd_d[d] = _metrics(cfg, cache[d], d)
        except Exception as e:
            log.warning("optimizer: backtest of %s=%s failed: %s", key, value, e)
            continue
        deltas = {d: pd_d[d] - base_pd[d] for d in _VALIDATE_WINDOWS}
        dd_ok = all(dd_d[d] <= base_dd[d] + DD_TOLERANCE_PP for d in _VALIDATE_WINDOWS)
        beats = all(deltas[d] > 0 for d in _VALIDATE_WINDOWS) and dd_ok
        log.info("optimizer: %s=%s → Δ180d %+.2f(DD%+.1f) Δ360d %+.2f(DD%+.1f) → %s",
                 key, value, deltas[180], dd_d[180] - base_dd[180],
                 deltas[360], dd_d[360] - base_dd[360], "PASS" if beats else "drop")
        if beats:
            validated.append(dict(p, _deltas=deltas, _dd=dict(dd_d)))
    return validated


def backtest_risk_change(value: float) -> dict | None:
    """RISK-adjusted check for a risk_per_trade change (half-Kelly). Unlike
    validate_proposals (which needs $/day to rise), this ALLOWS a protective
    down-size whose $/day dips so long as it cuts drawdown more — i.e. Calmar-like
    per_day/DD must not get worse on either window. The baseline is the
    risk_per_trade ACTUALLY in force (runtime override beats .env), so the
    comparison reflects changing from the current live state. Returns per-window
    metrics (current vs proposed) + a risk_adjusted_ok verdict, or None if it
    can't run."""
    from dataclasses import replace
    from . import runtime_config
    try:
        pc = _prep_cache()
    except Exception as e:
        log.warning("optimizer: risk backtest setup failed (%s)", e)
        return None
    if pc is None:
        return None
    base, cache = pc
    cur = runtime_config.risk_per_trade()
    pd_b, dd_b, pd_p, dd_p = {}, {}, {}, {}
    try:
        for d in _VALIDATE_WINDOWS:
            pd_b[d], dd_b[d] = _metrics(replace(base[d], risk_per_trade=cur), cache[d], d)
            pd_p[d], dd_p[d] = _metrics(replace(base[d], risk_per_trade=float(value)), cache[d], d)
    except Exception as e:
        log.warning("optimizer: risk backtest of %s failed: %s", value, e)
        return None

    def calmar(pd_: float, dd_: float) -> float:
        return pd_ / max(dd_, 1.0)

    # Calmar must not get worse AND absolute DD may not worsen by more than the
    # same tolerance validate_proposals uses — the ratio alone would let a
    # proportional up-size (or a higher-$/day-but-worse-DD change) slip through.
    risk_adjusted_ok = all(
        calmar(pd_p[d], dd_p[d]) >= calmar(pd_b[d], dd_b[d]) - 1e-9
        and dd_p[d] <= dd_b[d] + DD_TOLERANCE_PP
        for d in _VALIDATE_WINDOWS)
    return {"per_day": pd_p, "dd": dd_p, "base_per_day": pd_b, "base_dd": dd_b,
            "risk_adjusted_ok": risk_adjusted_ok}


def propose_from_review(review: dict) -> int:
    """Weekly: DeepSeek proposes param tweaks → each is BACKTESTED on the honest
    engine → only proposals that beat the current config on BOTH 180d & 360d are
    enqueued for your approval (annotated with the measured $/day gain). Plausible-
    but-unvalidated LLM ideas are dropped. No-op (0) without a DeepSeek key.
    """
    proposals = _call_deepseek(review)
    if not proposals:
        return 0
    validated = validate_proposals(proposals)
    n = 0
    for p in validated:
        key, value = p["key"], p["value"]
        cur = _current_params().get(key)
        d = p.get("_deltas", {})
        dd = p.get("_dd", {})
        gain = (f"backtested +${d.get(180, 0):.1f}/day(180d,DD{dd.get(180, 0):.1f}%), "
                f"+${d.get(360, 0):.1f}/day(360d,DD{dd.get(360, 0):.1f}%)")
        approvals.enqueue(
            kind="param_change",
            detail=f"DeepSeek (验证过): {key} {cur} → {value} — {gain}. {p.get('rationale', '')}",
            action=f"Set {key} = {value} (live, no restart)",
            payload={"key": key, "value": float(value)},
        )
        n += 1
    log.info("optimizer: %d/%d proposals passed backtest → enqueued",
             n, len(proposals))
    return n
