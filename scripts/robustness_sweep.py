"""360-day-focused parameter robustness sweep.

The 180-day window cherry-picks the Sep'25-May'26 bull regime where everything
worked. The 360-day window includes Dec'24-Feb'25 hostile chop. If a parameter
improves the 360-day result, that's a REAL gain — not regime luck.

We sweep three dimensions independently then combine winners:
  1. Threshold (65, 68, 70, 72, 75, 78)
  2. Max position pct (0.33, 0.40, 0.45, 0.50)
  3. TP/SL ratios (6/3, 7/3.5, 8/3.5, 8/4, 10/5)
  4. Watchlist concentration: full 19 vs top-10 vs top-5

Result metric: 360-day $/day (regime-robust)
"""
from __future__ import annotations
import logging, sys, json
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache

logging.basicConfig(level=logging.WARNING, format="%(message)s")

import builtins as _builtins
_orig_print = _builtins.print

def print(*args, **kwargs):  # noqa: shadows builtin on purpose
    """Auto-flush prints so monitor sees progress live during long runs."""
    kwargs.setdefault("flush", True)
    _orig_print(*args, **kwargs)
from src.config import settings

TOP_10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
TOP_5 = ["SNDK", "INTC", "MU", "WDC", "LRCX"]

# Common base: current best combo settings (A+B+C from previous sweep)
def base_cfg(days=360, tickers=None):
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0,
        tickers=tickers or [],
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=0.40,
        max_hold_days=settings.max_hold_days,
        tp_atr_mult=7.0,
        sl_atr_mult=3.5,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct,
        dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False,
    )

print("Prefetching 360 days …")
cache = prefetch_data(base_cfg(days=360))
print(f"Done. {len(cache['per_ticker'])} tickers cached.\n")


def run_variant(cfg, label, narrow_tickers=None):
    if narrow_tickers:
        narrowed = {**cache, "per_ticker":
                    {k: v for k, v in cache["per_ticker"].items() if k in narrow_tickers}}
        result = simulate_with_cache(cfg, narrowed)
    else:
        result = simulate_with_cache(cfg, cache)
    m = result["metrics"]
    return {
        "label": label,
        "trades": m.get("total_trades", 0),
        "wr": m.get("win_rate_pct", 0),
        "pf": m.get("profit_factor", 0),
        "net": m.get("net_pnl_usd", 0),
        "daily": m.get("net_pnl_usd", 0) / cfg.days,
        "sortino": m.get("sortino_ratio", 0),
        "max_dd_pct": m.get("max_drawdown_pct", 0),
        "monthly": m.get("monthly_pnl", {}),
    }


# ---------------------- Dimension 1: threshold ----------------------
print("=== Dim 1: Threshold ===")
threshold_results = []
for thr in [65, 68, 70, 72, 75, 78]:
    cfg = replace(base_cfg(), threshold=float(thr))
    r = run_variant(cfg, f"thr={thr}")
    threshold_results.append(r)
    print(f"  thr={thr:>2}  trades={r['trades']:>3}  WR={r['wr']:>5.1f}  "
          f"PF={r['pf']:>4.2f}  ${r['daily']:>+7.2f}/day  Sortino={r['sortino']:>5.2f}  "
          f"DD={r['max_dd_pct']:>5.2f}%")

# ---------------------- Dimension 2: max position pct ----------------------
print("\n=== Dim 2: max_position_pct ===")
mpp_results = []
for mpp in [0.33, 0.40, 0.45, 0.50]:
    cfg = replace(base_cfg(), max_position_pct=mpp)
    r = run_variant(cfg, f"mpp={mpp}")
    mpp_results.append(r)
    print(f"  mpp={mpp:>4.2f}  trades={r['trades']:>3}  WR={r['wr']:>5.1f}  "
          f"PF={r['pf']:>4.2f}  ${r['daily']:>+7.2f}/day  Sortino={r['sortino']:>5.2f}  "
          f"DD={r['max_dd_pct']:>5.2f}%")

# ---------------------- Dimension 3: TP/SL ratio ----------------------
print("\n=== Dim 3: TP/SL ratio ===")
tpsl_results = []
for tp, sl in [(6, 3), (6, 3.5), (7, 3.5), (8, 3.5), (8, 4), (10, 5)]:
    cfg = replace(base_cfg(), tp_atr_mult=float(tp), sl_atr_mult=float(sl))
    r = run_variant(cfg, f"tp={tp}/sl={sl}")
    tpsl_results.append(r)
    print(f"  tp={tp:>2}/sl={sl:>3.1f}  trades={r['trades']:>3}  WR={r['wr']:>5.1f}  "
          f"PF={r['pf']:>4.2f}  ${r['daily']:>+7.2f}/day  Sortino={r['sortino']:>5.2f}  "
          f"DD={r['max_dd_pct']:>5.2f}%")

# ---------------------- Dimension 4: Watchlist concentration ----------------------
print("\n=== Dim 4: Watchlist concentration ===")
watch_results = []
for name, tickers in [("19 (full)", None), ("top-10", TOP_10), ("top-5", TOP_5)]:
    cfg = base_cfg()
    r = run_variant(cfg, name, narrow_tickers=tickers)
    watch_results.append(r)
    print(f"  {name:<12}  trades={r['trades']:>3}  WR={r['wr']:>5.1f}  "
          f"PF={r['pf']:>4.2f}  ${r['daily']:>+7.2f}/day  Sortino={r['sortino']:>5.2f}  "
          f"DD={r['max_dd_pct']:>5.2f}%")

# ---------------------- Best-combo across dimensions ----------------------
print("\n=== Combo: Pick best from each dimension ===")
best_thr = max(threshold_results, key=lambda r: r["daily"])["label"].split("=")[1]
best_mpp = max(mpp_results, key=lambda r: r["daily"])["label"].split("=")[1]
best_tpsl = max(tpsl_results, key=lambda r: r["daily"])["label"]
best_watch = max(watch_results, key=lambda r: r["daily"])["label"]
print(f"  Best threshold: {best_thr}")
print(f"  Best max_pos:   {best_mpp}")
print(f"  Best TP/SL:     {best_tpsl}")
print(f"  Best watchlist: {best_watch}")

# Now build the combined "best" config and run 360-day.
best_tp, best_sl = best_tpsl.split("/")
combo_cfg = replace(
    base_cfg(),
    threshold=float(best_thr),
    max_position_pct=float(best_mpp),
    tp_atr_mult=float(best_tp.split("=")[1]),
    sl_atr_mult=float(best_sl.split("=")[1]),
)
watch_tickers = None
if best_watch == "top-10":
    watch_tickers = TOP_10
elif best_watch == "top-5":
    watch_tickers = TOP_5
combo = run_variant(combo_cfg, "COMBO_360", narrow_tickers=watch_tickers)
print(f"\n  Best 360-day combo: ${combo['daily']:+.2f}/day  "
      f"Sortino {combo['sortino']}, MaxDD {combo['max_dd_pct']}%")
print(f"  Monthly:")
for m, v in combo["monthly"].items():
    print(f"    {m}  {v:+.2f}")

# Save full results
out = ROOT / "data" / "robustness_sweep.json"
out.write_text(json.dumps({
    "threshold": threshold_results,
    "max_position_pct": mpp_results,
    "tp_sl_ratio": tpsl_results,
    "watchlist": watch_results,
    "combo": combo,
}, indent=2, default=str))
print(f"\n  Saved → {out}")
