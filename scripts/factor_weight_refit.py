"""factor_weight_refit (#6) — does LEARNING factor weights from real outcomes
beat the hand+Optuna-tuned weights?

Honest prior: the full 36-feature ML model is inert (holdout AUC≈0.5) on this
pinned mega-cap universe. This tests the narrower question: if we fit weights on
just the factor sub-scores (the trend/momentum/volume/pattern/adx/vwap signals
that indicators.evaluate actually weights), do learned weights carry more
signal than the fixed ones? We compare holdout AUC of:
  • fixed-weight composite (current HOUR_1_WEIGHTS)
  • logistic-learned-weight composite (refit on TP-before-SL labels)

If the learned AUC isn't meaningfully > fixed AUC (and > 0.55), factor-weight
self-learning is the same dead end as ML — report it, don't add the complexity.

Run: .venv/bin/python3 scripts/factor_weight_refit.py
"""
from __future__ import annotations
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TIMEFRAME", "HOUR_1")

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from src.ml.train import _fetch_history, _time_split
from src.ml.dataset import build_dataset, LabelConfig
from src.indicators import HOUR_1_WEIGHTS

PINNED10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]

# Map each HOUR_1 factor → the ml feature(s) that proxy its sub-score.
FACTOR_PROXIES = {
    "trend":    ["ema21_over_ema50", "px_over_ema20"],
    "momentum": ["rsi_14", "macd_hist"],
    "volume":   ["vol_ratio_20"],
    "pattern":  ["bb_pct"],
    "adx":      ["adx_14"],
    "vwap":     ["vwap_dist_pct"],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
log = logging.getLogger("refit")


def main() -> None:
    log.info("fetch 10 tickers × 365d HOUR_1")
    klines = _fetch_history(PINNED10, days=365, timeframe="HOUR_1")
    if not klines:
        raise SystemExit("no klines — OpenD down?")
    X, y, _s, _t = build_dataset(klines, LabelConfig())
    log.info("dataset %d rows, pos-rate %.1f%%", len(X), y.mean() * 100)

    feats = [f for fs in FACTOR_PROXIES.values() for f in fs if f in X.columns]
    Xf = X[feats]
    Xtr, ytr, Xval, yval, Xte, yte = _time_split(Xf, y)

    # Fixed-weight composite proxy: standardize each proxy feature, weight by the
    # current factor weight, sum. (A faithful stand-in for the hand-tuned score.)
    mu, sd = Xtr.mean(), Xtr.std().replace(0, 1)
    def composite(df):
        z = (df - mu) / sd
        s = np.zeros(len(df))
        for factor, cols in FACTOR_PROXIES.items():
            w = HOUR_1_WEIGHTS.get(factor, 0)
            for c in cols:
                if c in z.columns:
                    s = s + w * z[c].values / len(cols)
        return s
    fixed_te = composite(Xte)
    auc_fixed = roc_auc_score(yte, fixed_te) if yte.nunique() > 1 else float("nan")

    # Learned weights: logistic regression on the proxy features.
    lr = LogisticRegression(max_iter=1000, C=1.0)
    lr.fit(Xtr, ytr)
    auc_learned = roc_auc_score(yte, lr.predict_proba(Xte)[:, 1]) if yte.nunique() > 1 else float("nan")

    # Learned per-factor weight = sum of |coef| of its proxy features, normalized to 90.
    coef = dict(zip(feats, np.abs(lr.coef_[0])))
    raw = {f: sum(coef.get(c, 0) for c in cols) for f, cols in FACTOR_PROXIES.items()}
    tot = sum(raw.values()) or 1
    learned_w = {f: round(v / tot * 90, 1) for f, v in raw.items()}

    print("\n" + "=" * 78)
    print("FACTOR-WEIGHT REFIT (#6) — learned vs fixed, holdout AUC")
    print("=" * 78)
    print(f"  holdout rows: {len(Xte)}")
    print(f"  AUC fixed-weight composite : {auc_fixed:.4f}")
    print(f"  AUC logistic-learned       : {auc_learned:.4f}")
    print(f"  ΔAUC (learned − fixed)     : {auc_learned - auc_fixed:+.4f}")
    print(f"\n  current HOUR_1 weights : {HOUR_1_WEIGHTS}")
    print(f"  learned weights (→90)  : {learned_w}")
    print("\n" + "=" * 78)
    if auc_learned > 0.55 and (auc_learned - auc_fixed) > 0.03:
        print("  ✅ Learned weights carry meaningfully more signal — worth backtesting "
              "+ proposing via approval.")
    else:
        print("  ❌ No meaningful edge from learning weights (AUC ≈ coin flip / ≈ fixed).")
        print("     Same dead end as the 36-feature ML model on this universe.")
        print("     → KEEP the fixed hand+Optuna weights; don't add weight-learning complexity.")
    print()


if __name__ == "__main__":
    main()
