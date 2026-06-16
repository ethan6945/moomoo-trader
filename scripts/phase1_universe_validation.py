"""Phase 1 validation (2026-06-11): walk-forward dynamic universe vs pinned-10.

Honest engine, ALL Phase 0 realism flags ON (via _run_live_engine), entries
gated to the week's top-N of the 72-name liquidity pool by 6-1 momentum —
recomputed point-in-time each week. Reference (Phase 0 pinned-10 baseline):
90d $17.9/day · 140d $11.5/day · recent 23d −$3.4/day.

Also sweeps top_n 10/15/20 at 140d (cache reused) — we judge the PLATEAU, not
the peak. Never writes data/backtest_results.json.
"""
from __future__ import annotations
import json, logging, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
from src.backtest import BacktestConfig, prefetch_data, _run_live_engine  # noqa: E402
from src.config import settings  # noqa: E402
from src.universe import load_pool  # noqa: E402

POOL = load_pool()


def make_cfg(days: int, top_n: int) -> BacktestConfig:
    return BacktestConfig(
        days=days, timeframe="HOUR_1",
        threshold=settings.entry_threshold,
        tickers=POOL,
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        use_scale_out=settings.use_scale_out,
        tp1_r=settings.tp1_r, tp2_r=settings.tp2_r,
        apply_dynamic_universe=True,
        universe_top_n=top_n,
    )


def run(cfg: BacktestConfig, cache: dict, tag: str) -> None:
    m = _run_live_engine(cfg, cache, rich_metrics=True)["metrics"]
    print("PHASE1 " + json.dumps({
        "tag": tag, "days": cfg.days, "top_n": cfg.universe_top_n,
        "trades": m.get("total_trades"),
        "win_rate_pct": m.get("win_rate_pct"),
        "net_pnl_usd": m.get("net_pnl_usd"),
        "daily_pnl_usd": m.get("daily_pnl_usd"),
        "max_dd_realized_pct": m.get("max_drawdown_pct"),
        "max_dd_mtm_pct": m.get("max_dd_mtm_pct"),
        "profit_factor": m.get("profit_factor"),
        "exit_reasons": m.get("exit_reasons"),
    }))


cache140 = None
for days in (140, 90, 23):
    cfg = make_cfg(days, 15)
    cache = prefetch_data(cfg)
    if not cache.get("per_ticker"):
        print(f"PHASE1 {days}d: prefetch failed (0 tickers)")
        continue
    if days == 140:
        cache140 = cache
    run(cfg, cache, f"dyn{days}")

if cache140 is not None:
    for tn in (10, 20):
        run(make_cfg(140, tn), cache140, f"sweep_topn{tn}")
