"""Risk gate — hard rules. AI cannot bypass these.

Each function returns (allowed: bool, reason: str). The caller MUST short-circuit
on the first False.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from . import db
from .config import settings
from .indicators import Signal

log = logging.getLogger(__name__)

STATE_FILE = settings.root / "data" / "state.json"   # legacy mirror only

_DEFAULT_STATE = {
    "day": str(date.today()),
    "starting_cash": 0.0,
    "realized_pnl_today": 0.0,
    "loss_streak_days": 0,
    "last_close_day": None,
    "halted": False,
    # Account-level peak equity for the drawdown circuit breaker. Tracked
    # via record_trade_close on every realised PnL — independent of
    # starting_cash so it survives day rollovers.
    "peak_equity": 0.0,
}


def _load_state() -> dict:
    """Read all kv_state rows; fill defaults for missing keys."""
    s = dict(_DEFAULT_STATE)
    s.update(db.get_state())
    return s


def _save_state(state: dict) -> None:
    """Persist state atomically to SQLite; mirror to legacy JSON for any
    external script still poking at it."""
    db.save_state(state)
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.warning("legacy state.json mirror failed: %s", e)


def reset_for_new_day(current_cash: float) -> dict:
    state = _load_state()
    today = str(date.today())
    if state["day"] != today:
        state["day"] = today
        state["starting_cash"] = current_cash
        state["realized_pnl_today"] = 0.0
        state["halted"] = False
        _save_state(state)
    return state


def _drawdown_risk_multiplier() -> float:
    """Reduce risk after consecutive loss days.

    2 losing days  → 75% risk
    3+ losing days → 50% risk
    Resets to 100% on next winning day (handled in record_trade_close).
    """
    state = _load_state()
    losses = state.get("loss_streak_days", 0)
    if losses >= 3:
        return 0.5
    if losses == 2:
        return 0.75
    return 1.0


def current_drawdown_pct() -> float:
    """Account-level drawdown as a percent, computed from peak_equity in state.

    Returns 0.0 if peak is unknown (no closed trades yet) — i.e. a fresh
    account starts at 0% DD and can't be circuit-broken.
    """
    state = _load_state()
    peak = float(state.get("peak_equity") or 0.0)
    if peak <= 0:
        return 0.0
    realized = float(state.get("realized_pnl_total") or 0.0)
    equity = settings.account_usd + realized
    if equity >= peak:
        return 0.0
    return (peak - equity) / peak * 100


def _dd_size_multiplier() -> float:
    """Halve qty when DD ≥ DD_SIZE_CUT_PCT (default 10%).
    Returns 1.0 below threshold."""
    dd = current_drawdown_pct()
    if dd >= settings.dd_size_cut_pct:
        return 0.5
    return 1.0


def calc_position_size(signal: Signal, vix: float = 15.0,
                       conviction: float = 1.0) -> int:
    """Risk-based sizing with three independent scaling layers.

    Layer 1 — Base risk:      account_usd × risk_per_trade  (e.g. 2% of $4500 = $90)
    Layer 2 — Drawdown mult:  100% / 75% / 50% by recent loss streak
    Layer 3 — VIX mult:       100% / 50% / 25% by vol regime
    Layer 4 — ML conviction:  caller-supplied 0.0-1.0 multiplier
                              (0.5 for neutral-zone ML, 1.0 for high-conviction)

    Final qty = floor(min(risk_qty, cap_qty)) — all multipliers compose.
    """
    if conviction <= 0:
        return 0
    dd_mult = _drawdown_risk_multiplier()
    # Account-level DD breaker (new): independent of loss-streak.
    dd_size_mult = _dd_size_multiplier()
    # Adaptive sizing — follows the rolling-30-trade Sortino. Lazy import
    # avoids a hard dependency at module load time (and a circular if
    # adaptive_sizing ever needs to read settings/portfolio).
    try:
        from . import adaptive_sizing
        adaptive_mult, _adaptive_reason = adaptive_sizing.compute_multiplier()
    except Exception as e:
        log.debug("adaptive_sizing skipped: %s", e)
        adaptive_mult = 1.0
    risk_dollars = (settings.account_usd * settings.risk_per_trade
                    * dd_mult * dd_size_mult * adaptive_mult * conviction)

    stop_distance = signal.price - signal.stop_loss
    if stop_distance <= 0:
        return 0
    qty_by_risk = int(risk_dollars / stop_distance)
    qty_by_cap = int(settings.account_usd * settings.max_position_pct / signal.price)
    base = max(0, min(qty_by_risk, qty_by_cap))
    if base <= 0:
        return 0

    if vix > 35:
        return max(1, base // 4)
    if vix > 25:
        return max(1, base // 2)
    return base


def can_open_new(
    signal: Signal,
    positions: pd.DataFrame,
    current_cash: float,
    pending_value: float = 0.0,
    pending_symbols: set[str] | None = None,
    vix: float = 15.0,
    conviction: float = 1.0,
) -> tuple[bool, str]:
    state = reset_for_new_day(current_cash)
    pending_symbols = pending_symbols or set()

    if state.get("halted"):
        return False, "trading halted (daily/streak)"

    # Account-level DD halt — independent of daily/streak. Recovers automatically
    # once peak-to-current DD drops back under the size-cut threshold (10%).
    dd_pct = current_drawdown_pct()
    if dd_pct >= settings.dd_halt_pct:
        return False, (f"DD halt: account drawdown {dd_pct:.1f}% "
                       f"≥ DD_HALT_PCT {settings.dd_halt_pct:.0f}%")

    held = positions[positions["qty"].astype(float) > 0] if not positions.empty else positions
    if len(held) + len(pending_symbols) >= settings.max_positions:
        return False, f"max positions ({settings.max_positions}) reached (incl. pending)"

    if not held.empty and signal.symbol in held["code"].str.split(".").str[-1].tolist():
        return False, f"already holding {signal.symbol}"

    if signal.symbol in pending_symbols:
        return False, f"buy order for {signal.symbol} already pending"

    qty = calc_position_size(signal, vix=vix, conviction=conviction)
    if qty == 0:
        return False, "computed qty=0 (stop too tight, price too high, or conviction=0)"

    required_cash = qty * signal.price
    if required_cash > current_cash:
        return False, f"insufficient cash: need ${required_cash:.0f}, have ${current_cash:.0f}"

    # ----- Hard budget cap (ACCOUNT_USD) — never exceed user's allocated capital.
    # Committed = filled positions + pending buy orders (avoid double-spending).
    invested = 0.0
    if not held.empty:
        invested = float((held["qty"].astype(float) * held["cost_price"].astype(float)).sum())
    committed = invested + pending_value
    if committed + required_cash > settings.account_usd:
        return False, (f"budget cap ${settings.account_usd:.0f} would be exceeded "
                       f"(committed ${committed:.0f} + new ${required_cash:.0f})")

    drawdown = (state["starting_cash"] - current_cash) / state["starting_cash"] \
        if state["starting_cash"] else 0
    if drawdown >= settings.daily_drawdown_stop:
        state["halted"] = True
        _save_state(state)
        return False, f"daily drawdown {drawdown:.1%} ≥ {settings.daily_drawdown_stop:.0%}"

    return True, "ok"


def record_trade_close(realized_pnl: float, account_usd: float | None = None) -> None:
    """Race-safe R-M-W of PnL totals + loss streak via SQLite atomic_state.

    Also bumps `peak_equity` for the DD circuit breaker. We measure equity as
    starting_capital + realized_pnl_total — a slight underestimate vs. mark-
    to-market open positions, but stable across position open/close cycles
    and good enough for the DD breaker's 10/15% thresholds.
    """
    def _apply(current: dict) -> dict:
        day = current.get("day", str(date.today()))
        new_today = current.get("realized_pnl_today", 0.0) + realized_pnl
        new_total = current.get("realized_pnl_total", 0.0) + realized_pnl
        # account_usd is needed to compute equity baseline. Fall back to
        # settings if caller didn't pass it.
        base = account_usd if account_usd is not None else settings.account_usd
        current_equity = base + new_total
        peak = max(current.get("peak_equity", 0.0) or 0.0,
                   base,        # never report a peak below starting capital
                   current_equity)
        updates: dict = {
            "realized_pnl_today": new_today,
            "realized_pnl_total": new_total,
            "last_close_day": day,
            "peak_equity": peak,
        }
        if realized_pnl < 0 and current.get("last_close_day") != day:
            updates["loss_streak_days"] = current.get("loss_streak_days", 0) + 1
            if updates["loss_streak_days"] >= 3:
                updates["halted"] = True
        elif realized_pnl > 0:
            updates["loss_streak_days"] = 0
        return updates

    merged = db.atomic_state(_apply)
    # legacy mirror
    try:
        STATE_FILE.write_text(json.dumps(merged, indent=2, default=str))
    except Exception:
        pass
