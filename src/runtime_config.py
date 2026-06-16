"""Runtime parameter overrides.

An owner-APPROVED param change (from the DeepSeek optimizer via the approval
queue) is written to db-state as `param_<key>` and read here — so it takes
effect on the NEXT scan with no restart, no .env edit. If no override exists,
the frozen .env setting is used (so default behavior is byte-identical until
the owner approves a change).

LIVE-ONLY: the backtest engine uses its own explicit cfg (cfg.threshold,
cfg.tp_atr_mult, …), so these overrides never perturb a backtest measurement.
"""
from __future__ import annotations

from . import db
from .config import settings


def _param(key: str):
    try:
        return db.get_state().get(f"param_{key}")
    except Exception:
        return None


def entry_threshold() -> float:
    v = _param("entry_threshold")
    try:
        return float(v) if v is not None else settings.entry_threshold
    except (TypeError, ValueError):
        return settings.entry_threshold


def tp_atr_mult() -> float:
    v = _param("tp_atr_mult")
    try:
        return float(v) if v is not None else settings.tp_atr_mult
    except (TypeError, ValueError):
        return settings.tp_atr_mult


def sl_atr_mult() -> float:
    v = _param("sl_atr_mult")
    try:
        return float(v) if v is not None else settings.sl_atr_mult
    except (TypeError, ValueError):
        return settings.sl_atr_mult


def risk_per_trade() -> float:
    v = _param("risk_per_trade")
    try:
        return float(v) if v is not None else settings.risk_per_trade
    except (TypeError, ValueError):
        return settings.risk_per_trade


def breakeven_trigger_r() -> float:
    v = _param("breakeven_trigger_r")
    try:
        return float(v) if v is not None else settings.breakeven_trigger_r
    except (TypeError, ValueError):
        return settings.breakeven_trigger_r


def max_hold_days() -> int:
    v = _param("max_hold_days")
    try:
        return int(float(v)) if v is not None else settings.max_hold_days
    except (TypeError, ValueError):
        return settings.max_hold_days


def universe_top_n() -> int:
    v = _param("universe_top_n")
    try:
        return int(float(v)) if v is not None else settings.universe_top_n
    except (TypeError, ValueError):
        return settings.universe_top_n


def max_position_pct() -> float:
    v = _param("max_position_pct")
    try:
        return float(v) if v is not None else settings.max_position_pct
    except (TypeError, ValueError):
        return settings.max_position_pct


# Whitelist of keys the optimizer is allowed to propose (guards against a bad
# LLM proposal touching something dangerous). Values are (min, max) sanity bounds.
# 2026-06-11: extended beyond threshold/tp/sl/risk — the exit audit measured all
# four at their plateau, so the levers with remaining evidence-backed movement
# (breakeven trigger, hold window, universe breadth) are now reachable too.
# Position cap / budget / regime multiplier stay OWNER-ONLY by design.
ALLOWED_PARAMS = {
    "entry_threshold": (55.0, 85.0),
    "tp_atr_mult": (2.0, 12.0),
    "sl_atr_mult": (2.0, 6.0),
    "risk_per_trade": (0.01, 0.08),
    "breakeven_trigger_r": (0.75, 1.5),
    "max_hold_days": (5.0, 10.0),
    "universe_top_n": (10.0, 20.0),
    # 2026-06-12 cap ablation: 30%=$12.8 / 40%=$22.1 / 50%=$17.6 / 70%=$19.2
    # per day — 40-70 statistically flat in-sample, so the optimizer may tune
    # within [0.20, 0.55]. The 0.55 ceiling is deliberate and NOT tunable: a
    # backtest can never price the overnight single-name gap (the window
    # contains no blowup), so the upper bound is the tail-risk guard the
    # in-sample gate structurally cannot provide.
    "max_position_pct": (0.20, 0.55),
}


def is_valid(key: str, value: float) -> bool:
    if key not in ALLOWED_PARAMS:
        return False
    lo, hi = ALLOWED_PARAMS[key]
    try:
        return lo <= float(value) <= hi
    except (TypeError, ValueError):
        return False


def current(key: str):
    """Runtime-effective value of an ALLOWED param (override beats .env)."""
    return {
        "entry_threshold": entry_threshold,
        "tp_atr_mult": tp_atr_mult,
        "sl_atr_mult": sl_atr_mult,
        "risk_per_trade": risk_per_trade,
        "breakeven_trigger_r": breakeven_trigger_r,
        "max_hold_days": max_hold_days,
        "universe_top_n": universe_top_n,
        "max_position_pct": max_position_pct,
    }[key]()


def set_param(key: str, value: float, source: str) -> dict:
    """Single write path for runtime param changes (approval executor AND the
    bounded-autonomy auto-apply both come through here). Validates against
    ALLOWED_PARAMS, records the change in the param_history db-state list
    (which powers the autopilot's auto-rollback), and returns the record.
    Raises ValueError on an out-of-bounds/unknown key."""
    if not is_valid(key, value):
        raise ValueError(f"param {key}={value} outside ALLOWED_PARAMS bounds")
    old = current(key)
    from datetime import datetime, timezone
    rec = {"key": key, "old": old, "new": float(value), "source": source,
           "applied_at": datetime.now(timezone.utc).isoformat(),
           "active": True}
    state = db.get_state()
    hist = list(state.get("param_history", []))
    # Supersede any still-active record for the same key.
    for h in hist:
        if h.get("key") == key:
            h["active"] = False
    hist.append(rec)
    db.update_state({f"param_{key}": float(value), "param_history": hist[-50:]})
    return rec


def revert_param(key: str, reason: str) -> dict | None:
    """Auto-rollback: restore the previous value of the most recent ACTIVE
    change for `key`. Marks the history record rolled_back; returns it (or
    None if there was nothing active to revert)."""
    state = db.get_state()
    hist = list(state.get("param_history", []))
    for h in reversed(hist):
        if h.get("key") == key and h.get("active"):
            h["active"] = False
            h["rolled_back"] = True
            h["rollback_reason"] = reason
            db.update_state({f"param_{key}": h.get("old"),
                             "param_history": hist})
            return h
    return None
