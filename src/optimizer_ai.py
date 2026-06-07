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
    VIX sizing + earnings gate + real commissions + momentum on top."""
    from src.backtest import BacktestConfig
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=list(_PINNED), account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct, apply_ml_gate=False, apply_mr_strategy=False,
        use_scale_out=settings.use_scale_out, tp1_r=settings.tp1_r, tp2_r=settings.tp2_r)


def _per_day(cfg, cache, days: int) -> float:
    from src.backtest import _run_live_engine
    m = _run_live_engine(cfg, cache, rich_metrics=False)["metrics"]
    return m.get("net_pnl_usd", 0.0) / days


def validate_proposals(proposals: list[dict]) -> list[dict]:
    """Backtest each proposal on the honest engine vs the current config. Returns
    only proposals that BEAT baseline on BOTH windows, annotated with the deltas.
    Empty list if the backtest can't run (so nothing unvalidated is enqueued)."""
    from dataclasses import replace
    from src.backtest import prefetch_data
    try:
        base = {d: _base_cfg(d) for d in _VALIDATE_WINDOWS}
        cache = {d: prefetch_data(base[d]) for d in _VALIDATE_WINDOWS}
        # Guard against a degenerate prefetch (OpenD hiccup → too few tickers),
        # which would make every Δ ≈ 0 and the validation meaningless. Require
        # most of the universe to have loaded; else skip (enqueue nothing).
        need = max(6, len(_PINNED) - 2)
        for d in _VALIDATE_WINDOWS:
            got = len(cache[d].get("per_ticker", {}))
            if got < need:
                log.warning("optimizer: %dd prefetch degenerate (%d/%d tickers) "
                            "— skipping validation, no proposals enqueued", d, got, len(_PINNED))
                return []
        base_pd = {d: _per_day(base[d], cache[d], d) for d in _VALIDATE_WINDOWS}
    except Exception as e:
        log.warning("optimizer: backtest setup failed (%s) — no proposals enqueued", e)
        return []

    validated = []
    for p in proposals:
        key, value = p.get("key"), p.get("value")
        field = _PARAM_TO_CFG.get(key)
        if not field or not runtime_config.is_valid(key, value):
            continue
        try:
            deltas = {}
            for d in _VALIDATE_WINDOWS:
                cfg = replace(base[d], **{field: float(value)})
                deltas[d] = _per_day(cfg, cache[d], d) - base_pd[d]
        except Exception as e:
            log.warning("optimizer: backtest of %s=%s failed: %s", key, value, e)
            continue
        beats_both = all(deltas[d] > 0 for d in _VALIDATE_WINDOWS)
        log.info("optimizer: %s=%s → Δ180d %+.2f, Δ360d %+.2f → %s",
                 key, value, deltas[180], deltas[360], "PASS" if beats_both else "drop")
        if beats_both:
            p = dict(p, _deltas=deltas)
            validated.append(p)
    return validated


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
        gain = f"backtested +${d.get(180, 0):.1f}/day(180d), +${d.get(360, 0):.1f}/day(360d)"
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
