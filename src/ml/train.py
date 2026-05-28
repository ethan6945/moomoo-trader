"""Train an XGBoost classifier on labeled historical bars.

Pipeline:
  1. Pull historical klines from MooMoo for every ticker in watchlist.
  2. Build (X, y) dataset (see dataset.py).
  3. Time-series split: oldest 70% train / next 15% val / newest 15% holdout.
  4. Train XGBoost with early stopping on val.
  5. Report metrics on holdout + save model + feature importance.

CLI:
    python -m src.ml.train                 # default: 12 months, current timeframe
    python -m src.ml.train --days 365 --tickers AAPL MSFT NVDA
    python -m src.ml.train --timeframe DAILY
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import (
    accuracy_score, classification_report, log_loss, roc_auc_score
)

from ..config import settings
from .dataset import LabelConfig, build_dataset
from .features import FEATURE_NAMES

log = logging.getLogger(__name__)

ML_DIR = settings.root / "data" / "ml"
MODEL_FILE = ML_DIR / "model.joblib"
META_FILE = ML_DIR / "model_meta.json"
WATCHLIST_FILE = settings.root / "config" / "watchlist.json"


def _fetch_history(tickers: list[str], days: int, timeframe: str) -> dict[str, pd.DataFrame]:
    """Fetch klines for each ticker. Same throttle as backtester."""
    from ..moomoo_client import MoomooClient
    from moomoo import KLType

    kltype = KLType.K_60M if timeframe == "HOUR_1" else KLType.K_DAY
    bars_per_day = 6.5 if timeframe == "HOUR_1" else 1
    total_bars = int(days * bars_per_day) + 100   # warmup buffer

    c = MoomooClient()
    out: dict[str, pd.DataFrame] = {}
    try:
        for sym in tickers:
            try:
                df = c.get_kline(sym, bars=total_bars, ktype=kltype)
                if len(df) > 100:
                    out[sym] = df
                    log.info("fetched %s: %d bars", sym, len(df))
                else:
                    log.warning("fetched %s: only %d bars — skipped", sym, len(df))
            except Exception as e:
                log.warning("fetch failed %s: %s", sym, e)
    finally:
        c.close()
    return out


def _time_split(X: pd.DataFrame, y: pd.Series, train_pct=0.70, val_pct=0.15):
    n = len(X)
    n_train = int(n * train_pct)
    n_val = int(n * val_pct)
    return (
        X.iloc[:n_train], y.iloc[:n_train],
        X.iloc[n_train:n_train + n_val], y.iloc[n_train:n_train + n_val],
        X.iloc[n_train + n_val:], y.iloc[n_train + n_val:],
    )


def train_model(
    klines: dict[str, pd.DataFrame],
    label_cfg: LabelConfig | None = None,
    xgb_params: dict | None = None,
) -> tuple[xgb.XGBClassifier, dict]:
    """Train XGBoost, return (model, metrics). Saves nothing — caller persists."""
    label_cfg = label_cfg or LabelConfig()
    X, y, syms, _ts = build_dataset(klines, label_cfg)
    log.info("dataset size: %d rows, positive rate %.1f%%", len(X), y.mean() * 100 if len(y) else 0)

    if len(X) < 500:
        raise RuntimeError(f"dataset too small ({len(X)} rows) — need ≥ 500. "
                           "Try --days higher or more tickers.")

    X_tr, y_tr, X_val, y_val, X_te, y_te = _time_split(X, y)
    log.info("split: train=%d val=%d holdout=%d", len(X_tr), len(X_val), len(X_te))

    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "n_estimators": 400,
        "max_depth": 5,
        "learning_rate": 0.05,
        "min_child_weight": 5,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "reg_alpha": 0.1,
        "reg_lambda": 1.0,
        "random_state": 42,
        "early_stopping_rounds": 30,
    }
    if xgb_params:
        params.update(xgb_params)

    model = xgb.XGBClassifier(**params)
    model.fit(
        X_tr, y_tr,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )

    # Holdout metrics
    p_te = model.predict_proba(X_te)[:, 1]
    yhat = (p_te > 0.5).astype(int)
    metrics = {
        "n_rows_total": len(X),
        "n_train": len(X_tr),
        "n_val": len(X_val),
        "n_holdout": len(X_te),
        "positive_rate_train": float(y_tr.mean()),
        "positive_rate_holdout": float(y_te.mean()),
        "accuracy_holdout": float(accuracy_score(y_te, yhat)),
        "auc_holdout": float(roc_auc_score(y_te, p_te)) if y_te.nunique() > 1 else float("nan"),
        "logloss_holdout": float(log_loss(y_te, p_te, labels=[0, 1])),
        "best_iteration": int(model.best_iteration) if hasattr(model, "best_iteration") else None,
        "feature_importance": dict(
            sorted(zip(FEATURE_NAMES, model.feature_importances_.tolist()),
                   key=lambda x: -x[1])
        ),
        "label_config": {
            "horizon_bars": label_cfg.horizon_bars,
            "tp_atr_mult": label_cfg.tp_atr_mult,
            "sl_atr_mult": label_cfg.sl_atr_mult,
            "atr_period": label_cfg.atr_period,
        },
        "trained_at": datetime.utcnow().isoformat() + "Z",
        "feature_names": FEATURE_NAMES,
    }
    log.info("holdout: acc=%.3f  auc=%.3f  logloss=%.3f",
             metrics["accuracy_holdout"], metrics["auc_holdout"], metrics["logloss_holdout"])
    return model, metrics


def save_model(model: xgb.XGBClassifier, metrics: dict) -> None:
    ML_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_FILE)
    META_FILE.write_text(json.dumps(metrics, indent=2))
    log.info("saved → %s", MODEL_FILE)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")

    ap = argparse.ArgumentParser(description="Train moomoo-trader ML model")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--timeframe", default=None,
                    help="HOUR_1 or DAILY (default: from .env)")
    ap.add_argument("--tickers", nargs="*", help="Symbols (default: watchlist)")
    ap.add_argument("--horizon", type=int, default=65,
                    help="Future bars defining outcome (default 65 ≈ MAX_HOLD_DAYS at HOUR_1)")
    ap.add_argument("--tp_atr", type=float, default=1.5,
                    help="TP distance in ATR multiples (matches live executor)")
    ap.add_argument("--sl_atr", type=float, default=2.0,
                    help="SL distance in ATR multiples (matches live executor)")
    args = ap.parse_args()

    timeframe = args.timeframe or settings.timeframe
    os.environ["TIMEFRAME"] = timeframe
    tickers = args.tickers or json.loads(WATCHLIST_FILE.read_text())["tickers"]

    log.info("training on %d tickers, %d days, timeframe=%s", len(tickers), args.days, timeframe)
    t0 = time.time()
    klines = _fetch_history(tickers, days=args.days, timeframe=timeframe)
    log.info("fetch took %.1fs (%d tickers had data)", time.time() - t0, len(klines))

    if not klines:
        raise SystemExit("No data fetched — check OpenD connection.")

    lc = LabelConfig(horizon_bars=args.horizon,
                     tp_atr_mult=args.tp_atr, sl_atr_mult=args.sl_atr)
    t1 = time.time()
    model, metrics = train_model(klines, lc)
    log.info("train took %.1fs", time.time() - t1)

    save_model(model, metrics)

    print()
    print("=" * 60)
    print(f"  TRAINED  |  {len(klines)} tickers  |  {args.days} days  |  {timeframe}")
    print("=" * 60)
    print(f"  Holdout accuracy : {metrics['accuracy_holdout']*100:.1f}%")
    print(f"  Holdout AUC      : {metrics['auc_holdout']:.3f}")
    print(f"  Positive rate    : {metrics['positive_rate_holdout']*100:.1f}%")
    print(f"  Dataset rows     : {metrics['n_rows_total']:,}")
    print()
    print("  Top 8 features:")
    for name, imp in list(metrics["feature_importance"].items())[:8]:
        bar = "█" * int(imp * 50)
        print(f"    {name:<22} {imp:.3f}  {bar}")
    print()
    print(f"  Model saved → {MODEL_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
