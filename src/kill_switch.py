"""Centralised "can we open new positions right now?" decision.

Used to live across three places:
  • main.py:  in_trade_phase() check at top of scan_once
  • main.py:  regime.block_new_entries check before candidate loop
  • risk_manager.can_open_new():  halted + drawdown checks

Each had different short-circuit shapes (return / continue / break), different
audit formats, and the daily-reset for `halted` only fired lazily from inside
can_open_new — which left a "ghost halt" if no candidate reached that stage.

This module pulls all four checks into one function so:
  1. The same logic runs at the same point in every scan.
  2. The audit gate name is consistent ("kill_switch").
  3. The GUI can read one snapshot ("can_trade", "reason") instead of guessing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import clock, db
from .config import settings

log = logging.getLogger(__name__)

# Trade-phase boundaries (NY local time, minutes from midnight).
# Skip first 15 min of session (gap noise) + last 30 min (MOC volatility).
TRADE_PHASE_START_MIN = 9 * 60 + 45    # 09:45 ET
TRADE_PHASE_END_MIN = 15 * 60 + 30     # 15:30 ET

# Friday no-new-entries cutoff — weekend gap risk on swing positions.
# After this time on Friday we still MANAGE open positions (stops + tps still
# fire) but refuse to open new names. Existing positions can still close out
# normally. 2026-05-28: added per audit recommendation.
FRIDAY_CUTOFF_MIN = 14 * 60      # 14:00 ET


@dataclass(frozen=True)
class KillSwitchVerdict:
    can_trade: bool
    reason: str
    gate: str               # which check fired, e.g. "trade_phase" | "regime" | "halt"


def in_trade_phase(now: Optional[datetime] = None) -> bool:
    n = now or clock.ny_now()
    if n.weekday() >= 5:
        return False
    minutes = n.hour * 60 + n.minute
    # 2026-06-26 strict-boundary fix: 15:30 is the start of MOC volatility —
    # the bot should NOT open new entries at/after 15:30. Use < instead of <=
    # so the last valid entry minute is 15:29, consistent with the docstring.
    return TRADE_PHASE_START_MIN <= minutes < TRADE_PHASE_END_MIN


def evaluate(regime_block_new: bool, regime_label: str, regime_note: str,
             current_cash: float = 0.0) -> KillSwitchVerdict:
    """Run every "should we even try to open a new position" check.

    Caller still does manage_open_trades + snapshot refresh on `can_trade=False`
    — this only governs new entries.
    """
    now = clock.ny_now()
    if not in_trade_phase(now):
        return KillSwitchVerdict(
            can_trade=False,
            reason=f"outside trade phase ({now:%H:%M} ET, allowed 09:45-15:30)",
            gate="trade_phase",
        )

    # Friday no-new-entries cutoff — avoid weekend gap risk on a 7-day max-hold
    # swing. Existing positions still managed by manage_open_trades; we only
    # block NEW entries here.
    if now.weekday() == 4:   # 4 = Friday
        minutes = now.hour * 60 + now.minute
        if minutes >= FRIDAY_CUTOFF_MIN:
            return KillSwitchVerdict(
                can_trade=False,
                reason=f"Friday after {FRIDAY_CUTOFF_MIN // 60:02d}:00 ET — no new entries (weekend gap protection)",
                gate="friday_cutoff",
            )

    if regime_block_new:
        return KillSwitchVerdict(
            can_trade=False,
            reason=f"regime {regime_label}: {regime_note}",
            gate="regime",
        )

    # Halt / drawdown via SQLite atomic read (no torn data even if scheduler
    # and a manual-close are racing).
    state = db.get_state()
    if state.get("halted"):
        return KillSwitchVerdict(
            can_trade=False,
            reason="halted (loss-streak or drawdown — click 'Halted' label to reset)",
            gate="halt",
        )

    starting_cash = float(state.get("starting_cash") or 0)
    if starting_cash > 0:
        drawdown = (starting_cash - current_cash) / starting_cash
        if drawdown >= settings.daily_drawdown_stop:
            # Set the halt flag atomically so subsequent scans short-circuit.
            db.atomic_state(lambda s: {"halted": True})
            return KillSwitchVerdict(
                can_trade=False,
                reason=(f"daily drawdown {drawdown:.1%} "
                        f"≥ {settings.daily_drawdown_stop:.0%} — auto-halted"),
                gate="drawdown",
            )

    return KillSwitchVerdict(can_trade=True, reason="ok", gate="")


def reset_for_new_day(current_cash: float) -> None:
    """Idempotent daily rollover. Safe to call at the top of every scan."""
    today_str = clock.ny_now().strftime("%Y-%m-%d")
    def _apply(s: dict) -> dict:
        if s.get("day") != today_str:
            return {
                "day": today_str,
                "starting_cash": current_cash,
                "realized_pnl_today": 0.0,
                "halted": False,
            }
        return {}
    db.atomic_state(_apply)
