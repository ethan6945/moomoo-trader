"""Append-only audit log of every entry decision.

Why: when the bot doesn't trade, you should be able to ask "why didn't it
buy X today?" without grepping trader.log.  Every decision (skip / buy /
error / scan boundary) lands in data/audit.jsonl with full context.

The GUI's Audit panel reads from this file directly.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import pytz

from . import db
from .config import settings

log = logging.getLogger(__name__)

AUDIT_FILE = settings.root / "data" / "audit.jsonl"   # legacy mirror
NY = pytz.timezone("America/New_York")


def record(
    action: str,
    symbol: str = "",
    gate: str = "",
    reason: str = "",
    score: float = 0.0,
    extra: Optional[dict] = None,
) -> None:
    ts = datetime.now(NY).isoformat()
    try:
        db.audit_insert(action=action, symbol=symbol, gate=gate, reason=reason,
                        score=score, extra=extra, ts=ts)
    except Exception as e:
        log.warning("audit SQLite insert failed: %s", e)
    # legacy JSONL mirror (best-effort, append-only is already crash-safe)
    try:
        entry = {"ts": ts, "action": action, "symbol": symbol,
                 "gate": gate, "reason": reason, "score": score}
        if extra:
            entry.update(extra)
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        log.warning("audit legacy mirror failed: %s", e)


def recent(n: int = 100) -> list[dict]:
    """Read from SQLite — single source of truth now."""
    return db.audit_recent(n)


def gate_summary(n: int = 200) -> dict:
    return db.audit_gate_summary(n)
