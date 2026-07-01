"""Append-only history log.

Every scan appends one JSON line to `data/history.jsonl` containing:
    timestamp, week (YYYY-Www), invested, unrealized_pnl, realized_total,
    positions_count, position_symbols.

We never overwrite — easy to grep / parse / re-aggregate later. GUI reads the
last N lines and groups by ISO week for the weekly view.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from . import db
from .config import settings

HISTORY_FILE = settings.root / "data" / "history.jsonl"   # legacy mirror


def append(snapshot: dict) -> None:
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "week": date.today().strftime("%Y-W%V"),
        "day": date.today().isoformat(),
        **snapshot,
    }
    # Primary: SQLite. Mirror: legacy JSONL.
    sql_ok = True
    try:
        db.history_insert(record)
    except Exception:
        sql_ok = False
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    if not sql_ok:
        import logging
        logging.getLogger(__name__).warning("history SQLite insert failed — record in JSONL only")


def load_all() -> list[dict]:
    return db.history_rows(limit=10_000)


def weekly_digest() -> list[dict]:
    by_week: dict[str, dict] = {}
    for r in load_all():
        by_week[r.get("week", "")] = r
    return list(by_week.values())
