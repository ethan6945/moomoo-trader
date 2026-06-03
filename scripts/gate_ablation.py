"""gate_ablation — the COMPLETE-version honest backtest + which gates strangle returns.

Two deliverables for the owner:
  1. The latest/complete-version honest $/day (full .env mirror: trend+momentum,
     scale-out on, VIX sizing, earnings gate, real MY commissions, ML off, mpp=0.40).
  2. A gate-weighting table: turn each entry gate OFF and measure the $/day delta,
     so we can see which gates EARN their place vs which are just over-conservative
     gatekeeping that costs returns (owner wants fewer gates, more $/day).

A gate whose $/day goes UP when removed = a candidate to drop/loosen.
A gate whose $/day drops a lot when removed = protective, keep.

Reuses one prefetched cache per window across all configs. Run:
    .venv/bin/python3 scripts/gate_ablation.py
"""
from __future__ import annotations
import logging
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import settings
from src.backtest import BacktestConfig, prefetch_data
from src.backtest_v3 import simulate_v3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("gate_ablation")

PINNED10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]


def full_cfg(days: int, **overrides) -> BacktestConfig:
    """Complete live-mirror config = everything the real bot runs."""
    base = BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=list(PINNED10), account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        # the real bot today: ML OFF (inert, ML_ENABLED=false), MR off, momentum on
        apply_ml_gate=False, apply_mr_strategy=False, apply_momentum_strategy=True,
        # scale-out as per .env
        use_scale_out=settings.use_scale_out, tp1_r=settings.tp1_r, tp2_r=settings.tp2_r,
        # live-fidelity knobs (normally set by _run_live_engine)
        apply_vix_sizing=True, apply_earnings_gate=True, use_realistic_commission=True,
        # gates default ON (live funnel)
        apply_mtf_gate=True, apply_gap_gate=True, apply_regime_gate=True,
    )
    return replace(base, **overrides)


def run(cfg: BacktestConfig, cache: dict, days: int) -> dict:
    m = simulate_v3(cfg, cache, enforce_cash=True, rich_metrics=True)["metrics"]
    net = m.get("net_pnl_usd", 0.0)
    return {"per_day": net / days, "net": net, "dd": m.get("max_dd_mtm_pct", 0.0),
            "trades": m.get("total_trades", 0), "win": m.get("win_rate_pct", 0.0),
            "pf": m.get("profit_factor", 0.0)}


# (label, overrides) — each turns ONE gate off / loosens it vs the full baseline.
ABLATIONS = [
    ("FULL baseline",      {}),
    ("− MTF gate",         {"apply_mtf_gate": False}),
    ("− gap gate",         {"apply_gap_gate": False}),
    ("− regime gate",      {"apply_regime_gate": False}),
    ("− earnings gate",    {"apply_earnings_gate": False}),
    ("− scale-out",        {"use_scale_out": False}),
    ("threshold 65",       {"threshold": 65.0}),
    ("threshold 60",       {"threshold": 60.0}),
    ("gap cap 4%",         {"max_gap_pct": 4.0}),
    ("LOOSE combo",        {"apply_mtf_gate": False, "apply_gap_gate": False,
                            "apply_regime_gate": False, "threshold": 65.0,
                            "max_gap_pct": 4.0}),
]


def main() -> None:
    windows = [180, 360]
    results: dict = {}
    for days in windows:
        log.info("=== prefetch %dd ===", days)
        cache = prefetch_data(full_cfg(days))  # regime+mtf+gap on → SPY+daily cached
        results[days] = {}
        for label, ov in ABLATIONS:
            log.info("[%dd] %s", days, label)
            results[days][label] = run(full_cfg(days, **ov), cache, days)

    print("\n" + "=" * 96)
    print("COMPLETE-VERSION HONEST BACKTEST + GATE WEIGHTING (pinned 10, mpp=%.2f, "
          "thr=%.0f tp=%.1f sl=%.1f scale-out=%s)" % (
              settings.max_position_pct, settings.entry_threshold,
              settings.tp_atr_mult, settings.sl_atr_mult, settings.use_scale_out))
    print("  Δ/day = vs FULL baseline. POSITIVE Δ when a gate is removed ⇒ that gate "
          "was COSTING returns (over-conservative).")
    print("=" * 96)
    hdr = f"{'config':<18}{'$/day':>9}{'Δ/day':>9}{'net$':>9}{'DD%':>8}{'trades':>8}{'win%':>7}{'PF':>7}"
    for days in windows:
        base_pd = results[days]["FULL baseline"]["per_day"]
        print(f"\n--- {days}d ---")
        print(hdr)
        for label, _ in ABLATIONS:
            r = results[days][label]
            d = r["per_day"] - base_pd
            dstr = "" if label == "FULL baseline" else f"{d:+.2f}"
            print(f"{label:<18}{r['per_day']:>9.2f}{dstr:>9}{r['net']:>9.0f}"
                  f"{r['dd']:>8.2f}{r['trades']:>8}{r['win']:>7.1f}{r['pf']:>7.2f}")

    # ---- gate weighting summary (avg Δ across windows) ----
    print("\n" + "=" * 96)
    print("GATE WEIGHTING (avg Δ$/day across 180d+360d when REMOVED/loosened)")
    print("=" * 96)
    rows = []
    for label, _ in ABLATIONS:
        if label == "FULL baseline":
            continue
        deltas = [results[d][label]["per_day"] - results[d]["FULL baseline"]["per_day"]
                  for d in windows]
        avg = sum(deltas) / len(deltas)
        dd_chg = [results[d][label]["dd"] - results[d]["FULL baseline"]["dd"] for d in windows]
        avg_dd = sum(dd_chg) / len(dd_chg)
        rows.append((avg, label, deltas, avg_dd))
    rows.sort(reverse=True)
    for avg, label, deltas, avg_dd in rows:
        verdict = ("⬆ DROP/LOOSEN — costs returns" if avg > 0.5
                   else "↔ neutral — keep or loosen" if avg > -0.5
                   else "🛡 PROTECTIVE — keep")
        print(f"  {label:<18} avgΔ {avg:+.2f}/day  (180d {deltas[0]:+.2f}, 360d {deltas[1]:+.2f})  "
              f"DDΔ {avg_dd:+.2f}pp  → {verdict}")
    print()


if __name__ == "__main__":
    main()
