"""Final exhaustive sweep — try every dial that might break $25/day, honestly,
with the chronologically-trained ML model in place.
"""
from __future__ import annotations
import logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s | %(message)s")
from src.config import settings

cfg_base = BacktestConfig(
    days=180, timeframe="HOUR_1", threshold=70.0,
    tickers=[],
    account_usd=settings.account_usd,
    risk_per_trade=settings.risk_per_trade,
    max_position_pct=settings.max_position_pct,
    max_hold_days=settings.max_hold_days,
    tp_atr_mult=settings.tp_atr_mult,
    sl_atr_mult=settings.sl_atr_mult,
    max_gap_pct=settings.max_gap_pct,
    dd_size_cut_pct=settings.dd_size_cut_pct,
    dd_halt_pct=settings.dd_halt_pct,
)

print("Prefetching once …")
cache = prefetch_data(cfg_base)
print(f"Prefetched {len(cache['per_ticker'])} tickers.\n")

variants = [
    ("baseline (current .env)",      {}),
    ("ml_off",                       {"apply_ml_gate": False}),
    ("mr_off",                       {"apply_mr_strategy": False}),
    ("ml_and_mr_off",                {"apply_ml_gate": False, "apply_mr_strategy": False}),
    ("regime_off (no SPY filter)",   {"apply_regime_gate": False}),
    ("sl_cooldown_off",              {"sl_cooldown_hours": 0}),
    ("tp_8.0",                       {"tp_atr_mult": 8.0}),
    ("tp_6.0",                       {"tp_atr_mult": 6.0}),
    ("sl_3.5",                       {"sl_atr_mult": 3.5}),
    ("max_pos_pct_0.40",             {"max_position_pct": 0.40}),
    ("risk_0.07",                    {"risk_per_trade": 0.07}),
    ("threshold_68",                 {"threshold": 68.0}),
    ("threshold_72",                 {"threshold": 72.0}),
]

rows = []
for name, overrides in variants:
    cfg = replace(cfg_base, **overrides)
    r = simulate_with_cache(cfg, cache)
    m = r["metrics"]
    rows.append({
        "name": name,
        "trades": m.get("total_trades", 0),
        "wr": m.get("win_rate_pct", 0),
        "pf": m.get("profit_factor", 0),
        "net": m.get("net_pnl_usd", 0),
        "daily": m.get("net_pnl_usd", 0) / cfg.days,
        "sortino": m.get("sortino_ratio", 0),
        "max_dd_pct": m.get("max_drawdown_pct", 0),
    })

rows.sort(key=lambda r: -r["daily"])

print("\n" + "=" * 100)
print("  FINAL SWEEP — variant comparison sorted by $/day")
print("=" * 100)
print(f"  {'variant':<28} {'trades':>6} {'WR%':>5} {'PF':>5} "
      f"{'net $':>10} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
print("  " + "-" * 96)
for r in rows:
    marker = "  ⭐" if r["daily"] >= 25 else ""
    print(f"  {r['name']:<28} {r['trades']:>6} {r['wr']:>5.1f} {r['pf']:>5.2f} "
          f"{r['net']:>+10.2f} {r['daily']:>+8.2f} {r['sortino']:>+8.2f} "
          f"{r['max_dd_pct']:>7.2f}{marker}")
print("=" * 100)

best = rows[0]
print(f"\n  Top variant: {best['name']}  →  ${best['daily']:+.2f}/day  (Sortino {best['sortino']})")
