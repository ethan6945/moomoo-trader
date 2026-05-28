"""Verify the MAX_HOLD_DAYS=7 change against the W3 baseline.

Two configs only:
  • W3 with max_hold_days=5  (current backtest reference)
  • W3 with max_hold_days=7  (the change just made)
"""
from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache  # noqa: E402

WINNERS = [
    "SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC",
    "SWKS", "PANW", "MCHP", "HPE", "HPQ", "FTNT", "DELL",
    "TSLA", "NVDA", "AAPL", "GOOGL", "MSFT",
]

W3 = dict(
    tp_atr_mult=6.0,
    sl_atr_mult=3.25,
    max_position_pct=0.33,
    risk_per_trade=0.05,
    tickers=WINNERS,
)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")
    from src.config import settings
    cfg_base = BacktestConfig(
        days=180,
        timeframe="HOUR_1",
        threshold=60.0,
        tickers=[],
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
    )

    print("Prefetching once for both runs …")
    cache = prefetch_data(cfg_base)
    print(f"Prefetched {len(cache['per_ticker'])} tickers.\n")

    rows = []
    for label, hold in [("W3_hold5", 5), ("W3_hold7", 7)]:
        cfg = replace(cfg_base, **W3, max_hold_days=hold)
        narrowed = {**cache, "per_ticker":
                    {k: v for k, v in cache["per_ticker"].items()
                     if k in WINNERS}}
        result = simulate_with_cache(cfg, narrowed)
        m = result["metrics"]
        rows.append({
            "label": label,
            "hold": hold,
            "trades": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0),
            "daily": m.get("net_pnl_usd", 0) / 180,
            "sortino": m.get("sortino_ratio", 0),
            "max_dd_pct": m.get("max_drawdown_pct", 0),
            "exit_reasons": m.get("exit_reasons", {}),
            "monthly": m.get("monthly_pnl", {}),
        })

    print("\n" + "=" * 90)
    print("  MAX_HOLD_DAYS  5  vs  7  (everything else = W3)")
    print("=" * 90)
    print(f"  {'cfg':<10} {'hold':>4} {'trades':>6} {'WR%':>5} "
          f"{'PF':>5} {'net $':>10} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("  " + "-" * 86)
    for r in rows:
        print(f"  {r['label']:<10} {r['hold']:>4} {r['trades']:>6} "
              f"{r['wr']:>5.1f} {r['pf']:>5.2f} {r['net']:>+10.2f} "
              f"{r['daily']:>+8.2f} {r['sortino']:>+8.2f} {r['max_dd_pct']:>7.2f}")

    delta = rows[1]["daily"] - rows[0]["daily"]
    print(f"\n  Δ $/day  : ${delta:+.2f}")
    print(f"  Δ exits  : "
          f"hold5={rows[0]['exit_reasons']}  hold7={rows[1]['exit_reasons']}")

    print("\n  Monthly comparison:")
    months = sorted(set(rows[0]['monthly']) | set(rows[1]['monthly']))
    for m in months:
        p5 = rows[0]['monthly'].get(m, 0.0)
        p7 = rows[1]['monthly'].get(m, 0.0)
        print(f"    {m}   hold5 ${p5:+8.2f}   hold7 ${p7:+8.2f}   "
              f"Δ ${p7 - p5:+8.2f}")


if __name__ == "__main__":
    main()
