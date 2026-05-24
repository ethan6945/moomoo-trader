"""Earnings calendar — block entries within N days of next earnings report.

Uses `yfinance` (handles Yahoo's crumb/cookie flow).  Results cached
per-symbol for 7 days in data/earnings.json so we don't hammer Yahoo on
every scan.  ETFs return None (no earnings) — they always pass.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

CACHE_FILE = settings.root / "data" / "earnings.json"
CACHE_TTL_DAYS = 7
EARNINGS_AVOID_DAYS = 2   # block if earnings <= N days away


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _fetch_yahoo(symbol: str) -> str | None:
    """Return ISO date of next earnings, or None if unavailable (ETFs, missing data)."""
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        if not cal:
            return None
        ed = cal.get("Earnings Date")
        if not ed:
            return None
        # yfinance returns a list[date]; take the soonest future date.
        if isinstance(ed, list):
            future = [d for d in ed if isinstance(d, date) and d >= date.today()]
            if future:
                return min(future).isoformat()
            # All dates are in the past — use most recent as a hint.
            past = [d for d in ed if isinstance(d, date)]
            return max(past).isoformat() if past else None
        if isinstance(ed, date):
            return ed.isoformat()
        return None
    except Exception as e:
        log.warning("yfinance earnings fetch failed for %s: %s", symbol, e)
        return None


def get_next_earnings(symbol: str, force_refresh: bool = False) -> str | None:
    cache = _load_cache()
    key = symbol.upper()
    entry = cache.get(key, {})

    if not force_refresh and entry:
        try:
            age = (date.today() - date.fromisoformat(entry.get("fetched_at", ""))).days
            if age <= CACHE_TTL_DAYS:
                return entry.get("next_earnings")
        except ValueError:
            pass

    next_date = _fetch_yahoo(symbol)
    cache[key] = {
        "next_earnings": next_date,
        "fetched_at": date.today().isoformat(),
    }
    _save_cache(cache)
    return next_date


def earnings_block(symbol: str) -> tuple[bool, str]:
    """Return (blocked, reason). Blocked if earnings within EARNINGS_AVOID_DAYS."""
    next_date = get_next_earnings(symbol)
    if not next_date:
        return False, "no earnings (ETF or unavailable)"
    try:
        d = date.fromisoformat(next_date)
    except ValueError:
        return False, f"invalid date: {next_date}"
    days_until = (d - date.today()).days
    if 0 <= days_until <= EARNINGS_AVOID_DAYS:
        return True, f"earnings in {days_until}d ({next_date})"
    if days_until < 0:
        # Stale: earnings has passed; cache will refresh in next TTL window.
        return False, f"earnings was {next_date} (past — refresh pending)"
    return False, f"next earnings {next_date} ({days_until}d away)"
