"""universe_compare — same strategy, different universe. Does a higher-volatility
small/mid-cap universe beat the pinned mega-cap semis on $/day?

Runs the EXACT current live-aligned config (gap 4.0, threshold 70, tp8/sl3.5,
scale-out, momentum, ML off, VIX sizing, earnings gate, real commissions) through
the ONE honest engine on each universe, 180d + 360d. Only the ticker list differs.

CAVEATS (honest): the mid/small-cap basket is chosen for structural volatility +
theme diversity, NOT recent return — but it's still a hand-picked set (selection
bias) and the backtest under-models small-cap gap/halt/liquidity risk. Treat as a
DIRECTION check, not a validated universe.

Run: .venv/bin/python3 scripts/universe_compare.py
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import settings
from src.backtest import BacktestConfig, prefetch_data, _run_live_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("uni_cmp")

MEGA = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
# Liquid, structurally high-volatility small/mid-caps across themes (fintech, EV,
# growth/consumer, crypto-proxy). Picked for ATR% + liquidity + diversity, not
# recent performance. Names with thin/no MooMoo data are auto-skipped by prefetch.
MIDVOL = ["SOFI", "AFRM", "HOOD", "RIVN", "LCID", "RBLX", "U", "PINS",
          "SNAP", "DKNG", "CVNA", "MARA", "RIOT", "UPST"]


def cfg_for(days: int, tickers: list[str]) -> BacktestConfig:
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=list(tickers), account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        apply_ml_gate=False, apply_mr_strategy=False,
        use_scale_out=settings.use_scale_out, tp1_r=settings.tp1_r, tp2_r=settings.tp2_r)


def main() -> None:
    results = {}
    for label, tickers in [("MEGA (current 10)", MEGA), ("MID/SMALL vol basket", MIDVOL)]:
        results[label] = {}
        for days in (180, 360):
            cfg = cfg_for(days, tickers)
            log.info("=== prefetch %s %dd ===", label, days)
            cache = prefetch_data(cfg)
            n_have = len(cache["per_ticker"])
            m = _run_live_engine(cfg, cache, rich_metrics=True)["metrics"]
            results[label][days] = {
                "per_day": m["net_pnl_usd"] / days, "net": m["net_pnl_usd"],
                "dd": m["max_dd_mtm_pct"], "trades": m["total_trades"],
                "win": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
                "n_tickers": n_have,
            }
            log.info("[%s %dd] $%.2f/day (%d tickers had data)",
                     label, days, results[label][days]["per_day"], n_have)

    print("\n" + "=" * 90)
    print("UNIVERSE COMPARISON — identical strategy/params, only tickers differ")
    print("=" * 90)
    hdr = f"{'universe':<22}{'window':>7}{'$/day':>9}{'net$':>9}{'DD%':>8}{'trades':>8}{'win%':>7}{'PF':>7}{'#tkrs':>7}"
    print(hdr)
    for label in results:
        for days in (180, 360):
            r = results[label][days]
            print(f"{label:<22}{str(days)+'d':>7}{r['per_day']:>9.2f}{r['net']:>9.0f}"
                  f"{r['dd']:>8.2f}{r['trades']:>8}{r['win']:>7.1f}{r['pf']:>7.2f}{r['n_tickers']:>7}")

    print("\n" + "=" * 90)
    print("VERDICT (vs current mega-cap)")
    print("=" * 90)
    for days in (180, 360):
        mega = results["MEGA (current 10)"][days]["per_day"]
        mid = results["MID/SMALL vol basket"][days]["per_day"]
        d = mid - mega
        tag = ("⬆ mid/small WINS — worth a proper point-in-time screener" if d > 2
               else "↔ roughly even" if abs(d) <= 2 else "⬇ mega-cap still better")
        print(f"  {days}d: mega ${mega:.2f} vs mid/small ${mid:.2f}  → {d:+.2f}/day  {tag}")
    print("\n  NOTE: selection bias + under-modeled small-cap gap/liquidity risk — "
          "direction check only.")
    print()


if __name__ == "__main__":
    main()
