"""Combine the top single-variable winners into a few hybrid configs and see
if any breaks $30/day cleanly."""
from __future__ import annotations
import logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache

logging.basicConfig(level=logging.WARNING)
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

print("Prefetching …")
cache = prefetch_data(cfg_base)
print(f"Done.\n")

# Each combo stacks several single-variable winners.
combos = [
    ("baseline",                {}),
    # Winners from final sweep, individually:
    ("A: cap40",                {"max_position_pct": 0.40}),
    ("B: sl=3.5",               {"sl_atr_mult": 3.5}),
    ("C: no MR",                {"apply_mr_strategy": False}),
    ("D: tp=8.0",               {"tp_atr_mult": 8.0}),
    ("E: thr=72",               {"threshold": 72.0}),
    # Pairs:
    ("A+B (cap40 + sl3.5)",     {"max_position_pct": 0.40, "sl_atr_mult": 3.5}),
    ("A+C (cap40 + no MR)",     {"max_position_pct": 0.40, "apply_mr_strategy": False}),
    ("B+C (sl3.5 + no MR)",     {"sl_atr_mult": 3.5, "apply_mr_strategy": False}),
    ("A+B+C (the trio)",        {"max_position_pct": 0.40, "sl_atr_mult": 3.5,
                                 "apply_mr_strategy": False}),
    # Quads:
    ("A+B+C+D (+ tp8)",         {"max_position_pct": 0.40, "sl_atr_mult": 3.5,
                                 "apply_mr_strategy": False, "tp_atr_mult": 8.0}),
    ("A+B+C+E (+ thr72)",       {"max_position_pct": 0.40, "sl_atr_mult": 3.5,
                                 "apply_mr_strategy": False, "threshold": 72.0}),
    # Full stack:
    ("ALL (A+B+C+D+E)",         {"max_position_pct": 0.40, "sl_atr_mult": 3.5,
                                 "apply_mr_strategy": False, "tp_atr_mult": 8.0,
                                 "threshold": 72.0}),
]

rows = []
for name, overrides in combos:
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
        "monthly": m.get("monthly_pnl", {}),
    })

rows.sort(key=lambda r: -r["daily"])

print("\n" + "=" * 100)
print("  COMBO SWEEP — stacking the top single-variable winners")
print("=" * 100)
print(f"  {'combo':<32} {'trades':>6} {'WR%':>5} {'PF':>5} "
      f"{'net $':>10} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
print("  " + "-" * 96)
for r in rows:
    marker = "  🎯" if r["daily"] >= 30 else ("  ⭐" if r["daily"] >= 25 else "")
    print(f"  {r['name']:<32} {r['trades']:>6} {r['wr']:>5.1f} {r['pf']:>5.2f} "
          f"{r['net']:>+10.2f} {r['daily']:>+8.2f} {r['sortino']:>+8.2f} "
          f"{r['max_dd_pct']:>7.2f}{marker}")

best = rows[0]
print("\n=" * 50)
print(f"  Best: {best['name']}  →  ${best['daily']:+.2f}/day  Sortino {best['sortino']}, MaxDD {best['max_dd_pct']}%")
print("  Monthly:")
for m, v in best["monthly"].items():
    print(f"    {m}  {v:+.2f}")
