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


# Whitelist of keys the optimizer is allowed to propose (guards against a bad
# LLM proposal touching something dangerous). Values are (min, max) sanity bounds.
ALLOWED_PARAMS = {
    "entry_threshold": (55.0, 85.0),
    "tp_atr_mult": (2.0, 12.0),
    "sl_atr_mult": (2.0, 6.0),
    "risk_per_trade": (0.01, 0.08),
}


def is_valid(key: str, value: float) -> bool:
    if key not in ALLOWED_PARAMS:
        return False
    lo, hi = ALLOWED_PARAMS[key]
    try:
        return lo <= float(value) <= hi
    except (TypeError, ValueError):
        return False
