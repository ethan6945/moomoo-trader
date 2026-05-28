"""Compare Optuna Trial #1 (current .env) vs Trial #2 (more robust worst-fold)
on the same 180-day data + same .env feature flags.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache  # noqa: E402


VARIANTS = [
    # Trial #1 — the one currently in .env (best by composite Sortino).
    ("Trial_1_current", dict(threshold=70.0, tp_atr_mult=7.0, sl_atr_mult=2.75,
                             max_gap_pct=2.5)),
    # Trial #2 — slightly lower mean Sortino but worst-fold = 10.26 (vs 3.91).
    ("Trial_2_robust",  dict(threshold=67.0, tp_atr_mult=7.0, sl_atr_mult=3.5,
                             max_gap_pct=3.0)),
    # Trial #5 — broadest signal capture (143 trades) with worst-fold 7.38
    ("Trial_5_volume",  dict(threshold=69.0, tp_atr_mult=7.0, sl_atr_mult=2.75,
                             max_gap_pct=2.5)),
]


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")
    from src.config import settings
    cfg_base = BacktestConfig(
        days=180,
        timeframe="HOUR_1",
        threshold=70.0,   # overridden per variant
        tickers=[],
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
    )
    print("Prefetching …")
    cache = prefetch_data(cfg_base)
    print(f"Prefetched {len(cache['per_ticker'])} tickers.")

    rows = []
    for name, overrides in VARIANTS:
        cfg = replace(cfg_base, **overrides)
        result = simulate_with_cache(cfg, cache)
        m = result["metrics"]
        rows.append({
            "name": name,
            "trades": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0),
            "daily": m.get("net_pnl_usd", 0) / 180,
            "sortino": m.get("sortino_ratio", 0),
            "max_dd_pct": m.get("max_drawdown_pct", 0),
            "monthly": m.get("monthly_pnl", {}),
        })

    print("\n" + "=" * 100)
    print("  OPTUNA TRIAL COMPARISON — same .env feature flags, different exit params")
    print("=" * 100)
    print(f"  {'variant':<20} {'trades':>6} {'WR%':>5} {'PF':>5} "
          f"{'net $':>10} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("  " + "-" * 96)
    for r in rows:
        print(f"  {r['name']:<20} {r['trades']:>6} {r['wr']:>5.1f} "
              f"{r['pf']:>5.2f} {r['net']:>+10.2f} {r['daily']:>+8.2f} "
              f"{r['sortino']:>+8.2f} {r['max_dd_pct']:>7.2f}")
    print("=" * 100)

    print("\nMonthly PnL by variant:")
    months = sorted({m for r in rows for m in r['monthly'].keys()})
    print(f"  {'month':<10}", end="")
    for r in rows:
        print(f"  {r['name']:>20}", end="")
    print()
    for mn in months:
        print(f"  {mn:<10}", end="")
        for r in rows:
            v = r['monthly'].get(mn, 0.0)
            print(f"  {v:>+20.2f}", end="")
        print()


if __name__ == "__main__":
    main()
