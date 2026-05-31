"""HI_PNL v2 — combine scale-out 3R/5R with sweeps on other dimensions.

Last round we found scale-out 3R/5R adds +$0.43/day on 360d and +$2.18 on 180d
vs the HI_PNL baseline. Now we sweep around that new baseline to see if other
dimensions still have room to push higher.
"""
from __future__ import annotations
import logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache

logging.basicConfig(level=logging.WARNING, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

from src.config import settings

TOP_10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
TOP_5 = ["SNDK", "INTC", "MU", "WDC", "LRCX"]

# New baseline = HI_PNL combo + scale-out 3R/5R.
def base(days):
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0,
        tickers=TOP_10,
        account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
        max_position_pct=0.50, max_hold_days=settings.max_hold_days,
        tp_atr_mult=8.0, sl_atr_mult=3.5,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False,
        use_scale_out=True, tp1_r=3.0, tp2_r=5.0,
    )

def run(cfg, name, narrow=None):
    cache_local = narrowed if narrow else cache
    if narrow:
        cache_local = {**cache, "per_ticker":
                       {k: v for k, v in cache["per_ticker"].items() if k in narrow}}
    r = simulate_with_cache(cfg, cache_local)
    m = r["metrics"]
    return {
        "name": name,
        "trades": m.get("total_trades", 0),
        "wr": m.get("win_rate_pct", 0),
        "pf": m.get("profit_factor", 0),
        "net": m.get("net_pnl_usd", 0),
        "daily": m.get("net_pnl_usd", 0) / cfg.days,
        "sortino": m.get("sortino_ratio", 0),
        "dd": m.get("max_drawdown_pct", 0),
    }

for days in [180, 360]:
    print(f"\n=== {days}-day window (NEW base = HI_PNL + scale-out 3R/5R) ===")
    cache = prefetch_data(base(days))
    narrowed = {**cache, "per_ticker":
                {k: v for k, v in cache["per_ticker"].items() if k in TOP_10}}
    rows = []

    # Dim 1: max_position_pct — push higher
    for mpp in [0.45, 0.50, 0.55, 0.60, 0.65]:
        cfg = replace(base(days), max_position_pct=mpp)
        rows.append(run(cfg, f"mpp={mpp}"))
    # Dim 2: risk_per_trade
    for rpt in [0.04, 0.05, 0.06, 0.07, 0.08]:
        cfg = replace(base(days), risk_per_trade=rpt)
        rows.append(run(cfg, f"rpt={rpt}"))
    # Dim 3: sl_atr_mult variations
    for sl in [3.0, 3.5, 4.0, 4.5]:
        cfg = replace(base(days), sl_atr_mult=sl)
        rows.append(run(cfg, f"sl={sl}"))
    # Dim 4: tp1/tp2 R-multiples
    for tp1, tp2 in [(2.5, 4.5), (3.0, 5.0), (3.0, 6.0), (3.5, 5.5), (3.5, 6.0)]:
        cfg = replace(base(days), tp1_r=tp1, tp2_r=tp2)
        rows.append(run(cfg, f"tp1/tp2={tp1}/{tp2}"))
    # Dim 5: tickers
    rows.append(run(replace(base(days)), "top-10", narrow=TOP_10))
    rows.append(run(replace(base(days)), "top-5", narrow=TOP_5))

    rows.sort(key=lambda r: -r["daily"])
    print(f"{'variant':<22} {'trades':>6} {'WR%':>5} {'PF':>5} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("-" * 80)
    for r in rows:
        marker = "  🎯" if r["daily"] >= base(days).days and r["daily"] >= 25 else \
                 ("  ⭐" if r["daily"] >= 15 else "")
        print(f"{r['name']:<22} {r['trades']:>6} {r['wr']:>5.1f} {r['pf']:>5.2f} "
              f"{r['daily']:>+8.2f} {r['sortino']:>+8.2f} {r['dd']:>7.2f}{marker}")
