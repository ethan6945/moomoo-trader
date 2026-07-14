"""Diagnose losses + sweep aggressive configs targeting $30/day.

Steps:
  1. Read the saved backtest result, decompose trades to find leaks.
  2. Re-prefetch data once, run multiple variant configs against the cache.
  3. Rank variants by daily $ profit and Sortino.

Usage:
  python -m scripts.diagnose_and_optimize
"""
from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest import (  # noqa: E402
    BacktestConfig,
    prefetch_data,
    simulate_with_cache,
)


# ------------------------------------------------------------------
# (1) decompose existing trade log
# ------------------------------------------------------------------

def decompose_trades(result: dict) -> None:
    trades = result["trades"]
    cfg = result["config"]
    days = cfg["days"]
    pnl_total = sum(t["pnl"] for t in trades)
    daily = pnl_total / days

    print("\n" + "=" * 70)
    print(f"  BASELINE DIAGNOSTIC  |  {len(trades)} trades over {days} days")
    print("=" * 70)
    print(f"  Net PnL: ${pnl_total:+.2f}  →  ${daily:+.2f}/day "
          f"(target: $30/day)")
    print(f"  Gap to target: ${30 - daily:+.2f}/day  "
          f"(${(30 - daily) * days:+.0f} more over the window)")

    # By exit reason — what kind of exit is dragging?
    by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        r = t["exit_reason"]
        by_reason[r]["n"] += 1
        by_reason[r]["pnl"] += t["pnl"]
        by_reason[r]["wins"] += 1 if t["pnl"] > 0 else 0
    print("\n  -- Exit breakdown --")
    for r, s in sorted(by_reason.items(), key=lambda x: -x[1]["pnl"]):
        wr = s["wins"] / s["n"] * 100 if s["n"] else 0
        avg = s["pnl"] / s["n"] if s["n"] else 0
        print(f"    {r:<10}  {s['n']:4d} trades  WR={wr:4.0f}%  "
              f"avg=${avg:+7.2f}  total=${s['pnl']:+8.2f}")

    # By symbol — concentration
    by_sym = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        s = t["symbol"]
        by_sym[s]["n"] += 1
        by_sym[s]["pnl"] += t["pnl"]
        by_sym[s]["wins"] += 1 if t["pnl"] > 0 else 0
    ranked = sorted(by_sym.items(), key=lambda x: -x[1]["pnl"])
    print("\n  -- Per-ticker (top 10 winners) --")
    for sym, s in ranked[:10]:
        wr = s["wins"] / s["n"] * 100
        print(f"    {sym:<6}  {s['n']:3d} trades  WR={wr:4.0f}%  "
              f"pnl=${s['pnl']:+8.2f}")
    print("  -- Per-ticker (top 5 losers) --")
    for sym, s in ranked[-5:]:
        wr = s["wins"] / s["n"] * 100
        print(f"    {sym:<6}  {s['n']:3d} trades  WR={wr:4.0f}%  "
              f"pnl=${s['pnl']:+8.2f}")

    # Average winner vs loser
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    print(f"\n  -- R:R --")
    print(f"    Avg win  : ${avg_win:+.2f}  ({len(wins)} trades)")
    print(f"    Avg loss : ${avg_loss:+.2f}  ({len(losses)} trades)")
    print(f"    Payoff   : {abs(avg_win / avg_loss):.2f}:1  "
          f"(<1 means losses bigger than wins)")

    # Time exit drag: MAX_HOLD trades that ended slightly negative or flat
    mh = [t for t in trades if t["exit_reason"] == "MAX_HOLD"]
    if mh:
        mh_pnl = sum(t["pnl"] for t in mh)
        mh_near_zero = [t for t in mh if -3 < t["pnl"] < 3]
        print(f"\n  -- MAX_HOLD drag --")
        print(f"    {len(mh)} time-exits, net ${mh_pnl:+.2f}, "
              f"{len(mh_near_zero)} flat (-$3..+$3)")

    # SL bunching — same ticker hit SL twice within N trades (waste)
    rebleeds = []
    by_sym_chrono = defaultdict(list)
    for t in sorted(trades, key=lambda x: x["entry_date"]):
        by_sym_chrono[t["symbol"]].append(t)
    for sym, ts in by_sym_chrono.items():
        for i in range(1, len(ts)):
            if ts[i - 1]["exit_reason"] == "SL" and ts[i]["pnl"] < 0:
                # entry within 3 days of prior SL?
                prev_exit = pd.to_datetime(ts[i - 1]["exit_date"])
                curr_entry = pd.to_datetime(ts[i]["entry_date"])
                gap_days = (curr_entry - prev_exit).days
                if 0 <= gap_days <= 3:
                    rebleeds.append((sym, ts[i - 1]["exit_date"],
                                     ts[i]["entry_date"], ts[i]["pnl"]))
    if rebleeds:
        rebleed_pnl = sum(r[3] for r in rebleeds)
        print(f"\n  -- SL rebleeds (re-enter ≤3d after a stop) --")
        print(f"    {len(rebleeds)} rebleed losses, total ${rebleed_pnl:+.2f}")
        for r in rebleeds[:6]:
            print(f"      {r[0]:<6}  SL {r[1]} → re-entry {r[2]} → ${r[3]:+.2f}")
    print("=" * 70)


# ------------------------------------------------------------------
# (2) variant configs to test for $30/day goal
# ------------------------------------------------------------------

def variants() -> list[tuple[str, dict]]:
    """Each entry overrides BacktestConfig fields.

    Strategy menu:
      A. Lift the payoff ratio (bigger TP, same or wider SL).
      B. Lever up the risk-per-trade (current 2% has a 13.8% Kelly headroom).
      C. Combine A + B.
      D. Trim the watchlist to historical winners (concentration).
      E. Add tighter time-exit (max_hold 3 days instead of 5).
      F. The "go-for-broke" combo.
    """
    return [
        ("baseline",         dict()),
        ("A_wider_TP",       dict(tp_atr_mult=4.5, sl_atr_mult=3.25)),
        ("A2_huge_TP",       dict(tp_atr_mult=6.0, sl_atr_mult=3.25)),
        ("B_3pct_risk",      dict(risk_per_trade=0.03)),
        ("B2_4pct_risk",     dict(risk_per_trade=0.04)),
        ("C_combo_3R",       dict(tp_atr_mult=4.5, sl_atr_mult=3.25,
                                  risk_per_trade=0.03)),
        ("C2_combo_4R",      dict(tp_atr_mult=5.0, sl_atr_mult=3.25,
                                  risk_per_trade=0.04)),
        ("E_short_hold",     dict(max_hold_days=3, tp_atr_mult=3.0,
                                  sl_atr_mult=3.25)),
        ("F_aggressive",     dict(tp_atr_mult=5.0, sl_atr_mult=3.0,
                                  risk_per_trade=0.035, max_hold_days=4,
                                  threshold=58)),
        ("G_high_score",     dict(threshold=68, risk_per_trade=0.035,
                                  tp_atr_mult=5.0, sl_atr_mult=3.0)),
        ("H_loose_gap",      dict(max_gap_pct=5.0, threshold=58,
                                  risk_per_trade=0.03, tp_atr_mult=4.5,
                                  sl_atr_mult=3.0)),
        ("I_no_regime",      dict(apply_regime_gate=False,
                                  risk_per_trade=0.03, tp_atr_mult=5.0,
                                  sl_atr_mult=3.0)),
    ]


def run_variants(cfg_base: BacktestConfig, cache: dict,
                 winners: list[str] | None = None) -> list[dict]:
    rows = []
    for name, overrides in variants():
        cfg = replace(cfg_base, **overrides)
        result = simulate_with_cache(cfg, cache)
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
    return rows


def print_variants(rows: list[dict]) -> None:
    print("\n" + "=" * 100)
    print("  VARIANT COMPARISON — sorted by $/day")
    print("=" * 100)
    print(f"  {'name':<16} {'trades':>6} {'WR%':>5} {'PF':>5} "
          f"{'net $':>10} {'$/day':>8} {'Sortino':>8} {'MaxDD%':>7}")
    print("  " + "-" * 96)
    rows_sorted = sorted(rows, key=lambda r: -r["daily"])
    for r in rows_sorted:
        marker = "  ⭐" if r["daily"] >= 30 else ("  ↑" if r["daily"] >= 15 else "")
        print(f"  {r['name']:<16} {r['trades']:>6} {r['wr']:>5.1f} "
              f"{r['pf']:>5.2f} {r['net']:>+10.2f} {r['daily']:>+8.2f} "
              f"{r['sortino']:>+8.2f} {r['max_dd_pct']:>7.2f}{marker}")
    print("=" * 100)

    # Best result — show monthly breakdown
    best = rows_sorted[0]
    print(f"\n  Best variant: {best['name']}  →  ${best['daily']:+.2f}/day")
    print(f"  Overrides: {best['overrides']}")
    print(f"  Monthly PnL breakdown:")
    for month, pnl in best["monthly"].items():
        bar = "█" * min(30, max(0, int(abs(pnl) / 20)))
        sign = "+" if pnl >= 0 else "-"
        print(f"    {month}  {sign}${abs(pnl):.2f}  {bar}")


# ------------------------------------------------------------------
# (3) main
# ------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")

    # Step 1 — diagnose existing saved trades
    saved = json.loads((ROOT / "data" / "backtest_results.json").read_text())
    decompose_trades(saved)

    # Step 2 — re-prefetch and sweep variants (single OpenD prefetch pass)
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
    print("\n  Prefetching data (single round-trip to the broker)…")
    cache = prefetch_data(cfg_base)
    print(f"  Prefetched {len(cache['per_ticker'])} tickers.")

    rows = run_variants(cfg_base, cache)
    print_variants(rows)

    # Save the variant report
    out = ROOT / "data" / "variant_sweep.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\n  Variant sweep saved → {out}")


if __name__ == "__main__":
    main()
