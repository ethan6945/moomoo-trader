"""Adaptive symbol blacklist — soft ban with periodic re-review.

Why this is NOT a hard ban list:
  A symbol may underperform for 30 days because of one bad news event, then
  recover when the catalyst passes. A rigid blacklist forever-excludes those
  recoveries. So every entry here has a review period; on each review we
  re-decide based on current data.

Adaptive elements (the "AI 变通" the user asked for):
  1. The bar for entering the list scales with overall portfolio health.
     - Strong portfolio (Sortino > 3): cull aggressively (kick anyone losing $25+)
     - Struggling portfolio: be lenient (everyone is losing, blame regime not stock)
  2. Each entry has a *next_review_days* that grows on consecutive bad reviews
     (we give a sicker symbol more time to recover, not less — opposite of
     a "three strikes" rule, which would be too harsh).
  3. A symbol that improves between reviews is removed immediately.
  4. Manual override via `force_review(sym)` for the GUI.

Data:
  data/blacklist.json — list of entries with timestamps, snapshots, reasons.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import db
from .config import settings

log = logging.getLogger(__name__)

BLACKLIST_FILE = settings.root / "data" / "blacklist.json"

# Look-back window when computing per-symbol PnL.
LOOKBACK_TRADES = 60
# Minimum number of trades for a symbol before we consider blacklisting.
MIN_TRADES_FOR_REVIEW = 6

# Review-period growth caps.
INITIAL_REVIEW_DAYS = 30
MAX_REVIEW_DAYS = 90


@dataclass
class BlacklistEntry:
    symbol: str
    added_at: str
    last_review_at: str
    review_count: int = 1
    snapshot_pnl: float = 0.0
    snapshot_trades: int = 0
    next_review_days: int = INITIAL_REVIEW_DAYS
    reason: str = ""


# ---------- persistence ----------

def _load() -> dict[str, BlacklistEntry]:
    if not BLACKLIST_FILE.exists():
        return {}
    try:
        raw = json.loads(BLACKLIST_FILE.read_text())
        return {sym: BlacklistEntry(**data) for sym, data in raw.items()}
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("blacklist load failed (%s) — starting empty", e)
        return {}


def _save(entries: dict[str, BlacklistEntry]) -> None:
    BLACKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    serial = {sym: asdict(e) for sym, e in entries.items()}
    BLACKLIST_FILE.write_text(json.dumps(serial, indent=2))


# ---------- per-symbol stats ----------

def _per_symbol_pnl(lookback: int = LOOKBACK_TRADES) -> dict[str, tuple[float, int]]:
    """Return {symbol: (cumulative_pnl, trade_count)} over the last `lookback`
    closed trades."""
    rows = db.closed_trades(limit=10_000)
    if len(rows) > lookback:
        rows = rows[-lookback:]
    agg: dict[str, tuple[float, int]] = {}
    counts: dict[str, int] = defaultdict(int)
    pnls: dict[str, float] = defaultdict(float)
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        counts[sym] += 1
        pnls[sym] += float(r.get("pnl", 0.0))
    for sym in counts:
        agg[sym] = (round(pnls[sym], 2), counts[sym])
    return agg


# ---------- adaptive thresholds ----------

def _bad_pnl_threshold() -> float:
    """Dollar PnL below which a symbol counts as "bad enough to blacklist".

    The threshold scales with overall portfolio Sortino — strict when the
    bot is winning, lenient when it's losing. This is the "AI 变通" the
    user requested: rules aren't fixed numbers, they read the room.
    """
    try:
        from . import adaptive_sizing
        s = adaptive_sizing.recent_sortino()
    except Exception:
        s = 0.0
    if s > 3:
        return -25.0    # bot is hot — kick any net loser
    if s > 1:
        return -50.0    # OK — only kick meaningful losers
    if s > 0:
        return -100.0   # struggling — most things lose, demand more evidence
    return -200.0       # bad market — don't blame symbols at all


# ---------- core operations ----------

def is_blacklisted(symbol: str) -> bool:
    return symbol.upper() in _load()


def get_blacklist() -> dict[str, BlacklistEntry]:
    return _load()


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def evaluate_all(notifier_send=None) -> dict:
    """Re-evaluate every existing entry + scan for new candidates.

    Called from the daily scheduler job. Returns a summary dict useful for
    Telegram. `notifier_send` is the notifier.send function (optional —
    passed in to avoid import cycle).
    """
    entries = _load()
    per_sym = _per_symbol_pnl()
    bad_threshold = _bad_pnl_threshold()
    now = datetime.utcnow()

    added: list[str] = []
    removed: list[str] = []
    extended: list[str] = []

    # --- Step 1: review existing entries ---
    for sym, entry in list(entries.items()):
        try:
            last_review = datetime.fromisoformat(entry.last_review_at.rstrip("Z"))
        except ValueError:
            last_review = now - timedelta(days=999)
        days_since = (now - last_review).days
        if days_since < entry.next_review_days:
            continue   # not yet due

        cur_pnl, cur_trades = per_sym.get(sym, (0.0, 0))
        # No new data? Keep as-is, just bump the review clock so we don't
        # spam re-evals.
        if cur_trades == entry.snapshot_trades:
            entry.last_review_at = _now_iso()
            log.info("[blacklist] %s: no new trades since last review, deferring",
                     sym)
            continue

        # Symbol improved meaningfully — remove. "Meaningfully" means PnL
        # rose by at least $30 since snapshot OR is now above the bad-threshold.
        improved = (cur_pnl > entry.snapshot_pnl + 30.0
                    or cur_pnl > bad_threshold)
        if improved:
            entries.pop(sym)
            removed.append(sym)
            log.info("[blacklist] REMOVE %s — PnL recovered from $%.2f to $%.2f",
                     sym, entry.snapshot_pnl, cur_pnl)
            continue

        # Still bad → extend review period (give it more time to heal,
        # but not less risk-managed). Each consecutive bad review adds 15
        # days, capped at 90.
        entry.last_review_at = _now_iso()
        entry.review_count += 1
        entry.snapshot_pnl = cur_pnl
        entry.snapshot_trades = cur_trades
        entry.next_review_days = min(MAX_REVIEW_DAYS, entry.next_review_days + 15)
        entry.reason = (f"review #{entry.review_count}: PnL=$"
                        f"{cur_pnl:.2f} over {cur_trades} trades; "
                        f"next review in {entry.next_review_days}d")
        extended.append(sym)
        log.info("[blacklist] EXTEND %s — PnL $%.2f, next review in %dd",
                 sym, cur_pnl, entry.next_review_days)

    # --- Step 2: scan for new candidates ---
    for sym, (pnl, n_trades) in per_sym.items():
        if sym in entries:
            continue
        if n_trades < MIN_TRADES_FOR_REVIEW:
            continue
        if pnl >= bad_threshold:
            continue
        entries[sym] = BlacklistEntry(
            symbol=sym,
            added_at=_now_iso(),
            last_review_at=_now_iso(),
            snapshot_pnl=pnl,
            snapshot_trades=n_trades,
            next_review_days=INITIAL_REVIEW_DAYS,
            reason=(f"60-trade PnL=$ {pnl:.2f} over {n_trades} trades; "
                    f"threshold ${bad_threshold:.0f} (adaptive)"),
        )
        added.append(sym)
        log.info("[blacklist] ADD %s — PnL=$%.2f over %d trades (threshold=$%.2f)",
                 sym, pnl, n_trades, bad_threshold)

    _save(entries)

    summary = {
        "added": added,
        "removed": removed,
        "extended": extended,
        "active_count": len(entries),
        "threshold_used": bad_threshold,
    }

    if notifier_send and (added or removed or extended):
        lines = ["📋 *Blacklist review*"]
        if added:
            lines.append(f"  ➕ Added: {', '.join(added)}")
        if removed:
            lines.append(f"  ➖ Removed (recovered): {', '.join(removed)}")
        if extended:
            lines.append(f"  ⏱ Extended: {', '.join(extended)}")
        lines.append(f"  Threshold: ${bad_threshold:.0f}  Active: {len(entries)}")
        try:
            notifier_send("\n".join(lines))
        except Exception as e:
            log.warning("blacklist notifier failed: %s", e)

    return summary


def force_review(symbol: str) -> str:
    """Manual override — force an immediate review of `symbol`. GUI uses this."""
    entries = _load()
    sym = symbol.upper()
    if sym not in entries:
        return f"{sym} not in blacklist"
    entries[sym].last_review_at = "1970-01-01T00:00:00Z"  # force overdue
    _save(entries)
    return f"{sym} marked for immediate review"


def manual_remove(symbol: str) -> str:
    """Manual override — remove a symbol from the blacklist immediately."""
    entries = _load()
    sym = symbol.upper()
    if sym not in entries:
        return f"{sym} not in blacklist"
    entries.pop(sym)
    _save(entries)
    return f"{sym} removed from blacklist"
