"""insider_ablation — does the SEC Form 4 insider feature carry real alpha?

Phase 4-1. Ablates `insider_30d_net_log` from the ML feature set and measures
the cost two ways, on the PINNED 10-name universe:

  Part 1 — model quality: train two XGBoost models on IDENTICAL data/split/seed,
           one WITH the feature and one WITHOUT. Compare holdout AUC / logloss /
           accuracy, and report where insider ranks in the full model's gain.

  Part 2 — honest $/day: run the ONE honest cash engine (_run_live_engine) with
           each model as the ML gate, on 180d & 360d. This is the number we
           actually decide on (model AUC ≠ trading PnL — see the v2-feature note
           in features.py where +AUC cost −10% $/day).

Zero production risk: variant models are saved to data/ml/ablation/ and the
engine is pointed at them by monkeypatching predict.MODEL_FILE + FEATURE_NAMES.
The live data/ml/model.joblib is never touched.

Run:  .venv/bin/python3 scripts/insider_ablation.py
"""
from __future__ import annotations
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TIMEFRAME", "HOUR_1")

import joblib
import xgboost as xgb
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score

from src.config import settings
from src.ml.train import _fetch_history, _time_split
from src.ml.dataset import LabelConfig, build_dataset
from src.ml import features as feat_mod
from src.ml import predict as predict_mod
from src.backtest import BacktestConfig, prefetch_data
from src.backtest_v3 import simulate_v3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("ablation")

PINNED10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
INSIDER = "insider_30d_net_log"
ABL_DIR = settings.root / "data" / "ml" / "ablation"

# XGBoost params — copied from train.train_model so the ablation models match
# how production is trained (only the feature set differs).
XGB_PARAMS = {
    "objective": "binary:logistic", "eval_metric": "logloss",
    "n_estimators": 400, "max_depth": 5, "learning_rate": 0.05,
    "min_child_weight": 5, "subsample": 0.85, "colsample_bytree": 0.85,
    "reg_alpha": 0.1, "reg_lambda": 1.0, "random_state": 42,
    "early_stopping_rounds": 30,
}


def base_cfg(days: int) -> BacktestConfig:
    """Production-matching honest-engine config (same as phase3_validate)."""
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=list(PINNED10), account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        apply_ml_gate=True, apply_mr_strategy=False,
    )


def train_variant(X, y, cols: list[str], tag: str) -> tuple[object, dict]:
    """Train one XGBoost variant on X[cols] with the shared split/params."""
    Xc = X[cols]
    Xtr, ytr, Xval, yval, Xte, yte = _time_split(Xc, y)
    model = xgb.XGBClassifier(**XGB_PARAMS)
    model.fit(Xtr, ytr, eval_set=[(Xval, yval)], verbose=False)
    p = model.predict_proba(Xte)[:, 1]
    yhat = (p > 0.5).astype(int)
    m = {
        "tag": tag, "n_features": len(cols), "n_rows": len(Xc),
        "n_holdout": len(Xte),
        "auc": float(roc_auc_score(yte, p)) if yte.nunique() > 1 else float("nan"),
        "logloss": float(log_loss(yte, p, labels=[0, 1])),
        "accuracy": float(accuracy_score(yte, yhat)),
        "importance": dict(sorted(zip(cols, model.feature_importances_.tolist()),
                                  key=lambda x: -x[1])),
        "feature_names": list(cols),
    }
    return model, m


def run_honest(model, cols, caches: dict) -> dict:
    """Point the engine's ML gate at `model`/`cols`, run honest 180d+360d."""
    ABL_DIR.mkdir(parents=True, exist_ok=True)
    mpath = ABL_DIR / "variant_model.joblib"
    mmeta = ABL_DIR / "variant_meta.json"
    joblib.dump(model, mpath)
    mmeta_payload = {"feature_names": list(cols)}
    mmeta.write_text(json.dumps(mmeta_payload))

    # Monkeypatch the inference layer to use this variant (no production touch).
    orig_model_file = predict_mod.MODEL_FILE
    orig_meta_file = predict_mod.META_FILE
    orig_feats = predict_mod.FEATURE_NAMES
    predict_mod.MODEL_FILE = mpath
    predict_mod.META_FILE = mmeta
    predict_mod.FEATURE_NAMES = list(cols)
    predict_mod._model = None          # force reload from the variant path
    predict_mod._meta = {}
    out = {}
    try:
        for days, cache in caches.items():
            cfg = replace(base_cfg(days), apply_vix_sizing=True,
                          apply_earnings_gate=True, use_realistic_commission=True)
            res = simulate_v3(cfg, cache, enforce_cash=True, rich_metrics=True)
            mm = res["metrics"]
            net = mm.get("net_pnl_usd", 0.0)
            out[days] = {
                "per_day": net / days, "net": net,
                "dd": mm.get("max_dd_mtm_pct", 0.0),
                "trades": mm.get("total_trades", 0),
                "win": mm.get("win_rate_pct", 0.0),
                "sortino": mm.get("sortino_ratio", 0.0),
            }
    finally:
        predict_mod.MODEL_FILE = orig_model_file
        predict_mod.META_FILE = orig_meta_file
        predict_mod.FEATURE_NAMES = orig_feats
        predict_mod._model = None
        predict_mod._meta = {}
    return out


def main() -> None:
    all_feats = list(feat_mod.FEATURE_NAMES)
    if INSIDER not in all_feats:
        raise SystemExit(f"{INSIDER} not in FEATURE_NAMES — nothing to ablate")
    cols_with = list(all_feats)
    cols_without = [c for c in all_feats if c != INSIDER]

    # ---- shared dataset (fetch once, build once) ----
    log.info("=== fetch klines: 10 tickers × 365d HOUR_1 ===")
    klines = _fetch_history(PINNED10, days=365, timeframe="HOUR_1")
    if not klines:
        raise SystemExit("no klines fetched — is OpenD up?")
    log.info("=== build dataset ===")
    X, y, _syms, _ts = build_dataset(klines, LabelConfig())
    log.info("dataset: %d rows, %d features, pos-rate %.1f%%",
             len(X), X.shape[1], y.mean() * 100)

    # ---- Part 1: model quality ----
    log.info("=== train WITH insider (%d feats) ===", len(cols_with))
    m_with, q_with = train_variant(X, y, cols_with, "WITH")
    log.info("=== train WITHOUT insider (%d feats) ===", len(cols_without))
    m_without, q_without = train_variant(X, y, cols_without, "WITHOUT")

    ins_rank = list(q_with["importance"]).index(INSIDER) + 1
    ins_imp = q_with["importance"][INSIDER]

    # ---- Part 2: honest backtest (prefetch caches once, reuse for both) ----
    caches = {}
    for days in (180, 360):
        log.info("=== prefetch %dd (with insider join) ===", days)
        caches[days] = prefetch_data(base_cfg(days))
    log.info("=== honest backtest: WITH insider ===")
    bt_with = run_honest(m_with, cols_with, caches)
    log.info("=== honest backtest: WITHOUT insider ===")
    bt_without = run_honest(m_without, cols_without, caches)

    # ---- report ----
    print("\n" + "=" * 86)
    print("INSIDER FEATURE ABLATION — pinned 10, honest cash engine")
    print("=" * 86)
    print(f"\nPart 1 — model quality (holdout, {q_with['n_holdout']} rows, identical split/seed)")
    print(f"  {'variant':<10}{'#feat':>7}{'AUC':>9}{'logloss':>10}{'accuracy':>10}")
    for q in (q_with, q_without):
        print(f"  {q['tag']:<10}{q['n_features']:>7}{q['auc']:>9.4f}"
              f"{q['logloss']:>10.4f}{q['accuracy']:>10.4f}")
    d_auc = q_with["auc"] - q_without["auc"]
    d_ll = q_without["logloss"] - q_with["logloss"]   # positive = WITH is better
    print(f"  insider rank in WITH model gain: #{ins_rank}/{len(cols_with)} "
          f"(importance {ins_imp:.4f})")
    print(f"  ΔAUC (WITH−WITHOUT): {d_auc:+.4f} | Δlogloss (WITHOUT−WITH): {d_ll:+.4f} "
          f"(positive ⇒ insider helps)")

    print(f"\nPart 2 — honest $/day (THE deciding number)")
    print(f"  {'window':<8}{'WITH $/day':>13}{'WITHOUT $/day':>15}{'Δ(WITH−WO)':>13}"
          f"{'WITH DD%':>11}{'WO DD%':>9}{'WITH trades':>13}{'WO trades':>11}")
    verdict_both = []
    for days in (180, 360):
        w, wo = bt_with[days], bt_without[days]
        d = w["per_day"] - wo["per_day"]
        verdict_both.append(d)
        print(f"  {str(days)+'d':<8}{w['per_day']:>13.2f}{wo['per_day']:>15.2f}"
              f"{d:>+13.2f}{w['dd']:>11.2f}{wo['dd']:>9.2f}"
              f"{w['trades']:>13}{wo['trades']:>11}")

    print("\n" + "=" * 86)
    print("VERDICT")
    print("=" * 86)
    helps_both = all(d > 0 for d in verdict_both)
    hurts_both = all(d < 0 for d in verdict_both)
    if helps_both:
        msg = "✅ insider feature EARNS its place — removing it costs $/day on BOTH windows. KEEP."
    elif hurts_both:
        msg = "❌ insider feature is a DRAG — removing it RAISES $/day on both windows. DROP from FEATURE_NAMES."
    else:
        msg = ("⚠ mixed — insider helps one window, hurts the other. Likely noise; "
               "lean toward DROP for simplicity unless the helped window matters more.")
    print(f"  180d Δ={verdict_both[0]:+.2f}/day | 360d Δ={verdict_both[1]:+.2f}/day")
    print(f"  {msg}")
    print()


if __name__ == "__main__":
    main()
