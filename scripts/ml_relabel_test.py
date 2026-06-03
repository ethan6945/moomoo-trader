"""ml_relabel_test (#11) — is ML's inertness just a LABEL MISMATCH?

The saved model is trained with LabelConfig(tp=1.5, sl=2.0, horizon=65) — but the
live bot exits at tp=8.0 ATR / sl=3.5 ATR over ~7 trading days (≈49 HOUR_1 bars).
So the model predicts an outcome the bot never trades. This tests whether
RELABELING to the real exits revives predictive power:
  • current label   (tp 1.5 / sl 2.0 / h 65)
  • corrected label (tp 8.0 / sl 3.5 / h 49)  ← matches live exits

If the corrected-label holdout AUC clears ~0.55, ML revival is viable (worth a
retrain + approval-gated deploy). If it's still ≈0.5, ML stays dead and we stop
spending on it. Honest either way.

Run: .venv/bin/python3 scripts/ml_relabel_test.py
"""
from __future__ import annotations
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TIMEFRAME", "HOUR_1")

import xgboost as xgb
from sklearn.metrics import roc_auc_score

from src.ml.train import _fetch_history, _time_split
from src.ml.dataset import build_dataset, LabelConfig

PINNED10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
XGB = {"objective": "binary:logistic", "eval_metric": "logloss", "n_estimators": 400,
       "max_depth": 5, "learning_rate": 0.05, "min_child_weight": 5, "subsample": 0.85,
       "colsample_bytree": 0.85, "reg_alpha": 0.1, "reg_lambda": 1.0,
       "random_state": 42, "early_stopping_rounds": 30}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("relabel")


def auc_for(klines, label_cfg) -> tuple[float, float]:
    X, y, _s, _t = build_dataset(klines, label_cfg)
    Xtr, ytr, Xv, yv, Xte, yte = _time_split(X, y)
    m = xgb.XGBClassifier(**XGB)
    m.fit(Xtr, ytr, eval_set=[(Xv, yv)], verbose=False)
    p = m.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, p) if yte.nunique() > 1 else float("nan")
    return auc, float(y.mean())


def main() -> None:
    klines = _fetch_history(PINNED10, days=365, timeframe="HOUR_1")
    if not klines:
        raise SystemExit("no klines — OpenD down?")
    auc_cur, pr_cur = auc_for(klines, LabelConfig(horizon_bars=65, tp_atr_mult=1.5, sl_atr_mult=2.0))
    auc_fix, pr_fix = auc_for(klines, LabelConfig(horizon_bars=49, tp_atr_mult=8.0, sl_atr_mult=3.5))

    print("\n" + "=" * 74)
    print("ML RELABEL TEST (#11) — does matching the label to live exits revive AUC?")
    print("=" * 74)
    print(f"  current label  (tp1.5/sl2.0/h65): holdout AUC {auc_cur:.4f}  pos-rate {pr_cur:.0%}")
    print(f"  corrected label(tp8.0/sl3.5/h49): holdout AUC {auc_fix:.4f}  pos-rate {pr_fix:.0%}")
    print("=" * 74)
    if auc_fix >= 0.55:
        print("  ✅ Relabeling REVIVES signal — ML revival is viable: retrain on the")
        print("     corrected label, validate $/day on the honest engine, deploy via approval.")
    else:
        print("  ❌ Still ≈ coin flip even with the correct label — ML has no edge on this")
        print("     universe/features. KEEP it disabled; pursue alpha elsewhere (universe, alt-data).")
    print()


if __name__ == "__main__":
    main()
