"""LightGBM vs XGBoost shoot-out + top-10 specialist test.

Fetches 730 days of data ONCE, joins VIX history from yfinance, then trains
4 model variants with the new macro/time-of-day feature set:

  V1  XGBoost  | 19 tickers (current watchlist)  | baseline
  V2  LightGBM | 19 tickers                      | LGB drop-in replacement
  V3  LightGBM | top-10 winners only             | specialist (smaller, cleaner)
  V4  LightGBM | 19 tickers, 730d, more trees    | bigger model on more data

Reports holdout AUC + accuracy + which model wins.
Saves the winning model to data/ml/model.joblib so live picks it up.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import xgboost as xgb  # noqa: E402
import lightgbm as lgb  # noqa: E402
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score  # noqa: E402

from src.config import settings  # noqa: E402
from src.ml.dataset import LabelConfig, build_dataset  # noqa: E402
from src.ml.features import FEATURE_NAMES  # noqa: E402
from src.ml.train import _fetch_history, _time_split  # noqa: E402


def _fetch_vix_history(days: int) -> pd.DataFrame:
    """Pull ^VIX daily history via yfinance. MooMoo's API can't serve VIX kline
    but its snapshot works — and yfinance fills the historical gap free.

    Returns a DataFrame indexed by date (UTC midnight) with a 'vix' column.
    Callers reindex/forward-fill into per-ticker HOUR_1 indices.
    """
    import yfinance as yf
    from datetime import date, timedelta
    end_d = date.today()
    start_d = end_d - timedelta(days=days + 30)   # extra buffer
    df = yf.download("^VIX", start=start_d, end=end_d, interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        log.warning("VIX history empty — features will fall back to 15.0")
        return pd.DataFrame()
    # yfinance returns multi-index columns sometimes; normalise.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    out = pd.DataFrame({"vix": df["Close"]}, index=df.index)
    log.info("Fetched %d VIX daily bars (yfinance, range %s → %s)",
             len(out), out.index[0].date(), out.index[-1].date())
    return out


def _enrich_with_vix(klines: dict[str, pd.DataFrame], vix_df: pd.DataFrame
                     ) -> dict[str, pd.DataFrame]:
    """Forward-fill daily VIX into each ticker's HOUR_1 df.

    Implementation note: VIX is naturally a daily series. For HOUR_1 bars we
    use the PRIOR day's VIX close (info available at bar open). This avoids
    look-ahead. ffill after reindex naturally enforces this when the timezone
    aligns; here we explicitly shift by 1 day to be safe.
    """
    if vix_df.empty:
        # Inject a neutral column so feature columns stay consistent.
        for k, df in klines.items():
            df["vix"] = 15.0
        return klines
    # Convert tz-naive UTC vix index to date-only for the join.
    vix_daily = vix_df.copy()
    vix_daily.index = pd.to_datetime(vix_daily.index).tz_localize(None).normalize()
    # Shift forward 1 day so a bar at e.g. 2025-09-10 09:30 ET reads the
    # 2025-09-09 VIX close (no peek at same-day VIX).
    vix_daily.index = vix_daily.index + pd.Timedelta(days=1)
    for k, df in klines.items():
        df_date = pd.to_datetime(df.index).tz_localize(None).normalize()
        vix_col = vix_daily.reindex(df_date, method="ffill")["vix"].values
        df["vix"] = vix_col
        # Catch any tail NaN (very recent bars without VIX close yet).
        # pandas 3.0 dropped fillna(method=...) — use ffill() directly.
        df["vix"] = df["vix"].ffill().fillna(15.0)
    return klines


def _fetch_sector_etfs(unique_etfs: list[str], days: int,
                       timeframe: str = "HOUR_1") -> dict[str, pd.DataFrame]:
    """Pull HOUR_1 history for each sector ETF via MooMoo (same path as the
    main tickers). Returns {etf_symbol: df}. Skips any that fail.
    """
    log.info("Fetching %d sector ETFs (HOUR_1, %dd) …", len(unique_etfs), days)
    return _fetch_history(unique_etfs, days=days, timeframe=timeframe)


def _enrich_with_sector(klines: dict[str, pd.DataFrame],
                        sector_etfs: dict[str, pd.DataFrame]
                        ) -> dict[str, pd.DataFrame]:
    """Join each ticker's sector ETF close into the ticker df.

    The feature pipeline reads df['sector_close']; missing column → feature
    defaults to 0.0 (model treats it as a noise feature).
    """
    from src.sector import SECTOR_TO_ETF, get_sector
    for sym, df in klines.items():
        etf = SECTOR_TO_ETF.get(get_sector(sym))
        if not etf or etf not in sector_etfs:
            df["sector_close"] = float("nan")
            continue
        etf_df = sector_etfs[etf]
        # Align by timestamp — ETF and ticker share the same HOUR_1 cadence.
        df["sector_close"] = etf_df["close"].reindex(df.index, method="ffill").values
        df["sector_close"] = df["sector_close"].ffill().bfill()
    return klines


def _enrich_with_insider(klines: dict[str, pd.DataFrame], days: int = 730
                         ) -> dict[str, pd.DataFrame]:
    """Pull per-ticker SEC EDGAR insider history (rolling 30d net log $),
    forward-fill onto each HOUR_1 bar.

    Daily series shifted +1 day so a bar at 09:30 reads yesterday's net (no
    look-ahead). Bars before the first filing default to 0 (neutral).
    """
    from src.sec_edgar import insider_rolling_30d_net
    log.info("Fetching SEC EDGAR insider history for %d tickers (this takes ~5 min uncached) …",
             len(klines))
    for sym, df in klines.items():
        try:
            ins_df = insider_rolling_30d_net(sym, days=days)
        except Exception as e:
            log.warning("insider fetch failed for %s: %s", sym, e)
            df["insider_30d_net_log"] = 0.0
            continue
        if ins_df.empty:
            df["insider_30d_net_log"] = 0.0
            continue
        # Shift +1 day → bar reads yesterday's rolling-30d net to avoid look-ahead.
        ins_shifted = ins_df.copy()
        ins_shifted.index = ins_shifted.index + pd.Timedelta(days=1)
        bar_dates = pd.to_datetime(df.index).tz_localize(None).normalize()
        df["insider_30d_net_log"] = (
            ins_shifted["insider_30d_net_log"]
            .reindex(bar_dates, method="ffill")
            .ffill().fillna(0.0)
            .values
        )
        log.info("Insider joined onto %s: range [%.2f, %.2f]",
                 sym,
                 df["insider_30d_net_log"].min(),
                 df["insider_30d_net_log"].max())
    return klines

log = logging.getLogger(__name__)

# Top-10 historical winners from the 180-day baseline backtest.
TOP_10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
# Top-5 — the 80/20 of top-10: highest per-trade expectancy.
TOP_5 = ["SNDK", "INTC", "MU", "WDC", "LRCX"]


def _bad_regime_weights(ts: pd.Series) -> "np.ndarray":
    """Return per-sample weight, giving bad-regime months 3× the lift.

    The 360-day backtest revealed Dec 2024 → Feb 2025 as a brutal regime
    (3 months of net SLs). We weight those bars 3× during training so the
    model spends extra learning-budget on what makes those setups fail.
    """
    import numpy as np
    weights = np.ones(len(ts), dtype=float)
    # Bad regimes — periods where the live backtest lost money.
    bad_ranges = [
        ("2024-12-01", "2025-02-28"),    # post-election correction
        ("2025-05-01", "2025-08-31"),    # summer chop
        ("2025-11-01", "2025-11-30"),    # board-rotation pullback
    ]
    for start, end in bad_ranges:
        mask = (ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))
        weights[mask] = 3.0
    return weights

ML_DIR = settings.root / "data" / "ml"
ML_DIR.mkdir(parents=True, exist_ok=True)


def _train_xgb(X_tr, y_tr, X_val, y_val, **params) -> xgb.XGBClassifier:
    p = dict(
        objective="binary:logistic",
        eval_metric="logloss",
        n_estimators=600,
        max_depth=5,
        learning_rate=0.05,
        min_child_weight=5,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        early_stopping_rounds=40,
    )
    p.update(params)
    model = xgb.XGBClassifier(**p)
    model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
    return model


def _train_lgb(X_tr, y_tr, X_val, y_val, sample_weight=None, **params) -> lgb.LGBMClassifier:
    p = dict(
        objective="binary",
        metric="binary_logloss",
        n_estimators=600,
        max_depth=-1,
        num_leaves=63,
        learning_rate=0.05,
        min_child_samples=20,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        verbose=-1,
    )
    p.update(params)
    model = lgb.LGBMClassifier(**p)
    fit_kwargs = dict(
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )
    if sample_weight is not None:
        fit_kwargs["sample_weight"] = sample_weight
    model.fit(X_tr, y_tr, **fit_kwargs)
    return model


def _eval(model, X_te, y_te) -> dict:
    p_te = model.predict_proba(X_te)[:, 1]
    yhat = (p_te > 0.5).astype(int)
    return {
        "accuracy": float(accuracy_score(y_te, yhat)),
        "auc":      float(roc_auc_score(y_te, p_te)) if y_te.nunique() > 1 else float("nan"),
        "logloss":  float(log_loss(y_te, p_te, labels=[0, 1])),
        "positive_rate": float(y_te.mean()),
        "n_holdout": len(y_te),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")

    full_watchlist = json.loads((settings.root / "config" / "watchlist.json").read_text())["tickers"]
    days = 730   # 2 years
    timeframe = "HOUR_1"
    label_cfg = LabelConfig()

    log.info("Fetching %d days of HOUR_1 data for %d tickers …",
             days, len(full_watchlist))
    t0 = time.time()
    klines_all = _fetch_history(full_watchlist, days=days, timeframe=timeframe)
    log.info("Fetched %d tickers in %.1fs", len(klines_all), time.time() - t0)

    if len(klines_all) < 5:
        raise SystemExit("Too few tickers had data — abort.")

    # Join VIX history into every per-ticker df so the new macro features can
    # compute from real values during training.
    log.info("Fetching VIX history (yfinance) …")
    vix_df = _fetch_vix_history(days=days)
    klines_all = _enrich_with_vix(klines_all, vix_df)
    log.info("VIX joined onto %d tickers' bars", len(klines_all))

    # Sector ETFs — fetch once per unique ETF used by the watchlist, then join
    # the matching sector close onto each ticker's df. Powers the new
    # `rs_vs_sector_5d` feature.
    from src.sector import SECTOR_TO_ETF, get_sector
    unique_etfs = sorted({
        SECTOR_TO_ETF[s] for s in (get_sector(t) for t in klines_all.keys())
        if s in SECTOR_TO_ETF
    })
    sector_etf_data = _fetch_sector_etfs(unique_etfs, days=days, timeframe=timeframe)
    klines_all = _enrich_with_sector(klines_all, sector_etf_data)
    log.info("Sector ETF closes joined (%d ETFs)", len(sector_etf_data))

    # SEC EDGAR insider history per ticker (rolling 30-day signed $).
    klines_all = _enrich_with_insider(klines_all, days=days)

    # Build the FULL 19-ticker dataset once (NEW: returns parallel ts series).
    X_full, y_full, syms_full, ts_full = build_dataset(klines_all, label_cfg)
    X_tr_f, y_tr_f, X_val_f, y_val_f, X_te_f, y_te_f = _time_split(X_full, y_full)
    # Parallel-slice ts to match train fold so we can build per-row weights.
    n_tr_f = len(X_tr_f)
    ts_tr_f = ts_full.iloc[:n_tr_f]
    w_tr_f = _bad_regime_weights(ts_tr_f)
    log.info("Full set: %d rows, holdout %d (positive rate %.1f%%), bad-regime weight rows: %d",
             len(X_full), len(X_te_f), y_te_f.mean() * 100,
             int((w_tr_f > 1.0).sum()))

    # Top-10 dataset.
    klines_top10 = {k: v for k, v in klines_all.items() if k in TOP_10}
    X_t10, y_t10, _, ts_t10 = build_dataset(klines_top10, label_cfg)
    X_tr_t, y_tr_t, X_val_t, y_val_t, X_te_t, y_te_t = _time_split(X_t10, y_t10)
    w_tr_t = _bad_regime_weights(ts_t10.iloc[:len(X_tr_t)])
    log.info("Top-10 set: %d rows, holdout %d", len(X_t10), len(X_te_t))

    # Top-5 dataset (NEW): just the most-profitable historical names.
    klines_top5 = {k: v for k, v in klines_all.items() if k in TOP_5}
    X_t5, y_t5, _, ts_t5 = build_dataset(klines_top5, label_cfg)
    X_tr_5, y_tr_5, X_val_5, y_val_5, X_te_5, y_te_5 = _time_split(X_t5, y_t5)
    w_tr_5 = _bad_regime_weights(ts_t5.iloc[:len(X_tr_5)])
    log.info("Top-5 set: %d rows, holdout %d", len(X_t5), len(X_te_5))

    results = []

    # --- V1: XGBoost on full 19-ticker, 730d ---
    log.info("V1: XGBoost on 19 tickers, 730d …")
    m1 = _train_xgb(X_tr_f, y_tr_f, X_val_f, y_val_f)
    e1 = _eval(m1, X_te_f, y_te_f)
    results.append(("V1_xgb_19tk_730d", m1, e1, X_full.shape[0]))

    # --- V2: LightGBM, same data ---
    log.info("V2: LightGBM on 19 tickers, 730d …")
    m2 = _train_lgb(X_tr_f, y_tr_f, X_val_f, y_val_f)
    e2 = _eval(m2, X_te_f, y_te_f)
    results.append(("V2_lgb_19tk_730d", m2, e2, X_full.shape[0]))

    # --- V3: LightGBM, top-10 specialist (the historical winner) ---
    log.info("V3: LightGBM on TOP-10 only, 730d …")
    m3 = _train_lgb(X_tr_t, y_tr_t, X_val_t, y_val_t)
    e3 = _eval(m3, X_te_t, y_te_t)
    results.append(("V3_lgb_top10_730d", m3, e3, X_t10.shape[0]))

    # --- V4: LightGBM, 19 tickers, deeper trees ---
    log.info("V4: LightGBM 19 tickers, deeper trees, slower LR …")
    m4 = _train_lgb(X_tr_f, y_tr_f, X_val_f, y_val_f,
                    n_estimators=1500, learning_rate=0.02,
                    num_leaves=127, min_child_samples=15)
    e4 = _eval(m4, X_te_f, y_te_f)
    results.append(("V4_lgb_deeper_730d", m4, e4, X_full.shape[0]))

    # --- V5 (NEW): top-10 with bad-regime sample weights ---
    log.info("V5: LightGBM TOP-10 + bad-regime sample weights …")
    m5 = _train_lgb(X_tr_t, y_tr_t, X_val_t, y_val_t,
                    sample_weight=w_tr_t)
    e5 = _eval(m5, X_te_t, y_te_t)
    results.append(("V5_lgb_top10_weighted", m5, e5, X_t10.shape[0]))

    # --- V6 (NEW): top-5 specialist, the concentrated bet ---
    log.info("V6: LightGBM TOP-5 specialist, 730d …")
    m6 = _train_lgb(X_tr_5, y_tr_5, X_val_5, y_val_5)
    e6 = _eval(m6, X_te_5, y_te_5)
    results.append(("V6_lgb_top5", m6, e6, X_t5.shape[0]))

    # --- V7 (NEW): top-5 + bad-regime weights ---
    log.info("V7: LightGBM TOP-5 + bad-regime sample weights …")
    m7 = _train_lgb(X_tr_5, y_tr_5, X_val_5, y_val_5,
                    sample_weight=w_tr_5)
    e7 = _eval(m7, X_te_5, y_te_5)
    results.append(("V7_lgb_top5_weighted", m7, e7, X_t5.shape[0]))

    # --- Report ---
    print("\n" + "=" * 88)
    print("  ML SHOOT-OUT — 730-day HOUR_1 data, 30-feature pipeline")
    print("=" * 88)
    print(f"  {'variant':<22} {'rows':>8} {'pos%':>6} "
          f"{'acc%':>7} {'AUC':>6} {'logloss':>8}")
    print("  " + "-" * 84)
    for name, _model, m, n_rows in results:
        print(f"  {name:<22} {n_rows:>8} {m['positive_rate']*100:>5.1f}% "
              f"{m['accuracy']*100:>6.1f}% {m['auc']:>6.3f} {m['logloss']:>8.4f}")
    print("=" * 88)

    # Pick winner by holdout AUC.
    best_idx = max(range(len(results)), key=lambda i: results[i][2]["auc"])
    best_name, best_model, best_metrics, _ = results[best_idx]
    print(f"\n  ★ Winner: {best_name}  (AUC={best_metrics['auc']:.3f})")

    # Save winning model + metadata so `src.ml.predict` picks it up.
    meta = {
        "trained_at": datetime.utcnow().isoformat() + "Z",
        "winner_variant": best_name,
        "auc_holdout": best_metrics["auc"],
        "accuracy_holdout": best_metrics["accuracy"],
        "logloss_holdout": best_metrics["logloss"],
        "positive_rate_holdout": best_metrics["positive_rate"],
        "n_rows_total": int(results[best_idx][3]),
        "n_holdout": best_metrics["n_holdout"],
        "days_history": days,
        "label_config": {
            "horizon_bars": label_cfg.horizon_bars,
            "tp_atr_mult": label_cfg.tp_atr_mult,
            "sl_atr_mult": label_cfg.sl_atr_mult,
        },
        "feature_names": FEATURE_NAMES,
        "all_variants": [
            {"name": r[0],
             "auc": r[2]["auc"],
             "accuracy": r[2]["accuracy"],
             "n_rows": r[3]}
            for r in results
        ],
    }
    joblib.dump(best_model, ML_DIR / "model.joblib")
    (ML_DIR / "model_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"  Saved → {ML_DIR / 'model.joblib'}  (meta in model_meta.json)")


if __name__ == "__main__":
    main()
