"""Missed-job catchup tracker for the scheduler.

Problem: APScheduler's `misfire_grace_time` only catches very recent misses
(seconds-to-minutes). If the laptop is off when a weekly or monthly job is
due, that job is **silently skipped forever** when the scheduler restarts.

Solution: persist the last successful run timestamp for each named job to
`data/cron_state.json`. On scheduler startup, compute the "most-recent
expected fire" for each job's schedule; if `last_run < expected`, we fire
the job once (catchup), then resume the normal schedule.

Coalesce policy: missed runs are coalesced — we never fire a job multiple
times in a row to "catch up the backlog". One catchup per missed schedule
cycle is enough (monthly ML retrain runs once even if 3 months were missed).

Fresh-install policy: an empty state file is initialised to NOW for every
known job, so the first boot doesn't trigger every job at once.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import clock
from .config import settings

log = logging.getLogger(__name__)

STATE_FILE = settings.root / "data" / "cron_state.json"

# Every named job that participates in the catchup system. Adding a new
# scheduled job? Append it here and call `record_run(name)` on success.
KNOWN_JOBS: tuple[str, ...] = (
    "ml_retrain",
    "watchlist_refresh",
    "weekly_backtest",
    "monthly_optuna",
    "daily_blacklist",
)


# ---------- persistence ----------

def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("cron_state load failed (%s) — starting fresh", e)
        return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------- public state API ----------

def record_run(job_name: str) -> None:
    """Mark `job_name` as having successfully run NOW. Idempotent."""
    state = _load()
    state[job_name] = {"last_run": clock.ny_now().isoformat()}
    _save(state)


def last_run(job_name: str) -> Optional[datetime]:
    """Return last successful run as a NY-aware datetime, or None if unknown."""
    entry = _load().get(job_name)
    if not entry:
        return None
    try:
        ts = entry["last_run"]
        # `ny_now()` writes tz-aware ISO; fromisoformat needs the tz suffix
        # in 3.11+. Strip 'Z' if a future change introduces it.
        return datetime.fromisoformat(ts.replace("Z", ""))
    except (KeyError, ValueError):
        return None


def initialize_if_empty() -> bool:
    """First-boot guard: seed every KNOWN_JOBS with last_run=now so we don't
    fire all of them at first start. Returns True if it actually wrote."""
    state = _load()
    if state:
        return False
    now_iso = clock.ny_now().isoformat()
    for job in KNOWN_JOBS:
        state[job] = {"last_run": now_iso}
    _save(state)
    log.info("cron_state: first boot — initialised %d jobs to %s",
             len(KNOWN_JOBS), now_iso)
    return True


# ---------- "expected last fire" calculators ----------

def expected_last_fire_daily(hour: int, minute: int,
                              weekdays_only: bool = True) -> datetime:
    """Most recent past fire time for a daily cron at HH:MM.

    If `weekdays_only`, skip Sat/Sun back to the previous weekday.
    """
    now = clock.ny_now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    if weekdays_only:
        # weekday(): Mon=0 ... Sun=6
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
    return candidate


def expected_last_fire_weekly(weekday: int, hour: int, minute: int) -> datetime:
    """Most recent past fire for a weekly cron on (weekday) at HH:MM.

    `weekday`: 0=Mon ... 6=Sun.
    """
    now = clock.ny_now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_back = (now.weekday() - weekday) % 7
    # If today IS the cron day but the candidate time is still in the future,
    # the most recent fire was a week ago.
    if days_back == 0 and candidate > now:
        days_back = 7
    return candidate - timedelta(days=days_back)


def expected_last_fire_monthly(day: int, hour: int, minute: int) -> datetime:
    """Most recent past fire for a monthly cron on the N-th day at HH:MM."""
    now = clock.ny_now()
    try:
        candidate = now.replace(day=day, hour=hour, minute=minute,
                                second=0, microsecond=0)
    except ValueError:
        # Month has fewer days than `day` (e.g. Feb 30) — fall back to the
        # 1st of the next month, then step back.
        candidate = now.replace(day=1, hour=hour, minute=minute,
                                second=0, microsecond=0)
    if candidate > now:
        # Go back to the previous month.
        if candidate.month == 1:
            candidate = candidate.replace(year=candidate.year - 1, month=12)
        else:
            candidate = candidate.replace(month=candidate.month - 1)
    return candidate


# ---------- catchup decision ----------

def needs_catchup(job_name: str, expected_fire: datetime) -> bool:
    """True iff the job's last_run is BEFORE the most recent expected fire."""
    lr = last_run(job_name)
    if lr is None:
        # Unknown job → never run before; conservative: do NOT catchup
        # (initialize_if_empty() should have seeded all KNOWN_JOBS).
        return False
    # Strip tz info for naive comparison if datetimes differ in tz handling.
    if lr.tzinfo is None and expected_fire.tzinfo is not None:
        expected_fire = expected_fire.replace(tzinfo=None)
    elif lr.tzinfo is not None and expected_fire.tzinfo is None:
        lr = lr.replace(tzinfo=None)
    return lr < expected_fire
