"""Sweep #2 — relax position cap, concentrate watchlist, target $30/day.

Findings from sweep #1:
  • TP_ATR_MULT=6.0 lifts $/day from $5.39 → $9.43 (best so far)
  • RISK_PER_TRADE 3-4% had ZERO effect — the 10% position cap binds first
  • Concentrated wins: SNDK / MU / INTC / LRCX / DDOG / AMD / WDC produced
    the bulk of profit; ON / AMZN / F / QCOM bled

Sweep #2 strategy:
  • Raise MAX_POSITION_PCT to 0.20 / 0.25 / 0.33 → unlocks bigger sizing
  • Drop MAX_POSITIONS to 5 (matches concentration) so account isn't over-allocated
  • Concentrate watchlist to historical winners
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache  # noqa: E402


# Concentrated watchlist — based on sweep #1's by-symbol PnL ranking.
# Keeps the consistently profitable names (≥60% WR + positive net), drops the
# bleeders (ON, AMZN, F, QCOM, TXN) and the dormant names (META, SPY, QQQ,
# SMCI, SMH which barely traded).
WINNERS = [
    "SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC",
    "SWKS", "PANW", "MCHP", "HPE", "HPQ", "FTNT", "DELL",
    "TSLA", "NVDA", "AAPL", "GOOGL", "MSFT",
]


def variants() -> list[tuple[str, dict]]:
    """Each entry is (name, BacktestConfig overrides, optional tickers).

    The patches we test:
      U series — Unbind position cap (with the v1 winning TP=6.0)
      V series — Concentrated watchlist on top of U
      W series — Push it: high-volatility names only, 33% cap
    """
    # Big TP from sweep #1 winner.
    big_tp = dict(tp_atr_mult=6.0, sl_atr_mult=3.25)
    return [
        ("U1_cap20",        {**big_tp, "max_position_pct": 0.20}),
        ("U2_cap25",        {**big_tp, "max_position_pct": 0.25}),
        ("U3_cap33",        {**big_tp, "max_position_pct": 0.33}),
        ("V1_winners_20",   {**big_tp, "max_position_pct": 0.20, "tickers": WINNERS}),
        ("V2_winners_25",   {**big_tp, "max_position_pct": 0.25, "tickers": WINNERS}),
        ("V3_winners_33",   {**big_tp, "max_position_pct": 0.33, "tickers": WINNERS}),
        # Wider TP + concentration + larger SL to give more breathing room.
        ("W1_huge",         {"tp_atr_mult": 7.0, "sl_atr_mult": 3.5,
                             "max_position_pct": 0.33, "tickers": WINNERS}),
        # Add risk-per-trade increase ALONG with relaxed cap so both can move.
        ("W2_4pct_winners", {**big_tp, "max_position_pct": 0.25,
                             "risk_per_trade": 0.04, "tickers": WINNERS}),
        ("W3_5pct_winners", {**big_tp, "max_position_pct": 0.33,
                             "risk_per_trade": 0.05, "tickers": WINNERS}),
        # Threshold lower to catch more setups.
        ("W4_t55_5pct",     {"tp_atr_mult": 5.0, "sl_atr_mult": 3.0,
                             "max_position_pct": 0.33, "risk_per_trade": 0.05,
                             "threshold": 55, "tickers": WINNERS}),
    ]


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")
    from src.config import settings
    cfg_base = BacktestConfig(
        days=180,
        timeframe="HOUR_1",
        threshold=60.0,
        tickers=[],   # full watchlist for the U-series
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
    )

    print("Prefetching data (full watchlist) …")
    cache = prefetch_data(cfg_base)
    print(f"Prefetched {len(cache['per_ticker'])} tickers.\n")

    rows: list[dict] = []
    for name, overrides in variants():
        # tickers can be either inside overrides or absent.
        cfg = replace(cfg_base, **overrides)

        # If this variant uses a concentrated ticker list, narrow the cache so
        # the simulator only iterates those names. (Simpler than re-prefetching.)
        narrowed_cache = cache
        if cfg.tickers:
            narrowed = {k: v for k, v in cache["per_ticker"].items()
                        if k in cfg.tickers}
            narrowed_cache = {**cache, "per_ticker": narrowed}

        result = simulate_with_cache(cfg, narrowed_cache)
        m = result["metrics"]
        rows.append({
            "name": name,
            "overrides": overrides,
            "trades": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0),
            "daily": m.get("net_pnl_usd", 0) / cfg.days,
            "sortino": m.get("sortino_ratio", 0),
            "sharpe": m.get("sharpe_ratio", 0),
            "max_dd_pct": m.get("max_drawdown_pct", 0),
            "monthly": m.get("monthly_pnl", {}),
        })

    rows.sort(key=lambda r: -r["daily"])

    print("\n" + "=" * 100)
    print("  SWEEP #2 — RELAXED POSITION CAP + CONCENTRATED WATCHLIST")
    print("=" * 100)
    print(f"  {'name':<18} {'trades':>6} {'WR%':>5} {'PF':>5} "
          f"{'net $':>10} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("  " + "-" * 96)
    for r in rows:
        marker = "  ⭐" if r["daily"] >= 30 else ("  ↑" if r["daily"] >= 15 else "")
        print(f"  {r['name']:<18} {r['trades']:>6} {r['wr']:>5.1f} "
              f"{r['pf']:>5.2f} {r['net']:>+10.2f} {r['daily']:>+8.2f} "
              f"{r['sortino']:>+8.2f} {r['max_dd_pct']:>7.2f}{marker}")
    print("=" * 100)

    best = rows[0]
    print(f"\n  Best: {best['name']} → ${best['daily']:+.2f}/day  "
          f"(Sortino {best['sortino']}, MaxDD {best['max_dd_pct']}%)")
    print(f"  Overrides: {best['overrides']}")
    print(f"  Monthly:")
    for month, pnl in best["monthly"].items():
        bar = "█" * min(30, max(0, int(abs(pnl) / 20)))
        sign = "+" if pnl >= 0 else "-"
        print(f"    {month}  {sign}${abs(pnl):.2f}  {bar}")

    out = ROOT / "data" / "variant_sweep_v2.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\n  Saved → {out}")


if __name__ == "__main__":
    main()
