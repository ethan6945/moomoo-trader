"""Phase 0 true baseline (2026-06-11): the honest engine with ALL realism flags
ON (scan-grid soft-stop exits, open-only entry fills, no same-day daily-close
lookahead, post-multiplier cap re-clamp, live trade-phase windows — wired
through _run_live_engine) across three windows on the current universe/.env.

Replaces the old $36.47/day headline, which was measured WITHOUT these
frictions. Never writes data/backtest_results.json.
"""
from __future__ import annotations
import json, logging, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
from src.backtest import BacktestConfig, prefetch_data, _run_live_engine  # noqa: E402
from src.config import settings  # noqa: E402

for days in (23, 90, 140):
    cfg = BacktestConfig(
        days=days, timeframe="HOUR_1",
        threshold=settings.entry_threshold,
        tickers=[],  # config/watchlist.json
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        use_scale_out=settings.use_scale_out,
        tp1_r=settings.tp1_r, tp2_r=settings.tp2_r,
    )
    cache = prefetch_data(cfg)
    if not cache.get("per_ticker"):
        print(f"BASELINE {days}d: prefetch failed (0 tickers)")
        continue
    m = _run_live_engine(cfg, cache, rich_metrics=True)["metrics"]
    print("BASELINE " + json.dumps({
        "days": days,
        "trades": m.get("total_trades"),
        "positions": m.get("total_positions"),
        "win_rate_pct": m.get("win_rate_pct"),
        "win_rate_per_position_pct": m.get("win_rate_per_position_pct"),
        "net_pnl_usd": m.get("net_pnl_usd"),
        "daily_pnl_usd": m.get("daily_pnl_usd"),
        "max_dd_realized_pct": m.get("max_drawdown_pct"),
        "max_dd_mtm_pct": m.get("max_dd_mtm_pct"),
        "profit_factor": m.get("profit_factor"),
        "exit_reasons": m.get("exit_reasons"),
    }))
