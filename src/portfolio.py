"""Portfolio-level risk gates + closed-trade R-multiple log.

Portfolio heat = Σ (entry − stop) × qty across all open trades. We cap this
at PORTFOLIO_HEAT_PCT of ACCOUNT_USD (default 8%) — even if every single
trade hits its stop, total drawdown is bounded to that %.

Every close is appended to data/trades.jsonl with R-multiple = realised
PnL / initial risk. After a few dozen trades this tells you whether you
actually have an edge (avg R > 0.5 is decent, > 1.0 is excellent).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytz

from . import db
from .config import settings
from .indicators import Signal

log = logging.getLogger(__name__)

OPEN_TRADES_FILE = settings.root / "data" / "open_trades.json"
TRADE_LOG_FILE = settings.root / "data" / "trades.jsonl"   # legacy mirror
NY = pytz.timezone("America/New_York")

PORTFOLIO_HEAT_PCT = 0.08   # 8% account cap on total open risk


def current_open_risk() -> float:
    """Σ of (entry - stop) × qty across all tracked trades — from SQLite."""
    total = 0.0
    for t in db.load_open_trades().values():
        try:
            risk = (t["entry_price"] - t["stop_loss"]) * t["qty"]
            if risk > 0:
                total += risk
        except (KeyError, ValueError, TypeError):
            pass
    return total


def heat_check(signal: Signal, qty: int) -> tuple[bool, str]:
    """Block entry if portfolio heat would exceed cap."""
    if qty <= 0:
        return False, "qty=0"
    new_risk = (signal.price - signal.stop_loss) * qty
    if new_risk <= 0:
        return True, "no positive risk"
    open_risk = current_open_risk()
    cap = settings.account_usd * PORTFOLIO_HEAT_PCT
    total = open_risk + new_risk
    if total > cap:
        return False, (
            f"portfolio heat ${total:.0f} > cap ${cap:.0f} "
            f"(open ${open_risk:.0f} + new ${new_risk:.0f})"
        )
    return True, f"heat OK ${total:.0f}/${cap:.0f}"


def record_close(
    symbol: str,
    qty: int,
    entry: float,
    stop: float,
    exit_price: float,
    exit_reason: str,
    opened_at: str = "",
    mfe_pct: float | None = None,
    mae_pct: float | None = None,
    ml_proba_entry: float | None = None,
    strategy: str = "trend",
) -> dict:
    """Append closed trade with R-multiple, MFE/MAE, ML proba, strategy tag."""
    pnl = (exit_price - entry) * qty
    pnl_pct = (exit_price - entry) / entry * 100 if entry > 0 else 0.0
    r_unit = entry - stop
    r_multiple = (exit_price - entry) / r_unit if r_unit > 0 else 0.0
    row = {
        "ts": datetime.now(NY).isoformat(),
        "symbol": symbol,
        "qty": qty,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "exit": round(exit_price, 2),
        "exit_reason": exit_reason,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "r_multiple": round(r_multiple, 2),
        "opened_at": opened_at,
        "mfe_pct": round(mfe_pct, 2) if mfe_pct is not None else None,
        "mae_pct": round(mae_pct, 2) if mae_pct is not None else None,
        "ml_proba_entry": round(ml_proba_entry, 3) if ml_proba_entry is not None else None,
        "strategy": strategy,
    }
    # Persist to SQLite (primary) + legacy JSONL mirror (best-effort).
    try:
        db.closed_trade_insert(row)
    except Exception as e:
        log.warning("closed_trade SQLite insert failed: %s", e)
    try:
        TRADE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        log.warning("legacy trade log write failed: %s", e)
    # Recompute ML calibration (cheap — full scan of closed_trades).
    try:
        from .ml.calibration import compute as _calib_compute
        _calib_compute()
    except Exception as e:
        log.debug("calibration recompute skipped: %s", e)
    return row


def trade_stats(last_n: int = 50) -> dict:
    """Quick summary of recent closed trades — now reads from SQLite."""
    rows = db.closed_trades(last_n)
    if not rows:
        return {"count": 0}
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    avg_r = sum(r["r_multiple"] for r in rows) / len(rows)

    # Per-strategy breakdown (helps user see if trend vs mean-revert is earning).
    by_strategy: dict = {}
    for r in rows:
        s = r.get("strategy") or "trend"
        agg = by_strategy.setdefault(s, {"n": 0, "wins": 0, "pnl": 0.0, "avg_r": 0.0})
        agg["n"] += 1
        if r["pnl"] > 0:
            agg["wins"] += 1
        agg["pnl"] = round(agg["pnl"] + r["pnl"], 2)
        agg["avg_r"] = round(agg["avg_r"] + r["r_multiple"], 2)
    for s, agg in by_strategy.items():
        if agg["n"] > 0:
            agg["avg_r"] = round(agg["avg_r"] / agg["n"], 2)
            agg["win_rate"] = round(agg["wins"] / agg["n"] * 100, 1)

    # MFE/MAE summary (only over rows that have it; older v1 rows return None)
    mfe_rows = [r for r in rows if r.get("mfe_pct") is not None]
    avg_mfe = round(sum(r["mfe_pct"] for r in mfe_rows) / len(mfe_rows), 2) if mfe_rows else None
    avg_mae = round(sum(r["mae_pct"] for r in mfe_rows) / len(mfe_rows), 2) if mfe_rows else None

    return {
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "avg_r_multiple": round(avg_r, 2),
        "total_pnl": round(sum(r["pnl"] for r in rows), 2),
        "best_r": round(max((r["r_multiple"] for r in rows), default=0), 2),
        "worst_r": round(min((r["r_multiple"] for r in rows), default=0), 2),
        "by_strategy": by_strategy,
        "avg_mfe_pct": avg_mfe,
        "avg_mae_pct": avg_mae,
    }
