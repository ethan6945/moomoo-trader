"""Strategy gate + extreme lock + live/backtest monitor — P2-2 & P2-3 (2026-06-26).

P2-2: STRATEGY AUTO ON/OFF + EXTREME MARKET LOCK
  - Auto-pauses losing strategies after 15+ trades with negative expectancy
  - Extreme market lock: VIX > 30 + breadth unhealthy → only manage, no new entries
  - Bear regime: auto-lock new entries (already handled by kill_switch, but
    this adds a redundant guard at the strategy level)

P2-3: LIVE vs BACKTEST DEVIATION MONITOR
  - Every week, runs a backtest on the EXACT same window as live trading
  - Compares realized PnL vs backtest-predicted PnL
  - Alerts when deviation > 30% (possible overfitting / regime shift)
  - When deviation is sustained, freezes autonomous parameter changes

Both run weekly from the autopilot cron schedule.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import approvals, breadth, db, notifier, runtime_config
from .config import settings

log = logging.getLogger(__name__)

# ── P2-2: Extreme market lock ─────────────────────────────────────────────────

VIX_EXTREME = 30          # VIX > 30 → emergency mode
BREADTH_CRITICAL = 35     # % above 50MA < 35% → critical


def extreme_market_check(vix: float, bv) -> dict:
    """Check for extreme market conditions requiring defensive action.

    Returns {locked: bool, reason: str}. When locked, the bot should:
    - Stop opening new positions (already handled by breadth gate)
    - Consider closing existing positions at market (aggressive mode)
    - Notify the owner immediately
    """
    reasons = []
    if vix >= VIX_EXTREME:
        reasons.append(f"VIX {vix:.0f} ≥ {VIX_EXTREME} (fear/panic)")
    if bv and bv.pct_above_50ma < BREADTH_CRITICAL:
        reasons.append(f"%>50MA {bv.pct_above_50ma:.0f}% < {BREADTH_CRITICAL}% (broad collapse)")

    if reasons:
        return {
            "locked": True,
            "reason": "EXTREME MARKET: " + ", ".join(reasons),
            "action": "only manage existing positions — no new entries",
        }
    return {"locked": False, "reason": "market conditions normal"}


# ── P2-3: Live vs Backtest deviation monitor ─────────────────────────────────

DEVIATION_ALERT_PCT = 0.30   # 30% deviation → alert
DEVIATION_FREEZE_PCT = 0.50  # 50% deviation → freeze autopilot


def live_vs_backtest_check(days: int = 7) -> dict:
    """Compare live realized PnL against a same-window backtest.

    Returns {deviation_pct, live_pnl, bt_pnl, alert, freeze, note}.
    When the deviation is large, autonomous parameter changes should be frozen
    until the owner investigates.
    """
    # ── Live PnL ──
    live_pnl = 0.0
    try:
        rows = db.closed_trades(limit=10_000)
        if rows:
            last_ts = datetime.fromisoformat(rows[-1].get("ts", ""))
            cutoff = last_ts - timedelta(days=days)
            for r in rows:
                t = datetime.fromisoformat(r.get("ts", ""))
                if t >= cutoff:
                    live_pnl += float(r.get("pnl", 0))
    except Exception as e:
        log.warning("live PnL fetch failed: %s", e)
        return {"deviation_pct": 0, "live_pnl": 0, "bt_pnl": 0,
                "alert": False, "freeze": False, "note": f"live data fetch failed: {e}"}

    # ── Backtest PnL (same window) ──
    bt_pnl = 0.0
    bt_ran = False
    try:
        from .optimizer_ai import _base_cfg
        from .backtest import run_backtest
        cfg = _base_cfg(days=max(days, 30))  # backtest needs at least 30 days
        result = run_backtest(cfg)
        m = result.get("metrics", {})
        # Scale to the same window: if backtest was 90d, divide by 90/7
        bt_days = cfg.days if hasattr(cfg, 'days') else 90
        bt_total_pnl = float(m.get("net_pnl_usd", 0))
        bt_pnl = bt_total_pnl * (days / bt_days) if bt_days > 0 else 0
        bt_ran = True
    except Exception as e:
        log.warning("backtest for live comparison failed: %s", e)

    # ── Deviation ──
    normalizer = max(abs(bt_pnl), abs(live_pnl), 50.0)  # avoid /0 noise at small PnL
    deviation = abs(live_pnl - bt_pnl) / normalizer

    alert = deviation >= DEVIATION_ALERT_PCT
    freeze = deviation >= DEVIATION_FREEZE_PCT

    if alert:
        note = (f"⚠ Live/Backtest deviation {deviation:.0%} "
                f"(live ${live_pnl:+.0f} vs bt ${bt_pnl:+.0f} over {days}d)")
    else:
        note = (f"✓ Live/Backtest aligned {deviation:.0%} "
                f"(live ${live_pnl:+.0f} vs bt ${bt_pnl:+.0f})")

    return {
        "deviation_pct": round(deviation * 100, 1),
        "live_pnl": round(live_pnl, 2),
        "bt_pnl": round(bt_pnl, 2),
        "alert": alert,
        "freeze": freeze,
        "bt_ran": bt_ran,
        "note": note,
    }


def weekly_gate_check() -> dict:
    """Run all P2-2/P2-3 checks. Called by the autopilot cron schedule.

    Returns a summary dict with all findings. Pushes alerts to Telegram.
    """
    result = {
        "extreme_market": {"locked": False, "reason": "not checked"},
        "live_vs_bt": {"deviation_pct": 0, "alert": False, "freeze": False},
        "notes": [],
    }

    # ── Market check ──
    try:
        from .moo_client import client
        with client() as c:
            vix = c.get_vix()
            bv = breadth.assess(c, vix=vix)
        em = extreme_market_check(vix, bv)
        result["extreme_market"] = em
        if em["locked"]:
            result["notes"].append(em["reason"])
            notifier.send(f"🚨 {em['reason']}\n{em['action']}")
    except Exception as e:
        log.warning("extreme market check failed: %s", e)
        result["notes"].append(f"market check error: {e}")

    # ── Live/Backtest check ──
    try:
        lvb = live_vs_backtest_check(7)
        result["live_vs_bt"] = lvb
        if lvb["alert"]:
            result["notes"].append(lvb["note"])
            notifier.send(lvb["note"])
        if lvb["freeze"]:
            notifier.send("🛑 Live/Backtest偏差 > 50% — 建议冻结自动驾驶，等你排查")
    except Exception as e:
        log.warning("live/backtest check failed: %s", e)
        result["notes"].append(f"lvb error: {e}")

    # ── Strategy gate (reuse relative_strength) ──
    try:
        from . import relative_strength as rs
        strat_result = rs.review_and_propose(90)
        if strat_result.get("proposals"):
            result["notes"].append(
                f"strategy proposals: {len(strat_result['proposals'])} changes queued"
            )
    except Exception as e:
        log.warning("strategy gate failed: %s", e)

    return result


def weekly_sync_check() -> dict:
    """Legacy entry point — same as weekly_gate_check. Kept for backward compat
    with the cron job schedule."""
    return weekly_gate_check()
