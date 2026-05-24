"""Build labeled (X, y) training datasets from historical kline bars.

Labeling scheme — "ATR-based TP-before-SL within N bars" (aligned with LIVE):
  At each entry bar:
    • Compute ATR(14) at that bar  (same indicator used live)
    • TP threshold = entry + TP_ATR_MULT × ATR   (live uses 1.5)
    • SL threshold = entry - SL_ATR_MULT × ATR   (live uses 2.0)
    • Look forward HORIZON_BARS bars  (live: 65 bars at HOUR_1 ≈ 10 days)
    • Label 1 if high reaches TP before low reaches SL; else 0

This is identical to what the executor actually does in live trading.  An
older percentage-based version (TP=+1.2% / SL=-1.8%) was strictly looser than
live ATR thresholds in low-volatility names and tighter in high-volatility
ones — the model was learning patterns that didn't match real execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from .features import FEATURE_NAMES, compute_features

log = logging.getLogger(__name__)


@dataclass
class LabelConfig:
    horizon_bars: int = 65          # ~10 trading days at HOUR_1 (matches MAX_HOLD_DAYS=10)
    tp_atr_mult: float = 1.5        # matches Signal.take_profit = price + 1.5 × ATR
    sl_atr_mult: float = 2.0        # matches Signal.stop_loss = price - 2.0 × ATR
    atr_period: int = 14            # same as indicators.atr_period
    min_bars: int = 60              # warmup buffer
    require_features_clean: bool = True


def label_bars(df: pd.DataFrame, cfg: LabelConfig = LabelConfig()) -> pd.Series:
    """Return a pd.Series of {0, 1, NaN} labels indexed like `df`.

    Each bar's TP/SL distance is its own ATR × multiplier — matches LIVE.
    NaN for warmup bars (ATR not ready) and last `horizon_bars` (no future window).
    """
    n = len(df)
    labels = np.full(n, np.nan)

    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    atr_series = ta.atr(df["high"], df["low"], df["close"], length=cfg.atr_period)
    atr_arr = atr_series.values.astype(float) if atr_series is not None else np.full(n, np.nan)

    for i in range(n - cfg.horizon_bars):
        entry = close[i]
        atr_i = atr_arr[i]
        if entry <= 0 or not np.isfinite(atr_i) or atr_i <= 0:
            continue
        tp = entry + cfg.tp_atr_mult * atr_i
        sl = entry - cfg.sl_atr_mult * atr_i

        hit_tp = -1
        hit_sl = -1
        for j in range(i + 1, i + 1 + cfg.horizon_bars):
            if hit_sl == -1 and low[j] <= sl:
                hit_sl = j
            if hit_tp == -1 and high[j] >= tp:
                hit_tp = j
            if hit_tp != -1 and hit_sl != -1:
                break

        if hit_tp == -1 and hit_sl == -1:
            labels[i] = 0   # max-hold expired with no resolution → treat as not-a-win
        elif hit_tp == -1:
            labels[i] = 0
        elif hit_sl == -1:
            labels[i] = 1
        else:
            labels[i] = 1 if hit_tp <= hit_sl else 0

    return pd.Series(labels, index=df.index, name="label")


def build_dataset(
    klines_by_symbol: dict[str, pd.DataFrame],
    cfg: LabelConfig = LabelConfig(),
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Concatenate features + labels across all tickers.

    Returns (X, y, symbol_index) — symbol_index is parallel to X/y for
    walk-forward by-symbol sampling later.
    """
    X_frames: list[pd.DataFrame] = []
    y_frames: list[pd.Series] = []
    sym_frames: list[pd.Series] = []

    for sym, df in klines_by_symbol.items():
        if len(df) < cfg.min_bars + cfg.horizon_bars + 5:
            log.warning("dataset: %s too short (%d bars) — skipped", sym, len(df))
            continue
        feats = compute_features(df)
        labels = label_bars(df, cfg)

        # Drop rows where features are NaN OR label is NaN.
        mask = (~feats.isna().any(axis=1)) & labels.notna()
        if mask.sum() == 0:
            continue
        X_frames.append(feats.loc[mask])
        y_frames.append(labels.loc[mask].astype(int))
        sym_frames.append(pd.Series(sym, index=feats.loc[mask].index, name="symbol"))

    if not X_frames:
        return (pd.DataFrame(columns=FEATURE_NAMES),
                pd.Series(dtype=int),
                pd.Series(dtype=str))

    X = pd.concat(X_frames).reset_index(drop=False).rename(columns={"index": "ts"})
    y = pd.concat(y_frames).reset_index(drop=True)
    syms = pd.concat(sym_frames).reset_index(drop=True)
    X = X.drop(columns=["ts"]) if "ts" in X.columns else X
    return X[FEATURE_NAMES], y, syms
