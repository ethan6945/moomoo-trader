"""phase3_validate — does any Phase 3 entry gate beat the honest baseline?

Runs the ONE honest production engine (_run_live_engine = deleveraged V3 +
cash wall + VIX sizing + earnings gate + real MY commissions) on the pinned
10-name universe and the current .env params, then layers the Phase 3
experiments on top — all on the SAME prefetched cache so the only difference
is the gate under test:

  baseline      production engine as-is (regime+earnings+ML already on)
  +RS(0)        Phase 3-A: require stock 20d return >= SPY 20d return
  +RS(+2pp)     stricter RS: must beat SPY by >= 2 percentage points
  +SOXX         Phase 3-B: pause new entries when SOXX EMA20 <= EMA50
  +RS(0)+SOXX   both

Reported per 180d & 360d window: net PnL, $/day, MTM drawdown, #trades, and how
many entries each gate blocked. NOTHING gets recommended for live until a combo
clearly beats baseline on BOTH windows (the 2b/2c lesson: most add-ons lose).

Run:  .venv/bin/python3 scripts/phase3_validate.py
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
log = logging.getLogger("phase3")

PINNED10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]


def base_cfg(days: int) -> BacktestConfig:
    """Production-matching config: current .env params, pinned 10, ML on / MR off."""
    return BacktestConfig(
        days=days,
        timeframe=settings.timeframe,
        threshold=settings.entry_threshold,
        tickers=list(PINNED10),
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,   # 0.40 after Phase 2a
        max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        # production funnel (defaults already match, set explicit for clarity)
        apply_ml_gate=True,
        apply_mr_strategy=False,
    )


# Phase 3 flag overlays applied on top of the honest-engine config.
COMBOS = {
    "baseline":     dict(),
    "+RS(0)":       dict(apply_rs_gate=True, rs_min_pct=0.0),
    "+RS(+2pp)":    dict(apply_rs_gate=True, rs_min_pct=2.0),
    "+SOXX":        dict(apply_sector_regime_gate=True),
    "+RS(0)+SOXX":  dict(apply_rs_gate=True, rs_min_pct=0.0,
                         apply_sector_regime_gate=True),
}


def run_combo(base: BacktestConfig, overlay: dict, cache: dict, days: int) -> dict:
    """Mirror _run_live_engine exactly, plus the Phase 3 overlay under test."""
    cfg = replace(base, apply_vix_sizing=True, apply_earnings_gate=True,
                  use_realistic_commission=True, **overlay)
    res = simulate_v3(cfg, cache, enforce_cash=True, rich_metrics=True)
    m = res["metrics"]
    net = m.get("net_pnl_usd", 0.0)
    return {
        "net": net,
        "per_day": net / days,
        "dd": m.get("max_dd_mtm_pct", 0.0),
        "trades": m.get("total_trades", 0),
        "win": m.get("win_rate_pct", 0.0),
        "sortino": m.get("sortino_ratio", 0.0),
        "rs_blocked": m.get("n_rs_blocked", 0),
        "soxx_blocked": m.get("n_sector_regime_blocked", 0),
        "cash_blocked": m.get("n_cash_limited_entries", 0),
    }


def main() -> None:
    windows = [180, 360]
    all_results: dict[int, dict] = {}

    for days in windows:
        log.info("=== prefetch %dd (SPY + SOXX + per-ticker daily) ===", days)
        # Prefetch with BOTH gates enabled so the cache carries SPY + SOXX +
        # per-ticker daily; the per-combo runs just toggle whether each bites.
        pf_cfg = replace(base_cfg(days), apply_rs_gate=True,
                         apply_sector_regime_gate=True)
        cache = prefetch_data(pf_cfg)
        log.info("[%dd] cache: spy=%s soxx=%s tickers=%d", days,
                 cache.get("spy_daily") is not None,
                 cache.get("soxx_daily") is not None,
                 len(cache["per_ticker"]))

        base = base_cfg(days)
        results = {}
        for name, overlay in COMBOS.items():
            log.info("[%dd] running combo: %s", days, name)
            results[name] = run_combo(base, overlay, cache, days)
        all_results[days] = results

    # ---- report ----
    print("\n" + "=" * 88)
    print("PHASE 3 VALIDATION — honest cash engine (pinned 10, mpp=%.2f, thr=%.0f, "
          "tp=%.1f sl=%.1f)" % (settings.max_position_pct, settings.entry_threshold,
                                settings.tp_atr_mult, settings.sl_atr_mult))
    print("=" * 88)
    hdr = f"{'combo':<14}{'$/day':>9}{'net$':>10}{'MTM-DD%':>9}{'trades':>8}{'win%':>7}{'sortino':>9}{'blocked(rs/soxx)':>18}"
    for days in windows:
        print(f"\n--- {days}d ---")
        print(hdr)
        base_pd = all_results[days]["baseline"]["per_day"]
        for name, r in all_results[days].items():
            delta = r["per_day"] - base_pd
            mark = "" if name == "baseline" else f"  ({delta:+.1f}/day vs base)"
            blocked = f"{r['rs_blocked']}/{r['soxx_blocked']}"
            print(f"{name:<14}{r['per_day']:>9.2f}{r['net']:>10.0f}{r['dd']:>9.2f}"
                  f"{r['trades']:>8}{r['win']:>7.1f}{r['sortino']:>9.2f}{blocked:>18}{mark}")

    # ---- verdict ----
    print("\n" + "=" * 88)
    print("VERDICT (must beat baseline on BOTH windows to earn a live recommendation)")
    print("=" * 88)
    for name in COMBOS:
        if name == "baseline":
            continue
        d180 = all_results[180][name]["per_day"] - all_results[180]["baseline"]["per_day"]
        d360 = all_results[360][name]["per_day"] - all_results[360]["baseline"]["per_day"]
        both_win = d180 > 0 and d360 > 0
        tag = "✅ WIN both" if both_win else ("⚠ mixed" if (d180 > 0 or d360 > 0) else "❌ loses both")
        print(f"  {name:<14} 180d {d180:+.2f}/day | 360d {d360:+.2f}/day  → {tag}")
    print()


if __name__ == "__main__":
    main()
