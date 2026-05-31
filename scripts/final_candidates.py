"""Test 4 candidate configs on BOTH 180-day and 360-day windows.

Goal: pick the config that gives the best balance of bull-regime upside
(180-day) AND multi-regime survival (360-day)."""
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

def base(days, tickers=None):
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0,
        tickers=tickers or [],
        account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
        max_position_pct=0.40, max_hold_days=settings.max_hold_days,
        tp_atr_mult=7.0, sl_atr_mult=3.5,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False,
    )

# Four candidate configs to compare:
CANDIDATES = [
    ("CURRENT (.env)",     {}),
    ("HI_PNL (combo)",     {"max_position_pct": 0.50, "tp_atr_mult": 8.0,
                            "sl_atr_mult": 3.5, "tickers": TOP_10}),
    ("BALANCED (top5+mpp50)", {"max_position_pct": 0.50, "tickers": TOP_5}),
    ("STABLE (top5 only)", {"tickers": TOP_5}),
]

for days in [180, 360]:
    print(f"\n=== {days}-day window ===")
    print(f"{'config':<22} {'trades':>6} {'WR%':>5} {'PF':>5} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("-" * 70)
    cache = prefetch_data(base(days))
    for name, overrides in CANDIDATES:
        tickers = overrides.pop("tickers", None) if "tickers" in overrides else None
        cfg = replace(base(days), **overrides)
        if tickers:
            narrowed = {**cache, "per_ticker":
                        {k: v for k, v in cache["per_ticker"].items() if k in tickers}}
            r = simulate_with_cache(cfg, narrowed)
        else:
            r = simulate_with_cache(cfg, cache)
        if "tickers" not in overrides and tickers:
            overrides["tickers"] = tickers
        m = r["metrics"]
        print(f"{name:<22} {m.get('total_trades', 0):>6} "
              f"{m.get('win_rate_pct', 0):>5.1f} {m.get('profit_factor', 0):>5.2f} "
              f"{m.get('net_pnl_usd', 0)/days:>+8.2f} {m.get('sortino_ratio', 0):>+8.2f} "
              f"{m.get('max_drawdown_pct', 0):>7.2f}")
