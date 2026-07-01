"""Relative strength ranking + strategy weight allocation — P1-3 (2026-06-26).

TWO JOBS:

1. RELATIVE STRENGTH RANKING (IBD / CAN SLIM)
   Inside the universe, rank every ticker by its 20-day return vs the market.
   The scan loop only evaluates the top N — focusing capital on leaders, not
   laggards. This is the same principle every momentum-based fund uses: the
   strongest stocks stay strong.

2. STRATEGY WEIGHT DYNAMIC ALLOCATION
   Track per-strategy real-fill expectancy over a rolling window. Strategies
   with positive expectancy keep running; losing strategies get auto-paused
   (proposal → approval queue, never silently disabled — 铁律).

PROTOCOL: both functions only PROPOSE. Nothing changes behavior until the
owner approves via the GUI / CLI / Telegram approval flow.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import approvals, db, runtime_config
from .config import settings

log = logging.getLogger(__name__)

RS_LOOKBACK_DAYS = 20
MIN_TRADES_FOR_STRATEGY_ACTION = 15


# ═══════════════════════════════════════════════════════════════════════════════
# 1. RELATIVE STRENGTH RANKING
# ═══════════════════════════════════════════════════════════════════════════════

def rank(universe: list[str], client) -> list[str]:
    """Return universe tickers ranked by 20-day return, highest first.

    Degrades gracefully: tickers with fetch errors bubble to the bottom
    with score 0 instead of crashing the scan.
    """
    from moomoo import KLType

    scores: dict[str, float] = {}
    for sym in universe:
        try:
            df = client.get_kline(sym, bars=25, ktype=KLType.K_DAY)
            if df is None or len(df) < RS_LOOKBACK_DAYS + 1:
                scores[sym] = 0.0
                continue
            close = df["close"]
            past = float(close.iloc[-1 - RS_LOOKBACK_DAYS])
            now = float(close.iloc[-1])
            if past <= 0:
                scores[sym] = 0.0
            else:
                scores[sym] = (now / past - 1) * 100
        except Exception:
            scores[sym] = 0.0

    return sorted(scores, key=lambda s: scores[s], reverse=True)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. STRATEGY WEIGHT DYNAMIC ALLOCATION
# ═══════════════════════════════════════════════════════════════════════════════

# Strategy registry — maps the strategy identifier used in trade records to
# the runtime_config key that toggles it on/off.
STRATEGY_TOGGLES = {
    "trend":           None,              # always on (baseline)
    "momentum_break":  None,              # always on (momentum is core)
    "mean_revert":     "mr_enabled",
    "pattern":         "pattern_enabled",
}


def strategy_performance(lookback_days: int = 90) -> dict:
    """Analyse per-strategy expectancy from closed trades.

    Returns a dict keyed by strategy name with {n, pnl, expectancy, win_rate}.
    """
    rows = db.closed_trades(limit=10_000)
    if not rows:
        return {}

    # Filter to lookback window
    try:
        last_ts = datetime.fromisoformat(rows[-1].get("ts", ""))
    except (ValueError, TypeError):
        last_ts = datetime.now()
    cutoff = last_ts - timedelta(days=lookback_days)

    by_strat: dict = defaultdict(list)
    for r in rows:
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
        except (ValueError, TypeError):
            ts = datetime.now()
        if ts < cutoff:
            continue
        strat = r.get("strategy", "trend")
        by_strat[strat].append(r)

    result = {}
    for strat, rs in by_strat.items():
        pnl = sum(r["pnl"] for r in rs)
        n = len(rs)
        exp = pnl / n if n > 0 else 0
        wr = (sum(1 for r in rs if r["pnl"] > 0) / n * 100) if n > 0 else 0
        result[strat] = {
            "n": n, "pnl": round(pnl, 2),
            "expectancy": round(exp, 2),
            "win_rate": round(wr, 1),
        }

    return result


def review_and_propose(lookback_days: int = 90) -> dict:
    """Review strategy performance and propose disabling chronic losers.

    Returns {proposals: [...], summary: str}. Only proposes — nothing auto-applies.
    The owner approves via GUI/Telegram (铁律).
    """
    perf = strategy_performance(lookback_days)
    proposals = []
    summary_lines = []

    for strat, data in sorted(perf.items(), key=lambda kv: kv[1]["expectancy"]):
        toggle_key = STRATEGY_TOGGLES.get(strat)
        if toggle_key is None:
            continue  # core strategy, never disable

        # Check if currently enabled
        currently_on = getattr(settings, toggle_key, False)
        if not currently_on:
            continue  # already off, nothing to do

        n = data["n"]
        exp = data["expectancy"]
        if n >= MIN_TRADES_FOR_STRATEGY_ACTION and exp < 0:
            proposals.append({
                "kind": "strategy_disable",
                "strategy": strat,
                "detail": (f"strategy '{strat}': {n} trades, ${exp:.2f}/trade, "
                           f"{data['win_rate']:.0f}% win — net drag over {lookback_days}d"),
                "action": f"Pause '{strat}' strategy (set {toggle_key}=false)",
                "payload": {"key": toggle_key, "value": False},
                "reversible": True,
            })
            summary_lines.append(
                f"  ❌ {strat}: ${exp:.2f}/trade ({n} trades) → proposed PAUSE"
            )
        else:
            summary_lines.append(
                f"  ✓ {strat}: ${exp:.2f}/trade ({n} trades)"
            )

    # Re-enable proposals: if a paused strategy had good performance recently,
    # propose re-enabling. Look at the last 20 trades specifically.
    recent_perf = strategy_performance(20)
    for strat, toggle_key in STRATEGY_TOGGLES.items():
        if toggle_key is None:
            continue
        currently_on = getattr(settings, toggle_key, False)
        if currently_on:
            continue  # already on
        rp = recent_perf.get(strat, {})
        if rp.get("n", 0) < 10:
            continue
        if rp.get("expectancy", -1) > 0:
            proposals.append({
                "kind": "strategy_enable",
                "strategy": strat,
                "detail": (f"strategy '{strat}': {rp['n']} recent trades, "
                           f"${rp['expectancy']:.2f}/trade — recovery signal"),
                "action": f"Re-enable '{strat}' strategy (set {toggle_key}=true)",
                "payload": {"key": toggle_key, "value": True},
                "reversible": True,
            })
            summary_lines.append(
                f"  🔄 {strat}: ${rp['expectancy']:.2f}/trade ({rp['n']} recent) → proposed RE-ENABLE"
            )

    # Enqueue all proposals
    for p in proposals:
        approvals.enqueue(
            kind=p["kind"],
            detail=p["detail"],
            action=p["action"],
            payload=p["payload"],
        )

    return {
        "proposals": proposals,
        "summary": "\n".join(summary_lines) if summary_lines else "all strategies look healthy",
        "performance": perf,
    }
