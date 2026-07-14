"""calibrate_fill_model — step 4 of the sandbox-trust plan: measure how the
broker ACTUALLY fills this bot's orders, versus what the simulation engines
assume (limit-touch ⇒ fill, flat 5 bps slippage per side, sandbox.py
_SLIPPAGE_BPS / backtest fill models).

MEASUREMENT ONLY. This script never edits a constant — it prints and saves the
evidence so the owner can decide. Two hard gates before any constant should be
touched, both enforced in the verdict:

  1. ENVIRONMENT — under MOO_TRD_ENV=SIMULATE the fills come from the broker's
     paper-trading engine (no queue position, no partial-fill dynamics, usually
     fills AT the touch). Paper "slippage" says nothing about real
     microstructure; only the CANCEL RATE (5-min TTL expiries) carries real
     signal because it is driven by real market prices reaching our limit.
  2. SAMPLE SIZE — fewer than MIN_FILLS_FOR_CHANGE filled entries is noise.

WHAT IT MEASURES
  • BUY fill rate: FILLED_ALL vs CANCELLED_ALL (+ partial fills) — the live
    counterpart of the engines' "touch = fill within one bar" assumption.
  • BUY execution: (dealt_avg_price − limit) / limit in bps, distribution.
    Negative = price improvement vs the placed limit.
  • SELL truthfulness: dealt_avg_price vs the exit price recorded in
    db.closed_trades (does the bot's own book match reality?), and for SL exits
    the overshoot vs the intended stop level (the live "soft stop" cost).

Excludes the cash-yield ETF (settings.cash_yield_symbol) — cash parking, not
strategy fills.

Run:  .venv/bin/python3 scripts/calibrate_fill_model.py [--days 90]
Output: data/fill_model_calibration.json (single file, overwritten per run)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_FILLS_FOR_CHANGE = 30
MODELED_SLIPPAGE_BPS = 5.0   # sandbox._SLIPPAGE_BPS — the assumption under test


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if v == v else None   # NaN guard
    except (TypeError, ValueError):
        return None


def _median(xs: list[float]):
    return round(statistics.median(xs), 1) if xs else None


def _pctl(xs: list[float], q: float):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(q * len(s)))], 1)


def fetch_orders(days: int):
    from src.config import settings
    from src.moo_client import MooClient, _env_enum
    start = (date.today() - timedelta(days=days)).isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    c = MooClient()
    try:
        ret, df = c.trade.history_order_list_query(
            start=start, end=end, trd_env=_env_enum())
    finally:
        c.close()
    if ret != 0:
        raise RuntimeError(f"history_order_list_query failed: {df}")
    skip_code = f"US.{settings.cash_yield_symbol}"
    df = df[df["code"] != skip_code]
    return df, settings.moo_trade_env


def analyze_buys(df) -> dict:
    buys = df[df["trd_side"] == "BUY"]
    filled = buys[buys["order_status"] == "FILLED_ALL"]
    cancelled = buys[buys["order_status"] == "CANCELLED_ALL"]
    partial = cancelled[cancelled["dealt_qty"].map(lambda q: (_f(q) or 0) > 0)]
    placed = len(filled) + len(cancelled)

    exec_bps = []
    for _, r in filled.iterrows():
        limit, dealt = _f(r["price"]), _f(r["dealt_avg_price"])
        if limit and dealt and limit > 0:
            exec_bps.append((dealt - limit) / limit * 1e4)

    return {
        "orders_placed": placed,
        "filled_all": len(filled),
        "cancelled_ttl": len(cancelled),
        "cancelled_with_partial_fill": len(partial),
        "fill_rate_pct": round((len(filled) + len(partial)) / placed * 100, 1) if placed else None,
        "exec_vs_limit_bps": {
            "n": len(exec_bps),
            "median": _median(exec_bps),
            "mean": round(statistics.fmean(exec_bps), 1) if exec_bps else None,
            "p90": _pctl(exec_bps, 0.9),
            "worst": round(max(exec_bps), 1) if exec_bps else None,
            "best": round(min(exec_bps), 1) if exec_bps else None,
        },
    }


def analyze_sells(df) -> dict:
    """Join filled SELLs to db.closed_trades (symbol + nearest close date ≤3d)."""
    from src import db
    sells = df[(df["trd_side"] == "SELL") & (df["order_status"] == "FILLED_ALL")]
    trades = db.closed_trades(limit=1000)

    def _trade_date(t):
        try:
            return datetime.fromisoformat(str(t.get("ts", ""))[:19]).date()
        except ValueError:
            return None

    unmatched = list(range(len(trades)))
    book_vs_dealt_bps, sl_overshoot_bps, rows = [], [], []
    for _, r in sells.iterrows():
        sym = str(r["code"]).replace("US.", "")
        dealt = _f(r["dealt_avg_price"])
        try:
            odate = datetime.strptime(str(r["create_time"])[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        best, best_gap = None, None
        for j in unmatched:
            t = trades[j]
            td = _trade_date(t)
            if t.get("symbol") != sym or td is None:
                continue
            gap = abs((td - odate).days)
            if gap <= 3 and (best_gap is None or gap < best_gap):
                best, best_gap = j, gap
        if best is None or not dealt:
            continue
        unmatched.remove(best)
        t = trades[best]
        rec_exit, stop = _f(t.get("exit")), _f(t.get("stop"))
        row = {"symbol": sym, "date": str(odate), "dealt": dealt,
               "recorded_exit": rec_exit, "exit_reason": t.get("exit_reason", "")}
        if rec_exit and rec_exit > 0:
            row["book_vs_dealt_bps"] = round((rec_exit - dealt) / dealt * 1e4, 1)
            book_vs_dealt_bps.append(row["book_vs_dealt_bps"])
        if t.get("exit_reason") == "SL" and stop and stop > 0:
            row["sl_overshoot_bps"] = round((dealt - stop) / stop * 1e4, 1)
            sl_overshoot_bps.append(row["sl_overshoot_bps"])
        rows.append(row)

    return {
        "sells_filled": len(sells),
        "matched_to_closed_trades": len(rows),
        "book_vs_dealt_bps": {   # + = the bot's book is optimistic vs reality
            "n": len(book_vs_dealt_bps),
            "median": _median(book_vs_dealt_bps),
            "worst": round(max(book_vs_dealt_bps), 1) if book_vs_dealt_bps else None,
        },
        "sl_overshoot_bps": {    # − = stopped out BELOW the intended stop
            "n": len(sl_overshoot_bps),
            "median": _median(sl_overshoot_bps),
            "worst": round(min(sl_overshoot_bps), 1) if sl_overshoot_bps else None,
        },
        "matches": rows[:100],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--output", default=str(ROOT / "data" / "fill_model_calibration.json"))
    args = ap.parse_args()

    df, env = fetch_orders(args.days)
    buys = analyze_buys(df)
    sells = analyze_sells(df)

    n_fills = buys["exec_vs_limit_bps"]["n"]
    blockers = []
    if env == "SIMULATE":
        blockers.append("paper trading (SIMULATE) — fill prices come from the broker's "
                        "paper engine, not real microstructure; only the cancel "
                        "rate carries real signal")
    if n_fills < MIN_FILLS_FOR_CHANGE:
        blockers.append(f"only {n_fills} filled entries < {MIN_FILLS_FOR_CHANGE} minimum sample")
    verdict = ("DO NOT change constants yet: " + "; ".join(blockers)) if blockers else (
        "sample + environment sufficient — compare measured bps to the modeled "
        f"{MODELED_SLIPPAGE_BPS} bps and update sandbox._SLIPPAGE_BPS if they disagree")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trd_env": env,
        "lookback_days": args.days,
        "modeled_assumptions": {"slippage_bps_per_side": MODELED_SLIPPAGE_BPS,
                                "fill_model": "limit-touch ⇒ fill within one bar"},
        "buys": buys,
        "sells": sells,
        "verdict": verdict,
    }

    print(f"\n{'='*68}")
    print(f"  FILL-MODEL CALIBRATION — last {args.days}d, env={env}")
    print(f"{'='*68}")
    b = buys
    print(f"  BUY orders: {b['orders_placed']} placed | {b['filled_all']} filled "
          f"| {b['cancelled_ttl']} TTL-cancelled ({b['cancelled_with_partial_fill']} partial)"
          f" → fill rate {b['fill_rate_pct']}%")
    e = b["exec_vs_limit_bps"]
    print(f"  BUY exec vs limit: median {e['median']}bps, mean {e['mean']}bps, "
          f"p90 {e['p90']}bps (n={e['n']}; negative = price improvement)")
    s = sells
    print(f"  SELL fills matched to book: {s['matched_to_closed_trades']}/{s['sells_filled']}")
    print(f"  book vs dealt: median {s['book_vs_dealt_bps']['median']}bps "
          f"(+ = book optimistic) | SL overshoot: median {s['sl_overshoot_bps']['median']}bps "
          f"(n={s['sl_overshoot_bps']['n']}; − = stopped below intended)")
    print(f"\n  modeled: {MODELED_SLIPPAGE_BPS}bps/side, touch⇒fill")
    print(f"  VERDICT: {verdict}")
    print(f"{'='*68}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved to {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
