"""leak_diagnose — Phase 2b, take 2: where do the losing months actually leak?

The regime hypothesis was REFUTED by regime_diagnose.py: ~97-98% of both the
wins AND the small losses live inside BULL; NEUTRAL is 8 tiny POSITIVE trades and
BEAR is already fully blocked. So the leak is not gateable on SPY regime.

This script re-cuts the SAME honest production trades two other ways to find a
real, no-leverage defense for the losing months:

  1. PnL BY EXIT REASON — is MAX_HOLD (the biggest bucket, trades that never hit
     TP or SL and time out) a net drag? If dead-money trades lose on balance, a
     tighter time-stop is a legitimate Phase-2b defense that cuts losers without
     touching the winners.

  2. MONTHLY PnL × EXIT COMPOSITION — for every losing month, what exits dominate
     it? If the red months are MAX_HOLD-heavy, that corroborates (1).

No engine change. Honest production path (_run_live_engine) at the Phase-2a base
(max_position_pct=0.40), same as regime_diagnose, so the two cuts are comparable.

Run:  .venv/bin/python3 scripts/leak_diagnose.py
"""
from __future__ import annotations
import json, logging, sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, _run_live_engine
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


def month_of(t) -> str:
    return str(t["exit_date"])[:7]


for days in [360, 180]:
    cfg = base(days, 0.40)
    cache = prefetch_data(cfg)
    res = _run_live_engine(cfg, cache, rich_metrics=False)
    trades = res["trades"]
    net_all = sum(t["pnl"] for t in trades) or 1.0

    print(f"\n{'='*72}\n  {days}-DAY · LEAK DIAGNOSIS  (mpp=0.40, honest production)\n{'='*72}")
    print(f"  total: {len(trades)} trades, net ${net_all:+,.0f}")

    # ---- (1) PnL by exit reason ----
    by_exit: dict[str, list] = defaultdict(list)
    for t in trades:
        by_exit[t["exit_reason"]].append(t["pnl"])
    print(f"\n  (1) PnL BY EXIT REASON")
    print(f"  {'exit':<11}{'trades':>7}{'WR%':>7}{'netPnL':>11}{'avg/trd':>10}{'share%':>8}")
    print("  " + "-"*54)
    for ex in sorted(by_exit, key=lambda e: -sum(by_exit[e])):
        pnls = by_exit[ex]
        n = len(pnls); net = sum(pnls)
        wr = sum(1 for p in pnls if p > 0) / n * 100
        print(f"  {ex:<11}{n:>7}{wr:>7.1f}{net:>+11.0f}{net/n:>+10.1f}{net/net_all*100:>+8.1f}")

    # ---- (2) monthly PnL + exit composition of losing months ----
    by_month: dict[str, list] = defaultdict(list)
    for t in trades:
        by_month[month_of(t)].append(t)
    print(f"\n  (2) MONTHLY PnL  (red months expanded by exit reason)")
    print(f"  {'month':<9}{'trades':>7}{'netPnL':>11}   exit composition (losers only shown for red)")
    print("  " + "-"*60)
    for m in sorted(by_month):
        ts = by_month[m]
        net = sum(t["pnl"] for t in ts)
        flag = "  RED" if net < 0 else ""
        comp = ""
        if net < 0:
            # exit-reason breakdown of the losers that sank the month
            losers = defaultdict(lambda: [0, 0.0])  # reason -> [count, pnl]
            for t in ts:
                if t["pnl"] < 0:
                    losers[t["exit_reason"]][0] += 1
                    losers[t["exit_reason"]][1] += t["pnl"]
            comp = "  ".join(f"{r}×{c[0]}({c[1]:+.0f})"
                             for r, c in sorted(losers.items(), key=lambda kv: kv[1][1]))
        print(f"  {m:<9}{len(ts):>7}{net:>+11.0f}{flag:<5}  {comp}")

print("\nDONE")
