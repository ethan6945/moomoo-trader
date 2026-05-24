"""Honest risk-adjusted performance metrics + Monte Carlo bootstrap.

Replaces the per-trade Sharpe in backtest.py with a proper daily-equity Sharpe,
adds Sortino / Calmar / MAR / Profit Factor / Ulcer Index, and runs a 1000-shot
bootstrap to separate skill from luck. All inputs are list[Trade]-shaped dicts
(asdict of backtest.Trade), so this module is decoupled from the backtester.

References:
  • Sharpe — std of daily returns; classic
  • Sortino — std of downside returns only (penalises only losses)
  • Calmar — CAGR / |max drawdown %| (rolling 3y in literature; we use total)
  • MAR  — annualised return / |max drawdown %|, same family as Calmar
  • Ulcer Index — RMS of drawdown depths (continuous DD pain measure)
  • Profit Factor — gross profit / gross loss
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import numpy as np

# Annualisation factor — US equities trade ~252 days/year.
TRADING_DAYS = 252


# ---------- equity curve ----------

def build_daily_equity(trades: list[dict], starting_capital: float) -> tuple[list[str], list[float]]:
    """Aggregate trade PnL by exit_date into a daily equity curve.

    Trades are expected to have keys: exit_date (YYYY-MM-DD), pnl.
    Returns (dates, equity_values) where equity[0] = starting_capital.
    """
    if not trades:
        return [], [starting_capital]

    daily_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        d = t.get("exit_date") or ""
        if d:
            daily_pnl[d[:10]] += float(t.get("pnl", 0.0))

    dates = sorted(daily_pnl.keys())
    equity = [starting_capital]
    for d in dates:
        equity.append(equity[-1] + daily_pnl[d])
    return dates, equity


def daily_returns(equity: list[float]) -> list[float]:
    """Daily simple returns from an equity series. Skips zero-prev guards."""
    out: list[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev <= 0:
            continue
        out.append((equity[i] - prev) / prev)
    return out


# ---------- risk metrics ----------

def sharpe_ratio(returns: list[float], risk_free_daily: float = 0.0) -> float:
    """Annualised Sharpe from daily returns. Returns 0 if too few samples."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns) - risk_free_daily
    sigma = float(arr.std(ddof=1))
    if sigma == 0:
        return 0.0
    return float(arr.mean() / sigma * math.sqrt(TRADING_DAYS))


def sortino_ratio(returns: list[float], target_daily: float = 0.0) -> float:
    """Annualised Sortino — same as Sharpe but only downside std in denominator."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    downside = arr[arr < target_daily] - target_daily
    if len(downside) < 2:
        # All returns above target → undefined; report 9.99 as "infinite-ish"
        return 9.99 if arr.mean() > 0 else 0.0
    dd_std = float(downside.std(ddof=1))
    if dd_std == 0:
        return 0.0
    return float((arr.mean() - target_daily) / dd_std * math.sqrt(TRADING_DAYS))


def max_drawdown(equity: list[float]) -> tuple[float, float, int]:
    """Returns (max_dd_dollars, max_dd_pct, longest_underwater_days)."""
    if not equity:
        return 0.0, 0.0, 0
    peak = equity[0]
    max_dd_abs = 0.0
    max_dd_pct = 0.0
    longest_under = 0
    underwater_run = 0
    for v in equity:
        if v >= peak:
            peak = v
            underwater_run = 0
        else:
            underwater_run += 1
            longest_under = max(longest_under, underwater_run)
            dd_abs = peak - v
            dd_pct = dd_abs / peak * 100 if peak > 0 else 0.0
            max_dd_abs = max(max_dd_abs, dd_abs)
            max_dd_pct = max(max_dd_pct, dd_pct)
    return max_dd_abs, max_dd_pct, longest_under


def cagr(equity: list[float], n_days: int) -> float:
    """Compound annual growth rate from total equity change over n_days."""
    if not equity or n_days <= 0:
        return 0.0
    start, end = equity[0], equity[-1]
    if start <= 0 or end <= 0:
        return 0.0
    years = max(n_days / 365.0, 1 / 365.0)
    return (end / start) ** (1 / years) - 1


def calmar_ratio(equity: list[float], n_days: int) -> float:
    """CAGR / max_dd_pct. Higher = better."""
    _, dd_pct, _ = max_drawdown(equity)
    if dd_pct <= 0:
        return 9.99
    return cagr(equity, n_days) * 100 / dd_pct


def mar_ratio(equity: list[float], n_days: int) -> float:
    """Annualised simple return / max_dd_pct (Calmar variant)."""
    if not equity or n_days <= 0 or equity[0] <= 0:
        return 0.0
    total_ret = (equity[-1] - equity[0]) / equity[0]
    annualised = total_ret * (365.0 / max(n_days, 1))
    _, dd_pct, _ = max_drawdown(equity)
    if dd_pct <= 0:
        return 9.99
    return annualised * 100 / dd_pct


def ulcer_index(equity: list[float]) -> float:
    """RMS of drawdown depths (%). Lower = smoother, less pain."""
    if not equity:
        return 0.0
    peak = equity[0]
    sq_dd = []
    for v in equity:
        peak = max(peak, v)
        dd_pct = (peak - v) / peak * 100 if peak > 0 else 0
        sq_dd.append(dd_pct * dd_pct)
    if not sq_dd:
        return 0.0
    return math.sqrt(sum(sq_dd) / len(sq_dd))


def profit_factor(trades: list[dict]) -> float:
    """Gross profit / |gross loss|. >1 = profitable, >1.5 = healthy."""
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    if gl == 0:
        return 9.99 if gp > 0 else 0.0
    return gp / gl


def expectancy(trades: list[dict]) -> float:
    """Expected PnL per trade ($)."""
    return sum(t["pnl"] for t in trades) / len(trades) if trades else 0.0


def kelly_fraction(trades: list[dict]) -> float:
    """Kelly criterion as % of bankroll. NEGATIVE means strategy loses money.

    f* = W - (1-W)/R   where W=win rate, R=avg_win/avg_loss
    Clamp to [-0.25, 0.25] for sanity (full Kelly is suicidal in practice).
    """
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    if not wins or not losses:
        return 0.0
    w = len(wins) / len(trades)
    avg_w = sum(t["pnl"] for t in wins) / len(wins)
    avg_l = abs(sum(t["pnl"] for t in losses) / len(losses))
    if avg_l <= 0:
        return 0.0
    r = avg_w / avg_l
    f = w - (1 - w) / r
    return max(-0.25, min(0.25, f))


# ---------- Monte Carlo bootstrap ----------

def monte_carlo_bootstrap(
    trades: list[dict],
    starting_capital: float,
    n_sims: int = 1000,
    seed: int = 42,
) -> dict:
    """Bootstrap resample trades to estimate the distribution of outcomes.

    Tells you whether the historical edge survives reshuffling. If P5 of final
    equity is below starting_capital, you can lose money even on the historical
    sample — the strategy is fragile.

    Returns:
      p5_final, p50_final, p95_final  — percentile of ending equity
      p5_max_dd_pct, p50_max_dd_pct, p95_max_dd_pct
      prob_profitable    — fraction of paths ending > start
      prob_ruin_pct      — fraction of paths hitting -50% drawdown
      prob_3pct_dd       — fraction of paths breaching daily drawdown stop
    """
    if n_sims < 1:
        return {"note": "monte carlo skipped (n_sims=0)"}
    if len(trades) < 20:
        return {"note": f"only {len(trades)} trades — bootstrap unreliable, skipped"}

    rng = random.Random(seed)
    pnls = [t["pnl"] for t in trades]
    n_trades = len(pnls)

    final_equities = []
    max_dds = []
    n_profitable = 0
    n_ruined = 0
    for _ in range(n_sims):
        shuffled = [pnls[rng.randrange(n_trades)] for _ in range(n_trades)]
        eq = [starting_capital]
        for p in shuffled:
            eq.append(eq[-1] + p)
        _, dd_pct, _ = max_drawdown(eq)
        final_equities.append(eq[-1])
        max_dds.append(dd_pct)
        if eq[-1] > starting_capital:
            n_profitable += 1
        if dd_pct >= 50:
            n_ruined += 1

    final_arr = np.array(final_equities)
    dd_arr = np.array(max_dds)
    return {
        "n_simulations": n_sims,
        "p5_final": round(float(np.percentile(final_arr, 5)), 2),
        "p50_final": round(float(np.percentile(final_arr, 50)), 2),
        "p95_final": round(float(np.percentile(final_arr, 95)), 2),
        "p5_max_dd_pct": round(float(np.percentile(dd_arr, 5)), 2),
        "p50_max_dd_pct": round(float(np.percentile(dd_arr, 50)), 2),
        "p95_max_dd_pct": round(float(np.percentile(dd_arr, 95)), 2),
        "prob_profitable_pct": round(n_profitable / n_sims * 100, 1),
        "prob_ruin_pct": round(n_ruined / n_sims * 100, 1),
    }


# ---------- combined report ----------

def compute_full_metrics(
    trades: list[dict],
    starting_capital: float,
    n_days: int,
    n_sims: int = 1000,
) -> dict:
    """Single entry point — computes everything backtest.py needs.

    Returns a dict with all the metrics keyed by name. Backtest can merge
    with its own per-trade tallies.
    """
    if not trades:
        return {"total_trades": 0, "note": "no trades"}

    dates, equity = build_daily_equity(trades, starting_capital)
    rets = daily_returns(equity)
    dd_abs, dd_pct, dd_days = max_drawdown(equity)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]

    return {
        # Sample / breakdown
        "total_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_win_usd": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss_usd": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "expectancy_per_trade_usd": round(expectancy(trades), 2),
        "profit_factor": round(profit_factor(trades), 2),
        "kelly_fraction_pct": round(kelly_fraction(trades) * 100, 2),

        # Return
        "starting_capital_usd": round(starting_capital, 2),
        "final_equity_usd": round(equity[-1], 2),
        "net_pnl_usd": round(equity[-1] - starting_capital, 2),
        "total_return_pct": round((equity[-1] / starting_capital - 1) * 100, 2)
                            if starting_capital > 0 else 0,
        "cagr_pct": round(cagr(equity, n_days) * 100, 2),

        # Risk-adjusted (using daily equity — the honest way)
        "sharpe_ratio": round(sharpe_ratio(rets), 2),
        "sortino_ratio": round(sortino_ratio(rets), 2),
        "calmar_ratio": round(calmar_ratio(equity, n_days), 2),
        "mar_ratio": round(mar_ratio(equity, n_days), 2),
        "ulcer_index": round(ulcer_index(equity), 2),

        # Drawdown
        "max_drawdown_usd": round(dd_abs, 2),
        "max_drawdown_pct": round(dd_pct, 2),
        "max_drawdown_days": dd_days,

        # Monte Carlo (the bullshit detector)
        "monte_carlo": monte_carlo_bootstrap(trades, starting_capital, n_sims=n_sims),

        # Equity curve (for plotting)
        "equity_curve": [round(e, 2) for e in equity],
        "equity_dates": dates,
    }
