"""Autonomous optimizer (Gemini 3.5 Flash) — PROPOSES, never decides.

Given the weekly real-fill self-review, it asks Gemini for parameter-change
proposals (entry threshold / TP / SL) and writes them to the APPROVAL QUEUE.
The owner approves in the GUI/CLI; only then does a change take effect (runtime
override, no restart). This is the feedback-铁律 implementation of requirement
#8 — the system can optimize itself, but every change passes through the owner.

Uses the same Gemini model the rest of the system runs on (unified 2026-06-08;
DeepSeek removed). With no GEMINI_API_KEYS configured this is a no-op (the
rules-based suggestions in self_review still run).
"""
from __future__ import annotations

import json
import logging

from . import approvals, runtime_config
from .config import settings, gemini_cascade

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a quantitative trading risk optimizer for a $5k CASH swing bot "
    "(no leverage, max 5 positions, US large caps, weekly momentum-rotated "
    "universe). You tune ONLY these params: entry_threshold (55-85), "
    "tp_atr_mult (2-12), sl_atr_mult (2-6), breakeven_trigger_r (0.75-1.5), "
    "max_hold_days (5-10), universe_top_n (10-20), max_position_pct "
    "(0.20-0.55; ceiling is a hard tail-risk guard, never argue above it). "
    "KNOWN from the 2026-06-11/12 exhaustive sweeps: entry_threshold=70, "
    "tp_atr_mult=8 and max_hold_days=7 are at measured plateau peaks, and "
    "max_position_pct=0.40 beat 0.30/0.50/0.70 — only propose moving these "
    "if the review shows NEW evidence; small moves elsewhere are more likely "
    "to validate. "
    "The owner wants higher $/day WITHOUT reckless risk. Propose 0-3 SMALL, "
    "justified changes based on the real-fill review. Reply ONLY with a JSON "
    'array of {"key","value","rationale"} objects — rationale ONE short '
    "sentence, no prose outside the array. Empty array [] if no change is "
    "warranted."
)


def _current_params() -> dict:
    return {
        "entry_threshold": runtime_config.entry_threshold(),
        "tp_atr_mult": runtime_config.tp_atr_mult(),
        "sl_atr_mult": runtime_config.sl_atr_mult(),
        "breakeven_trigger_r": runtime_config.breakeven_trigger_r(),
        "max_hold_days": runtime_config.max_hold_days(),
        "universe_top_n": runtime_config.universe_top_n(),
        "max_position_pct": runtime_config.max_position_pct(),
    }


def _call_gemini(review: dict) -> list[dict]:
    """Ask Gemini (unified system model) for parameter-change proposals. Returns
    parsed proposals or []. No-op when no Gemini key is configured."""
    keys = list(settings.gemini_keys)
    if not keys:
        return []
    from google import genai
    prompt = (
        _SYSTEM
        + "\n\nCurrent params: " + json.dumps(_current_params())
        + "\nReal-fill review (last week): " + json.dumps({
            k: review.get(k) for k in
            ("n_trades", "per_day", "win_rate", "avg_r_multiple",
             "by_strategy", "by_exit", "target_note")
        })
        + "\n\nReply ONLY with the JSON array."
    )
    for model_name in gemini_cascade():
        for key in keys:
            try:
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(model=model_name, contents=prompt)
                content = (resp.text or "").strip()
                # Pull the first [...] array out (tolerates fences / stray prose).
                start, end = content.find("["), content.rfind("]")
                if start == -1 or end == -1 or end <= start:
                    log.warning("Gemini optimizer: no JSON array in response")
                    return []
                proposals = json.loads(content[start:end + 1])
                return proposals if isinstance(proposals, list) else []
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    continue  # quota on this key → next key
                log.warning("Gemini optimizer call failed (%s): %s", model_name, e)
                break
    return []


# Map a tunable param key → the BacktestConfig field it overrides.
_PARAM_TO_CFG = {
    "entry_threshold": "threshold",
    "tp_atr_mult": "tp_atr_mult",
    "sl_atr_mult": "sl_atr_mult",
    "risk_per_trade": "risk_per_trade",
    # 2026-06-11: the levers with remaining evidence-backed headroom.
    # (breakeven validates because _run_live_engine turns use_breakeven_stop on
    # from settings; universe_top_n only matters under apply_dynamic_universe.)
    "breakeven_trigger_r": "breakeven_trigger_r",
    "max_hold_days": "max_hold_days",
    "universe_top_n": "universe_top_n",
    "max_position_pct": "max_position_pct",
}
_INT_PARAMS = {"max_hold_days", "universe_top_n"}
_VALIDATE_WINDOWS = (180, 360)
_PINNED = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]


def _base_cfg(days: int):
    """Live-aligned honest-engine config. _run_live_engine adds VIX sizing +
    earnings gate + real commissions + momentum + Phase 0 realism on top.

    TWO parity rules, both load-bearing for an optimizer that may auto-apply:
    1. RUNTIME-EFFECTIVE values, not frozen .env: account_usd honours the
       db-state budget override, and threshold/tp/sl/risk honour runtime_config
       — otherwise the first applied proposal makes every later validation
       measure deltas against a base the live bot no longer runs.
    2. THE UNIVERSE THE BOT ACTUALLY TRADES: with the Phase 1 dynamic universe
       enabled, validate walk-forward over the full pool (the engine re-derives
       each week's top-N exactly like the live Sunday refresh); only when the
       flag is off fall back to the pinned list."""
    from src.backtest import BacktestConfig
    from . import risk_manager, runtime_config   # local: avoids import cycles
    if settings.dynamic_universe_enabled:
        from .universe import load_pool
        tickers, dyn = load_pool(), True
    else:
        tickers, dyn = list(_PINNED), False
    return BacktestConfig(
        days=days, timeframe=settings.timeframe,
        threshold=runtime_config.entry_threshold(),
        tickers=tickers, account_usd=risk_manager.budget_usd(),
        risk_per_trade=runtime_config.risk_per_trade(),
        max_position_pct=runtime_config.max_position_pct(),
        max_hold_days=runtime_config.max_hold_days(),
        tp_atr_mult=runtime_config.tp_atr_mult(), sl_atr_mult=runtime_config.sl_atr_mult(),
        max_gap_pct=settings.max_gap_pct, apply_ml_gate=False, apply_mr_strategy=False,
        use_scale_out=settings.use_scale_out, tp1_r=settings.tp1_r, tp2_r=settings.tp2_r,
        apply_dynamic_universe=dyn, universe_top_n=runtime_config.universe_top_n())


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
    # would make every Δ ≈ 0 and the validation meaningless. Scales with the
    # universe actually being validated (72-name pool vs 10-name pinned list).
    expected = len(base[_VALIDATE_WINDOWS[0]].tickers)
    need = max(6, int(expected * 0.8))
    for d in _VALIDATE_WINDOWS:
        got = len(cache[d].get("per_ticker", {}))
        if got < need:
            log.warning("optimizer: %dd prefetch degenerate (%d/%d tickers) — skipping",
                        d, got, expected)
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
    """YIELD-seeking gate (for the LLM's threshold/tp/sl ideas): keep only
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
        if key == "universe_top_n" and not settings.dynamic_universe_enabled:
            continue   # meaningless (and unvalidatable) without the dynamic universe
        from . import autopilot
        if autopilot.in_cooldown(key):
            log.info("optimizer: %s in post-rollback cooldown — skipped", key)
            continue
        cast = int if key in _INT_PARAMS else float
        try:
            pd_d, dd_d = {}, {}
            for d in _VALIDATE_WINDOWS:
                cfg = replace(base[d], **{field: cast(float(value))})
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
    """Weekly: Gemini proposes param tweaks → each is BACKTESTED on the honest
    engine → only proposals that beat the current config on BOTH 180d & 360d are
    enqueued for your approval (annotated with the measured $/day gain). Plausible-
    but-unvalidated LLM ideas are dropped. No-op (0) without a Gemini key.
    """
    proposals = _call_gemini(review)
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
        # Bounded autonomy (2026-06-11, owner mandate): with AUTO_APPLY_PARAMS
        # on, a proposal that PASSED the dual-window gate and sits inside the
        # pre-approved ALLOWED_PARAMS bounds is applied immediately — with a
        # Telegram notification (铁律: never silent) and an auto-rollback
        # watcher (autopilot.check_and_rollback reverts it if live results
        # degrade). Anything outside bounds still queues for approval.
        if settings.auto_apply_params:
            try:
                runtime_config.set_param(key, float(value), source="auto-optimizer")
                from . import notifier
                notifier.send(
                    f"🤖 *自动调参已应用* (预批边界内): {key} {cur} → {value}\n"
                    f"  依据: {gain}\n  {p.get('rationale', '')}\n"
                    f"  若后续实盘表现恶化将自动回滚并通知。回复可随时手动改回。")
                log.info("optimizer: AUTO-APPLIED %s=%s (%s)", key, value, gain)
                n += 1
                continue
            except ValueError as e:
                log.warning("optimizer: auto-apply rejected (%s) — queuing for approval", e)
        approvals.enqueue(
            kind="param_change",
            detail=f"Gemini (验证过): {key} {cur} → {value} — {gain}. {p.get('rationale', '')}",
            action=f"Set {key} = {value} (live, no restart)",
            payload={"key": key, "value": float(value)},
        )
        n += 1
    log.info("optimizer: %d/%d proposals passed backtest → %s",
             n, len(proposals),
             "auto-applied/enqueued" if settings.auto_apply_params else "enqueued")
    return n
