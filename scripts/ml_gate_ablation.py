"""ml_gate_ablation — is the ML veto gate earning its keep, or vetoing winners?

Phase 4-2. The insider ablation surfaced a red flag: the production model's
holdout AUC ≈ 0.47 (< 0.5 = worse than a coin flip on recent data). If the
model is near-random, the ML veto (apply_ml_gate=True, proba<0.35 → block)
could be filtering out perfectly good setups for nothing.

This runs the ONE honest cash engine with the ML gate ON vs OFF on the pinned
10 names, 180d & 360d. Everything else is identical (same .env params, regime +
earnings + VIX + real commissions all on). The only difference is whether the
model gets a veto.

  ML-ON   = production today (apply_ml_gate=True, uses data/ml/model.joblib)
  ML-OFF  = apply_ml_gate=False (rule + AI funnel only, no model veto)

Decision rule (same bar as everything else): keep the gate only if ON beats OFF
on $/day on BOTH windows. If OFF wins, the model is hurting and should be
disabled (ML_ENABLED=false) until retrained into something with real edge.

Uses the current saved model untouched. Run:
    .venv/bin/python3 scripts/ml_gate_ablation.py
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
from src.ml import predict as ml_predict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("mlgate")

PINNED10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]


# Three variants: gate fully off, veto-only (old backtest behaviour), and
# veto + conviction sizing (mirrors LIVE main.py — the bit the first ablation missed).
VARIANTS = {
    "ML-OFF":    dict(apply_ml_gate=False),
    "ML-VETO":   dict(apply_ml_gate=True),
    "ML-FULL":   dict(apply_ml_gate=True, apply_ml_conviction_sizing=True),
}


def base_cfg(days: int) -> BacktestConfig:
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=list(PINNED10), account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        apply_mr_strategy=False,
    )


def run(days: int, overlay: dict, cache: dict) -> dict:
    cfg = replace(base_cfg(days), apply_vix_sizing=True,
                  apply_earnings_gate=True, use_realistic_commission=True, **overlay)
    res = simulate_v3(cfg, cache, enforce_cash=True, rich_metrics=True)
    m = res["metrics"]
    net = m.get("net_pnl_usd", 0.0)
    return {
        "per_day": net / days, "net": net, "dd": m.get("max_dd_mtm_pct", 0.0),
        "trades": m.get("total_trades", 0), "win": m.get("win_rate_pct", 0.0),
        "sortino": m.get("sortino_ratio", 0.0),
        "pf": m.get("profit_factor", 0.0),
        "halved": m.get("n_ml_conviction_halved", 0),
    }


def main() -> None:
    meta = ml_predict.model_meta()
    print(f"\nProduction model: trained {meta.get('trained_at','?')}, "
          f"holdout AUC {meta.get('auc_holdout','?')}, "
          f"{len(meta.get('feature_names',[]))} features")
    if not ml_predict.is_available():
        raise SystemExit("no model.joblib — ML gate ablation needs the saved model")

    results = {}
    for days in (180, 360):
        log.info("=== prefetch %dd ===", days)
        cache = prefetch_data(base_cfg(days))
        results[days] = {}
        for tag, overlay in VARIANTS.items():
            log.info("[%dd] running %s", days, tag)
            results[days][tag] = run(days, overlay, cache)

    print("\n" + "=" * 92)
    print("ML GATE ABLATION — honest cash engine, pinned 10 (mpp=%.2f thr=%.0f tp=%.1f sl=%.1f)"
          % (settings.max_position_pct, settings.entry_threshold,
             settings.tp_atr_mult, settings.sl_atr_mult))
    print("  ML-OFF = no model | ML-VETO = veto only (old bt) | ML-FULL = veto + conviction sizing (=LIVE)")
    print("=" * 92)
    hdr = (f"{'window':<8}{'variant':<10}{'$/day':>9}{'net$':>10}{'MTM-DD%':>9}"
           f"{'trades':>8}{'win%':>7}{'PF':>7}{'sortino':>9}{'halved':>8}")
    deltas = {"ML-VETO": [], "ML-FULL": []}
    for days in (180, 360):
        print(f"\n--- {days}d ---")
        print(hdr)
        off = results[days]["ML-OFF"]
        for tag, r in results[days].items():
            print(f"{str(days)+'d':<8}{tag:<10}{r['per_day']:>9.2f}{r['net']:>10.0f}"
                  f"{r['dd']:>9.2f}{r['trades']:>8}{r['win']:>7.1f}{r['pf']:>7.2f}"
                  f"{r['sortino']:>9.2f}{r['halved']:>8}")
        for tag in ("ML-VETO", "ML-FULL"):
            deltas[tag].append(results[days][tag]["per_day"] - off["per_day"])
        print(f"  → vs ML-OFF: VETO {deltas['ML-VETO'][-1]:+.2f}/day | "
              f"FULL {deltas['ML-FULL'][-1]:+.2f}/day "
              f"(FULL halved {results[days]['ML-FULL']['halved']} entries)")

    print("\n" + "=" * 92)
    print("VERDICT  (does the live ML model — veto + conviction sizing — beat running no model?)")
    print("=" * 92)
    for tag in ("ML-VETO", "ML-FULL"):
        d = deltas[tag]
        helps = all(x > 0 for x in d)
        hurts = all(x < 0 for x in d)
        verdict = ("✅ beats ML-OFF both windows" if helps
                   else "❌ loses to ML-OFF both windows" if hurts
                   else "⚠ mixed")
        print(f"  {tag:<9} 180d {d[0]:+.2f}/day | 360d {d[1]:+.2f}/day  → {verdict}")
    full = deltas["ML-FULL"]
    print()
    if all(x < 0 for x in full):
        print("  ❌ The LIVE ML model (ML-FULL) loses to running no model on BOTH windows.")
        print("     → Recommend ML_ENABLED=false until the model is retrained with real edge")
        print("       (holdout AUC ~0.5 = coin flip). Disabling stops it half-sizing good trades.")
    elif all(x > 0 for x in full):
        print("  ✅ The LIVE ML model (ML-FULL) earns its place — keep ML_ENABLED=true.")
    else:
        print("  ⚠ Mixed — the live ML model helps one window, hurts the other. Marginal at best;")
        print("    weigh against the AUC~0.5 red flag before keeping it on.")
    print()


if __name__ == "__main__":
    main()
