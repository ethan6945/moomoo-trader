"""Self-adjusting risk_per_trade multiplier based on rolling Sortino.

Why this exists:
  RISK_PER_TRADE in .env is a static 2%. But "2%" should mean different things
  when the bot is on a hot streak vs. when it's bleeding. This module reads
  the last N closed trades, computes their daily-equity Sortino, and returns
  a multiplier (0.5–1.25) that `risk_manager.calc_position_size` applies on
  top of the configured base risk.

  Result: when the system is doing well, sizes accelerate (capped). When it
  is cooling off, sizes shrink BEFORE the hard DD breaker has to fire. That
  is a much smoother way to manage risk than a step-function at -10% DD.

How it's used (live):
  - risk_manager.calc_position_size() calls compute_multiplier()
  - The multiplier composes with the loss-streak multiplier and the
    account-level DD multiplier — three independent layers protect risk

Backtest:
  - Disabled in backtest (see ADAPTIVE_SIZING_IN_BACKTEST below). The
    backtester already uses cfg.risk_per_trade as-is; auto-tuning over
    historical data introduces survivorship bias.

Bands (chosen conservatively — half-Kelly intuition):
   Sortino > 5    → 1.25× (hot streak, accelerate)
   Sortino 2–5    → 1.00× (baseline)
   Sortino 0–2    → 0.75× (cooling, slow down)
   Sortino ≤ 0    → 0.50× (protect mode)
   <10 trades     → 1.00× (warmup — not enough signal yet)
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict

from . import db

log = logging.getLogger(__name__)

LOOKBACK_TRADES = 30          # how many recent closed trades to consider
WARMUP_TRADES = 10            # below this count we don't adjust


def _recent_trades(n: int) -> list[dict]:
    """Pull the most recent `n` closed trades in chronological order.

    `db.closed_trades` returns oldest-first up to a hard limit. We fetch a
    big window (cheap, indexed scan) and tail-slice to get true recents.
    """
    rows = db.closed_trades(limit=10_000)
    return rows[-n:] if len(rows) > n else rows


def _daily_equity_returns(trades: list[dict]) -> list[float]:
    """Convert per-trade PnL into daily fractional returns of equity.

    Starting equity is normalised to 1.0 — we only need return shapes for
    the Sortino calculation. Trades without an exit date are skipped (open
    or malformed rows).
    """
    if not trades:
        return []
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        d = (t.get("ts") or t.get("opened_at") or "")[:10]
        if not d:
            continue
        daily[d] += float(t.get("pnl", 0.0))

    # Normalise PnL into a fractional daily return against the owner's current
    # allocated budget (dynamic — was a hardcoded $4500). Lazy import avoids a
    # circular dependency with risk_manager (which imports this module).
    try:
        from .risk_manager import budget_usd
        base_capital = budget_usd() or 4500.0
    except Exception:
        base_capital = 4500.0

    equity = 1.0
    rets: list[float] = []
    last_equity = equity
    for d in sorted(daily.keys()):
        ret = daily[d] / base_capital
        equity *= (1 + ret)
        rets.append(ret)
        last_equity = equity
    return rets


def _annualised_sortino(rets: list[float]) -> float:
    """Annualised Sortino from daily fractional returns."""
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    downside = [r for r in rets if r < 0]
    if len(downside) < 2:
        # No real downside days — Sortino is "great" but capped.
        return 9.99 if mean > 0 else 0.0
    dd_var = sum(r * r for r in downside) / len(downside)
    dd_std = math.sqrt(dd_var)
    if dd_std == 0:
        return 0.0
    return mean / dd_std * math.sqrt(252)


# ---------- public API ----------

def recent_sortino() -> float:
    """Sortino of the last LOOKBACK_TRADES closed trades. 0 when warming up."""
    trades = _recent_trades(LOOKBACK_TRADES)
    if len(trades) < WARMUP_TRADES:
        return 0.0
    rets = _daily_equity_returns(trades)
    return _annualised_sortino(rets)


def compute_multiplier() -> tuple[float, str]:
    """Return (multiplier, human reason). Multiplier composes with base risk.

    Always returns a finite multiplier in [0.5, 1.25]. The reason string is
    safe for Telegram / log inclusion.
    """
    trades = _recent_trades(LOOKBACK_TRADES)
    n = len(trades)
    if n < WARMUP_TRADES:
        return 1.0, f"warmup: {n}/{WARMUP_TRADES} trades, baseline 1.00×"

    rets = _daily_equity_returns(trades)
    sortino = _annualised_sortino(rets)

    if sortino > 5:
        return 1.25, f"hot streak Sortino={sortino:.2f} → 1.25×"
    if sortino > 2:
        return 1.0, f"baseline Sortino={sortino:.2f} → 1.00×"
    if sortino > 0:
        return 0.75, f"cooling Sortino={sortino:.2f} → 0.75×"
    return 0.5, f"protect mode Sortino={sortino:.2f} → 0.50×"
