"""Authoritative NY time + clock-drift detection.

pytz already handles EDT/EST DST switching correctly given the right tz
database (auto-updated by the OS).  What it can't catch: a misconfigured
or drifted machine clock.  If your laptop is 5 minutes fast, the bot will
fire trades 5 minutes early — past the 09:45 ET entry window cutoff if you
were just before it, or into the 15:30 ET MOC zone you're supposed to avoid.

This module:
  • Returns NY-aware datetimes via `ny_now()` (drop-in replacement for the
    old `datetime.now(pytz.timezone('America/New_York'))` calls).
  • Periodically (default: hourly) hits a public time API to compute the
    offset between the network and the local clock.
  • If drift > MAX_DRIFT_SEC, applies the offset so every subsequent call
    returns the network-authoritative time.
  • Caches the offset; non-validation calls are zero-cost.
  • Falls through to plain pytz if all network sources fail (so we never
    hard-fail when offline — just log a warning).

DST handling is automatic: the API responses are in America/New_York with
the correct EDT/EST abbreviation, and `pytz.timezone(...)` localises them
the same way.  Spring-forward / fall-back happens without any code change.
"""
from __future__ import annotations

import logging
import struct
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
import requests

log = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")

# How often we hit the network to re-check drift.
CHECK_INTERVAL_SEC = 3600              # once per hour is plenty
# Anything beyond this is treated as actual drift worth correcting.
MAX_DRIFT_SEC = 5.0
# HTTP request timeout (seconds).
HTTP_TIMEOUT = 4.0
# NTP server (RFC 5905). NIST is the canonical US reference.
NTP_SERVER = "time.nist.gov"
NTP_PORT = 123
NTP_TIMEOUT = 3.0

# Shared state (thread-safe via _lock).
_lock = threading.Lock()
_offset_sec: float = 0.0
_last_check: Optional[datetime] = None
_last_source: str = "system"
_last_drift: float = 0.0


# ---------- time sources ----------

def _from_timeapi_io() -> Optional[datetime]:
    """Most reliable in our smoke tests."""
    try:
        r = requests.get(
            "https://timeapi.io/api/Time/current/zone?timeZone=America/New_York",
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # Returns: "2026-05-20T10:15:30.1234567" — naive, NY-local
        iso = data.get("dateTime")
        if not iso:
            return None
        naive = datetime.fromisoformat(iso)
        return NY_TZ.localize(naive)
    except Exception as e:
        log.debug("timeapi.io failed: %s", e)
        return None


def _from_worldtimeapi() -> Optional[datetime]:
    """Backup source — handles DST abbreviation explicitly."""
    try:
        r = requests.get(
            "https://worldtimeapi.org/api/timezone/America/New_York",
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # "datetime" is tz-aware ISO e.g. "2026-05-20T10:15:30.123456-04:00"
        iso = data.get("datetime")
        if not iso:
            return None
        return datetime.fromisoformat(iso)
    except Exception as e:
        log.debug("worldtimeapi failed: %s", e)
        return None


def _from_ntp() -> Optional[datetime]:
    """RFC 5905 NTP query — last-resort fallback when HTTP is blocked."""
    try:
        import socket
        msg = b"\x1b" + 47 * b"\0"
        addr = (NTP_SERVER, NTP_PORT)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(NTP_TIMEOUT)
        try:
            s.sendto(msg, addr)
            data, _ = s.recvfrom(1024)
        finally:
            s.close()
        if len(data) < 48:
            return None
        # Transmit timestamp is at byte 40 (8 bytes: seconds + fraction).
        seconds = struct.unpack("!I", data[40:44])[0] - 2208988800  # NTP epoch → Unix epoch
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(NY_TZ)
    except Exception as e:
        log.debug("NTP query failed: %s", e)
        return None


def _fetch_authoritative_ny() -> tuple[Optional[datetime], str]:
    """Try HTTP sources in order, then NTP. Returns (ny_time, source_label)."""
    for fetcher, label in (
        (_from_timeapi_io, "timeapi.io"),
        (_from_worldtimeapi, "worldtimeapi.org"),
        (_from_ntp, "ntp"),
    ):
        t = fetcher()
        if t is not None:
            return t, label
    return None, "system"


# ---------- public API ----------

def _maybe_refresh() -> None:
    """Re-check drift if CHECK_INTERVAL_SEC has elapsed since the last check."""
    global _offset_sec, _last_check, _last_source, _last_drift
    sys_ny = datetime.now(NY_TZ)
    with _lock:
        if _last_check is not None:
            if (sys_ny - _last_check).total_seconds() < CHECK_INTERVAL_SEC:
                return
        net, source = _fetch_authoritative_ny()
        _last_check = sys_ny
        if net is None:
            _last_source = "system (network unreachable)"
            log.info("clock: network sources unreachable — using system + pytz")
            return
        drift = (net - sys_ny).total_seconds()
        _last_drift = drift
        _last_source = source
        if abs(drift) > MAX_DRIFT_SEC:
            _offset_sec = drift
            log.warning(
                "clock drift %+.1fs vs %s — applying correction. "
                "(Re-sync your system clock to fix permanently.)",
                drift, source,
            )
        else:
            _offset_sec = 0.0
            log.info("clock OK: %s shows %+.2fs drift (≤ %.1fs threshold)",
                     source, drift, MAX_DRIFT_SEC)


def ny_now() -> datetime:
    """Return current NY-aware datetime, corrected for any detected drift.

    Side effect: triggers a network re-check at most once per
    CHECK_INTERVAL_SEC.  All other calls are pure-Python and instant."""
    _maybe_refresh()
    return datetime.now(NY_TZ) + timedelta(seconds=_offset_sec)


def utc_now_corrected() -> datetime:
    """Same correction, but returned in UTC. Used by the heartbeat timestamps
    so they're consistent with ny_now()."""
    _maybe_refresh()
    return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=_offset_sec)


def status() -> dict:
    """Snapshot of clock health for the GUI / audit."""
    with _lock:
        return {
            "offset_sec": round(_offset_sec, 2),
            "last_drift_sec": round(_last_drift, 2),
            "last_check": _last_check.isoformat() if _last_check else None,
            "source": _last_source,
            "max_drift_threshold": MAX_DRIFT_SEC,
        }


def force_refresh() -> dict:
    """Force-bypass the cache (used by GUI 'Sync Clock' button)."""
    global _last_check
    with _lock:
        _last_check = None
    _maybe_refresh()
    return status()
