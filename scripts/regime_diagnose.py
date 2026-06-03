"""regime_diagnose — where does the money actually come from / leak, by regime?

Phase 2b hypothesis: the losing months are NEUTRAL-regime chop (SPY above its
200-SMA but below the 50-SMA, or vice versa). The engine blocks BEAR and trades
BULL normally, but NEUTRAL trades UNGUARDED at full threshold + full size unless
VIX happens to be high. This script proves/refutes that by bucketing every
closed trade's realized PnL by the SPY regime on its entry day.

No engine change. Runs the honest production path (_run_live_engine) at the new
Phase-2a base (max_position_pct=0.40), then re-derives each trade's entry-day
regime exactly the way the engine does (regime.assess on SPY history ≤ entry).

Run:  .venv/bin/python3 scripts/regime_diagnose.py
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, _run_live_engine
from src import regime as regime_mod
from src.config import settings

logging.basicConfig(level=logging.ERROR, format="%(message)s")
TICKERS = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]


def base(days, mpp):
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=TICKERS, account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade, max_position_pct=mpp,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
    )


def regime_on(spy_daily: pd.DataFrame, entry_date: str) -> str:
    """Replicate the engine's lookup: assess SPY history up to the entry day."""
    ts = pd.Timestamp(entry_date)
    s_until = spy_daily.loc[spy_daily.index <= ts].tail(250)
    if len(s_until) < 200:
        return "NEUTRAL"
    return regime_mod.assess(s_until).label


for days in [360, 180]:
    cfg = base(days, 0.40)
    cache = prefetch_data(cfg)
    res = _run_live_engine(cfg, cache, rich_metrics=False)
    trades = res["trades"]
    spy = cache["spy_daily"]

    buckets: dict[str, list] = {"BULL": [], "NEUTRAL": [], "BEAR": []}
    for t in trades:
        lab = regime_on(spy, t["entry_date"])
        buckets.setdefault(lab, []).append(t["pnl"])

    print(f"\n{'='*72}\n  {days}-DAY · PnL BY ENTRY-DAY REGIME  (mpp=0.40, honest production)\n{'='*72}")
    print(f"  total: {len(trades)} trades, net ${sum(t['pnl'] for t in trades):+,.0f}")
    print(f"  {'regime':<9}{'trades':>7}{'WR%':>7}{'netPnL':>11}{'avg/trd':>10}{'share%':>8}")
    print("  " + "-"*52)
    net_all = sum(t["pnl"] for t in trades) or 1.0
    for lab in ("BULL", "NEUTRAL", "BEAR"):
        pnls = buckets.get(lab, [])
        if not pnls:
            print(f"  {lab:<9}{0:>7}{'—':>7}{0:>11.0f}{'—':>10}{'—':>8}")
            continue
        n = len(pnls)
        wr = sum(1 for p in pnls if p > 0) / n * 100
        net = sum(pnls)
        print(f"  {lab:<9}{n:>7}{wr:>7.1f}{net:>+11.0f}{net/n:>+10.1f}{net/net_all*100:>+8.1f}")

print("\nDONE")
