"""sandbox_vs_backtest — TRADE-LEVEL differential between the two independent
strategy implementations:

  • sandbox (src/sandbox.py)          — slow replay through the LIVE gate code
  • backtest_v3 live-fidelity lens    — the fast honest engine (_run_live_engine)

engine_compare already cross-checks v3 against the frozen oracle — two FAST
engines validating each other. Nothing previously compared a fast engine with
the sandbox, and aggregate metrics alone are not enough: two runs can post the
same net PnL while holding completely different names. This script aligns the
portfolios trade-by-trade, so when backtest_v3 drifts again you learn WHICH
gate / WHICH trade drifted, not just "the number moved".

WHAT IT DOES
  1. runs both engines over the SAME last-N-days window and the SAME
     runtime-effective config (db budget override + runtime params — the
     2026-07-11 sandbox parity fix makes this alignment real)
  2. aligns trades by (symbol, entry date) within ±1 business day
  3. reports trades present on only one side — sandbox-missing ones are
     attributed to the sandbox gate that blocked them (from the bounded
     skip_events log run_sandbox now emits)
  4. reports entry/exit price deltas (bps) for matched pairs
  5. exits non-zero on tolerance breach → cron/health-check-able
     (per owner preference, only an actionable breach should ever page)

KNOWN STRUCTURAL DIFFERENCES (expected noise, not bugs — tune tolerances
around them rather than chasing each occurrence):
  • sandbox applies the live regime-adaptive threshold (BULL −5 / NEUTRAL +5);
    v3 runs a fixed cfg.threshold
  • sandbox exits are SL/TP/MAX_HOLD only; v3's live lens also models
    breakeven/scale-out/trailing when those settings are on → exit-reason and
    exit-price mismatches on matched trades
  • fill models differ (sandbox: limit vs next-bar low; v3 live lens:
    next-bar-open-only) → small entry-bps deltas are normal
  • sandbox scans a 30-min grid vs v3's hourly bars

BASELINE (2026-07-11, 30d window, THR=65/SL=3.5/TP=12 runtime overrides):
  sandbox 27 vs v3 15 in-window trades, 9 matched (33%) — the gap is mostly
  sandbox's regime-adaptive threshold (BULL floor 60 vs v3's fixed 65) plus
  v3's 17 cash-limited entries. Same-day matched entries: median |Δ| ≈ 6bps
  (fill models agree); ±1-day pairs carry weekend gaps (up to ~580bps, honest
  non-signal — that's why the tolerance reads same-day pairs only). Default
  tolerances are set from this baseline + margin; a breach means the relation
  between the two engines CHANGED, i.e. one of them drifted.

Exit codes: 0 = within tolerance · 2 = divergence breach · 1 = run error

Run:
  .venv/bin/python3 scripts/sandbox_vs_backtest.py --days 45
  .venv/bin/python3 scripts/sandbox_vs_backtest.py \
      --sandbox-json data/sandbox_result.json --v3-json data/v3_result.json
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")

DEFAULT_OUTPUT = ROOT / "data" / "sandbox_vs_backtest.json"
MAX_LISTED = 300          # bound every list in the JSON report
ATTRIB_WINDOW_DAYS = 2    # skip-event search radius around a missing entry


# ── engine runners ─────────────────────────────────────────

def _run_sandbox(days: int, tickers: list[str] | None) -> dict:
    from src.sandbox import SandboxConfig, run_sandbox
    end = datetime.now(ET).replace(hour=16, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    cfg = SandboxConfig(start=start, end=end, tickers=tickers or [],
                        universe_mode="static", enable_ai=False)
    return run_sandbox(cfg)


def _run_v3(days: int, tickers: list[str]) -> dict:
    from src import runtime_config as rc
    from src.backtest import BacktestConfig, prefetch_data, _run_live_engine
    from src.config import settings
    from src.risk_manager import budget_usd

    cfg = BacktestConfig(
        days=days, timeframe="HOUR_1",
        threshold=rc.entry_threshold(), tickers=tickers,
        account_usd=budget_usd(),                 # same capital the sandbox resolves
        risk_per_trade=rc.risk_per_trade(),
        max_position_pct=rc.max_position_pct(),
        max_hold_days=rc.max_hold_days(),
        tp_atr_mult=rc.tp_atr_mult(), sl_atr_mult=rc.sl_atr_mult(),
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=settings.mr_enabled,
    )
    cache = prefetch_data(cfg)
    if not cache.get("per_ticker"):
        raise RuntimeError("prefetch returned 0 tickers (network/OpenD down?)")
    return _run_live_engine(cfg, cache)


# ── trade normalization + alignment ────────────────────────

def _date_of(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _norm_sandbox_trades(result: dict) -> list[dict]:
    out = []
    for t in result.get("trades", []):
        if "entry_t" not in t:
            raise SystemExit("sandbox result has no entry_t timestamps — re-run "
                             "src/sandbox.py (needs the 2026-07-11 version)")
        out.append({"symbol": t["symbol"], "entry_date": _date_of(t["entry_t"]),
                    "exit_date": _date_of(t["exit_t"]), "entry": float(t["entry"]),
                    "exit": float(t["exit"]), "qty": int(t.get("qty", 0)),
                    "reason": t.get("reason", ""), "net_pnl": float(t.get("net_pnl", 0))})
    return out


def _norm_v3_trades(result: dict) -> list[dict]:
    out = []
    for t in result.get("trades", []):
        out.append({"symbol": t["symbol"], "entry_date": _date_of(t["entry_date"]),
                    "exit_date": _date_of(t["exit_date"]) if t.get("exit_date") else None,
                    "entry": float(t["entry_price"]), "exit": float(t.get("exit_price", 0)),
                    "qty": int(t.get("qty_initial") or t.get("qty") or 0),
                    "reason": t.get("exit_reason", ""), "net_pnl": float(t.get("pnl", 0))})
    return out


def _busdays_apart(a: date, b: date) -> int:
    lo, hi = min(a, b), max(a, b)
    return int(np.busday_count(lo, hi))


def align(sb: list[dict], v3: list[dict], max_busdays: int = 1):
    """Greedy nearest-entry-date matching per symbol. Returns
    (matched pairs, sandbox-only, v3-only)."""
    unmatched_v3 = list(range(len(v3)))
    matched, sb_only = [], []
    for s in sorted(sb, key=lambda t: t["entry_date"]):
        best, best_gap = None, None
        for j in unmatched_v3:
            if v3[j]["symbol"] != s["symbol"]:
                continue
            gap = _busdays_apart(s["entry_date"], v3[j]["entry_date"])
            if gap <= max_busdays and (best_gap is None or gap < best_gap):
                best, best_gap = j, gap
        if best is None:
            sb_only.append(s)
        else:
            unmatched_v3.remove(best)
            matched.append((s, v3[best]))
    v3_only = [v3[j] for j in unmatched_v3]
    return matched, sb_only, v3_only


# ── gate attribution (sandbox skip_events) ─────────────────

def attribute_gates(trade: dict, skip_events: list[dict]) -> list[str]:
    """Which sandbox gate(s) blocked `symbol` around this v3 entry date?"""
    d0 = trade["entry_date"]
    gates = {}
    for ev in skip_events:
        if ev.get("symbol") != trade["symbol"]:
            continue
        try:
            ed = _date_of(ev["t"])
        except (ValueError, KeyError):
            continue
        if abs((ed - d0).days) <= ATTRIB_WINDOW_DAYS:
            gates[ev["gate"]] = gates.get(ev["gate"], 0) + 1
    if not gates:
        return ["no_signal (never reached the sandbox funnel — score<floor, "
                "regime/breadth scan-skip, or fill missed)"]
    return [f"{g}×{n}" for g, n in sorted(gates.items(), key=lambda kv: -kv[1])]


# ── report ─────────────────────────────────────────────────

def _bps(a: float, b: float) -> float:
    return (a - b) / b * 1e4 if b else 0.0


def build_report(sb_result: dict, v3_result: dict, args) -> tuple[dict, list[str]]:
    sb_all = _norm_sandbox_trades(sb_result)
    v3_all = _norm_v3_trades(v3_result)

    # Compare ONLY the window both engines simulated. BacktestConfig.days is a
    # bar-count budget, so v3's simulated span starts EARLIER than the sandbox's
    # calendar window — v3 trades before the sandbox start are non-overlap, not
    # divergence (first real run: 11 of 20 "only in v3" predated the window).
    win = sb_result.get("config", {})
    w_lo = date.fromisoformat(str(win["start"])) if win.get("start") else None
    w_hi = date.fromisoformat(str(win["end"])) if win.get("end") else None

    def _in_window(t):
        return ((w_lo is None or t["entry_date"] >= w_lo)
                and (w_hi is None or t["entry_date"] <= w_hi))

    sb = [t for t in sb_all if _in_window(t)]
    v3 = [t for t in v3_all if _in_window(t)]
    matched, sb_only, v3_only = align(sb, v3)
    skip_events = sb_result.get("skip_events", [])

    pairs = []
    for s, v in matched:
        pairs.append({
            "symbol": s["symbol"],
            "entry_date_sb": str(s["entry_date"]), "entry_date_v3": str(v["entry_date"]),
            "same_day": s["entry_date"] == v["entry_date"],
            "entry_bps": round(_bps(s["entry"], v["entry"]), 1),
            "exit_bps": round(_bps(s["exit"], v["exit"]), 1) if v["exit"] else None,
            "reason_sb": s["reason"], "reason_v3": v["reason"],
            "reason_match": s["reason"] == v["reason"],
            "net_pnl_sb": s["net_pnl"], "net_pnl_v3": v["net_pnl"],
        })

    v3_only_rows = [{
        "symbol": t["symbol"], "entry_date": str(t["entry_date"]),
        "net_pnl_v3": t["net_pnl"],
        "sandbox_gates": attribute_gates(t, skip_events),
    } for t in sorted(v3_only, key=lambda t: t["entry_date"])]

    sb_only_rows = [{
        "symbol": t["symbol"], "entry_date": str(t["entry_date"]),
        "net_pnl_sb": t["net_pnl"],
        "note": "absent in v3 (no per-signal telemetry there; see v3_gate_counters)",
    } for t in sorted(sb_only, key=lambda t: t["entry_date"])]

    n_sb, n_v3, n_m = len(sb), len(v3), len(matched)
    match_rate = n_m / max(1, max(n_sb, n_v3))
    entry_bps_abs = [abs(p["entry_bps"]) for p in pairs]
    # The drift tolerance reads SAME-DAY pairs only: a ±1-day match compares two
    # different bars (weekend gaps → hundreds of bps of honest non-signal), while
    # same-day pairs isolate the FILL MODEL — the thing that can silently drift.
    sameday_bps_abs = [abs(p["entry_bps"]) for p in pairs if p["same_day"]]
    tol_bps = sameday_bps_abs or entry_bps_abs
    exit_bps_abs = [abs(p["exit_bps"]) for p in pairs if p["exit_bps"] is not None]
    m_v3 = v3_result.get("metrics", {})
    # Net PnL compared over IN-WINDOW trades only — the engine-reported totals
    # cover different spans (see window note above) and would inflate the gap.
    net_sb = sum(t["net_pnl"] for t in sb)
    net_v3 = sum(t["net_pnl"] for t in v3)
    # relative net gap only meaningful when both sides moved real money
    net_gap_pct = (abs(net_sb - net_v3) / max(abs(net_sb), abs(net_v3)) * 100
                   if max(abs(net_sb), abs(net_v3)) >= args.min_net_for_gap else 0.0)

    # ── tolerance checks ──
    breaches = []
    if n_sb + n_v3 == 0:
        breaches.append("ZERO trades on BOTH engines — window too quiet or data problem")
    else:
        if match_rate < args.min_match_rate:
            breaches.append(f"match rate {match_rate:.0%} < {args.min_match_rate:.0%}")
        if tol_bps and statistics.median(tol_bps) > args.max_median_entry_bps:
            breaches.append(f"median |entry Δ| {statistics.median(tol_bps):.0f}bps"
                            f" (same-day pairs) > {args.max_median_entry_bps:.0f}bps")
        if net_gap_pct > args.max_net_gap_pct:
            breaches.append(f"net PnL gap {net_gap_pct:.0f}% > {args.max_net_gap_pct:.0f}%"
                            f" (sandbox ${net_sb:,.0f} vs v3 ${net_v3:,.0f})")

    report = {
        "generated_at": datetime.now(ET).isoformat(),
        "window_days": args.days,
        "compare_window": {"start": str(w_lo), "end": str(w_hi),
                           "v3_trades_outside_window": len(v3_all) - len(v3),
                           "sandbox_trades_outside_window": len(sb_all) - len(sb)},
        "verdict": "BREACH" if breaches else "OK",
        "breaches": breaches,
        "summary": {
            "n_sandbox": n_sb, "n_v3": n_v3, "n_matched": n_m,
            "match_rate_pct": round(match_rate * 100, 1),
            "median_entry_bps": round(statistics.median(entry_bps_abs), 1) if entry_bps_abs else None,
            "median_entry_bps_same_day": round(statistics.median(sameday_bps_abs), 1) if sameday_bps_abs else None,
            "n_same_day_pairs": len(sameday_bps_abs),
            "max_entry_bps": round(max(entry_bps_abs), 1) if entry_bps_abs else None,
            "median_exit_bps": round(statistics.median(exit_bps_abs), 1) if exit_bps_abs else None,
            "reason_mismatches": sum(1 for p in pairs if not p["reason_match"]),
            "net_pnl_sandbox": round(net_sb, 2), "net_pnl_v3": round(net_v3, 2),
            "net_gap_pct": round(net_gap_pct, 1),
            "net_pnl_v3_full_run": round(m_v3.get("net_pnl_usd", 0.0), 2),
        },
        "tolerances": {"min_match_rate": args.min_match_rate,
                       "max_median_entry_bps": args.max_median_entry_bps,
                       "max_net_gap_pct": args.max_net_gap_pct},
        "matched": pairs[:MAX_LISTED],
        "only_in_v3": v3_only_rows[:MAX_LISTED],
        "only_in_sandbox": sb_only_rows[:MAX_LISTED],
        "sandbox_skip_counts": sb_result.get("skip_reasons", {}),
        "v3_gate_counters": {k: m_v3.get(k) for k in
                             ("n_cash_limited_entries", "n_earnings_blocked",
                              "n_rs_blocked", "n_sector_regime_blocked")
                             if k in m_v3},
    }
    return report, breaches


def print_report(rep: dict):
    s = rep["summary"]
    w = rep["compare_window"]
    print(f"\n{'='*72}")
    print(f"  SANDBOX vs BACKTEST_V3 — trade-level diff ({rep['window_days']}d window)")
    print(f"{'='*72}")
    print(f"  compare window: {w['start']} → {w['end']} "
          f"(dropped as non-overlap: v3 {w['v3_trades_outside_window']}, "
          f"sandbox {w['sandbox_trades_outside_window']})")
    print(f"  trades: sandbox {s['n_sandbox']} | v3 {s['n_v3']} | "
          f"matched {s['n_matched']} ({s['match_rate_pct']}%)")
    if s["median_entry_bps"] is not None:
        sd = (f"{s['median_entry_bps_same_day']}bps over {s['n_same_day_pairs']} same-day pairs"
              if s["median_entry_bps_same_day"] is not None else "no same-day pairs")
        print(f"  matched entries: median |Δ| {s['median_entry_bps']}bps all / {sd} "
              f"(max {s['max_entry_bps']}bps) | exit reason mismatches: "
              f"{s['reason_mismatches']}/{s['n_matched']}")
    print(f"  net PnL: sandbox ${s['net_pnl_sandbox']:,.2f} | "
          f"v3 ${s['net_pnl_v3']:,.2f} | gap {s['net_gap_pct']}%")

    if rep["only_in_v3"]:
        print(f"\n  ── only in v3 ({len(rep['only_in_v3'])}) — sandbox gate attribution:")
        for r in rep["only_in_v3"][:15]:
            print(f"    {r['entry_date']} {r['symbol']:<6} "
                  f"(v3 pnl ${r['net_pnl_v3']:+,.0f})  ← {', '.join(r['sandbox_gates'])}")
        if len(rep["only_in_v3"]) > 15:
            print(f"    … {len(rep['only_in_v3']) - 15} more (see JSON)")
    if rep["only_in_sandbox"]:
        print(f"\n  ── only in sandbox ({len(rep['only_in_sandbox'])}):")
        for r in rep["only_in_sandbox"][:15]:
            print(f"    {r['entry_date']} {r['symbol']:<6} (sb pnl ${r['net_pnl_sb']:+,.0f})")
        if len(rep["only_in_sandbox"]) > 15:
            print(f"    … {len(rep['only_in_sandbox']) - 15} more (see JSON)")
        print(f"    v3 aggregate gate counters: {rep['v3_gate_counters']}")

    print(f"\n  VERDICT: {rep['verdict']}")
    for b in rep["breaches"]:
        print(f"    ✗ {b}")
    print(f"{'='*72}")


# ── main ───────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=45,
                    help="compare the last N days (both engines, same window)")
    ap.add_argument("--tickers", nargs="*", default=None,
                    help="override watchlist tickers")
    ap.add_argument("--sandbox-json", default=None,
                    help="reuse a saved run_sandbox result instead of re-running")
    ap.add_argument("--v3-json", default=None,
                    help="reuse a saved v3 result instead of re-running")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    # Tolerances — calibrated from the 2026-07-11 baseline (see docstring) with
    # margin: the two implementations are structurally different (that's their
    # value); a breach should mean "a human must look", not "the fill models
    # rounded differently".
    ap.add_argument("--min-match-rate", type=float, default=0.25)
    ap.add_argument("--max-median-entry-bps", type=float, default=25.0,
                    help="tolerance on the SAME-DAY matched-pair median")
    ap.add_argument("--max-net-gap-pct", type=float, default=60.0)
    ap.add_argument("--min-net-for-gap", type=float, default=50.0,
                    help="skip the net-gap check when both |net|s are below this "
                         "(a $3-vs-$12 window is noise, not divergence)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Raw engine outputs are saved next to the report (single files, overwritten
    # each run) so tolerance tweaks can re-run instantly via --sandbox-json /
    # --v3-json without re-hitting OpenD.
    if args.sandbox_json:
        sb_result = json.loads(Path(args.sandbox_json).read_text())
    else:
        print(f"[1/2] sandbox replay — last {args.days} days …", flush=True)
        sb_result = _run_sandbox(args.days, args.tickers)
        (out.parent / "sandbox_vs_backtest_raw_sandbox.json").write_text(
            json.dumps(sb_result, default=str))

    if args.v3_json:
        v3_result = json.loads(Path(args.v3_json).read_text())
    else:
        print(f"[2/2] backtest_v3 live-fidelity — last {args.days} days …", flush=True)
        tickers = args.tickers or json.loads(
            (ROOT / "config" / "watchlist.json").read_text())["tickers"]
        v3_result = _run_v3(args.days, tickers)
        (out.parent / "sandbox_vs_backtest_raw_v3.json").write_text(
            json.dumps(v3_result, default=str))

    report, breaches = build_report(sb_result, v3_result, args)
    print_report(report)

    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved to {out}")
    return 2 if breaches else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
