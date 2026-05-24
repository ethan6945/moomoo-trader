"""Feature engineering — single source of truth used by training AND inference.

Given a kline DataFrame with columns (open, high, low, close, volume), returns
a fixed-order feature dict.  Same code path during training (vectorised over
historical bars) and live scoring (one bar at a time), so there's no drift.

Feature design principles:
  1. Stationary where possible — use ratios/z-scores, not raw prices.
  2. Multi-timescale — short (5/7), medium (14/20), long (50).
  3. Mix indicator families — trend, momentum, volume, volatility, candle shape.
  4. Leave room for time-of-day later (kept symbol-agnostic for now).
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

# Fixed order — DO NOT reorder without retraining. Models depend on column order.
FEATURE_NAMES: List[str] = [
    # Trend / EMA structure
    "ema9_over_ema21",       # ratio
    "ema21_over_ema50",
    "px_over_ema20",
    "px_over_ema50",
    "ema20_slope_5",         # 5-bar slope of EMA20
    # Momentum
    "rsi_7",
    "rsi_14",
    "rsi_21",
    "macd_hist",
    "macd_hist_change",      # macd_hist - macd_hist[-3]
    "stoch_k",
    # Volatility
    "atr_pct",               # ATR / price
    "bb_pct",                # (close - lower) / (upper - lower)
    "bb_width_pct",          # (upper - lower) / mid
    "true_range_pct_avg5",   # mean(TR/close, 5)
    # Volume
    "vol_ratio_5",
    "vol_ratio_20",
    "obv_slope_10",          # OBV linear slope
    "vwap_dist_pct",         # (close - VWAP20) / VWAP20
    # Returns (lagged)
    "ret_1",
    "ret_3",
    "ret_5",
    "ret_10",
    "ret_20",
    # Candle shape
    "body_pct",              # |close-open| / range
    "upper_wick_pct",
    "lower_wick_pct",
    "green_streak",          # consecutive up-bars
    # Trend strength
    "adx_14",
    "roc_10",                # rate of change 10 bars
]

N_FEATURES = len(FEATURE_NAMES)


def _safe_div(a, b, default=0.0):
    """np-safe division for series + scalars."""
    if isinstance(b, (pd.Series, np.ndarray)):
        return np.where(np.abs(b) > 1e-9, a / np.where(b == 0, 1, b), default)
    return a / b if abs(b) > 1e-9 else default


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the full feature matrix on a kline DataFrame.

    Returns a DataFrame indexed like `df` with columns = FEATURE_NAMES.
    Early rows where indicators aren't warm yet will be NaN — caller drops.
    """
    if len(df) < 60:
        return pd.DataFrame(index=df.index, columns=FEATURE_NAMES)

    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)

    out: dict = {}

    # --- Trend / EMA ---
    ema9 = ta.ema(c, length=9)
    ema21 = ta.ema(c, length=21)
    ema20 = ta.ema(c, length=20)
    ema50 = ta.ema(c, length=50)
    out["ema9_over_ema21"] = _safe_div(ema9, ema21, 1.0)
    out["ema21_over_ema50"] = _safe_div(ema21, ema50, 1.0)
    out["px_over_ema20"] = _safe_div(c, ema20, 1.0)
    out["px_over_ema50"] = _safe_div(c, ema50, 1.0)
    out["ema20_slope_5"] = (ema20 - ema20.shift(5)) / ema20.shift(5)

    # --- Momentum ---
    out["rsi_7"] = ta.rsi(c, length=7)
    out["rsi_14"] = ta.rsi(c, length=14)
    out["rsi_21"] = ta.rsi(c, length=21)
    macd = ta.macd(c, fast=12, slow=26, signal=9)
    if macd is not None and not macd.empty:
        hist = macd.iloc[:, 1]   # MACDh column
        out["macd_hist"] = hist
        out["macd_hist_change"] = hist - hist.shift(3)
    else:
        out["macd_hist"] = pd.Series(np.nan, index=df.index)
        out["macd_hist_change"] = pd.Series(np.nan, index=df.index)
    stoch = ta.stoch(h, l, c, k=14, d=3)
    out["stoch_k"] = stoch.iloc[:, 0] if stoch is not None and not stoch.empty else pd.Series(np.nan, index=df.index)

    # --- Volatility ---
    atr14 = ta.atr(h, l, c, length=14)
    out["atr_pct"] = _safe_div(atr14, c, 0)
    bb = ta.bbands(c, length=20, std=2)
    if bb is not None and not bb.empty:
        lower = bb.iloc[:, 0]; mid = bb.iloc[:, 1]; upper = bb.iloc[:, 2]
        out["bb_pct"] = _safe_div(c - lower, upper - lower, 0.5)
        out["bb_width_pct"] = _safe_div(upper - lower, mid, 0)
    else:
        out["bb_pct"] = pd.Series(np.nan, index=df.index)
        out["bb_width_pct"] = pd.Series(np.nan, index=df.index)
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    out["true_range_pct_avg5"] = _safe_div(tr, c, 0)
    out["true_range_pct_avg5"] = pd.Series(out["true_range_pct_avg5"], index=df.index).rolling(5).mean()

    # --- Volume ---
    vol_ma5 = v.rolling(5).mean()
    vol_ma20 = v.rolling(20).mean()
    out["vol_ratio_5"] = _safe_div(v, vol_ma5, 1.0)
    out["vol_ratio_20"] = _safe_div(v, vol_ma20, 1.0)
    obv = (np.sign(c.diff()) * v).fillna(0).cumsum()
    out["obv_slope_10"] = (obv - obv.shift(10)) / 10
    typical = (h + l + c) / 3
    vwap20 = (typical * v).rolling(20).sum() / v.rolling(20).sum()
    out["vwap_dist_pct"] = _safe_div(c - vwap20, vwap20, 0)

    # --- Returns ---
    for n in (1, 3, 5, 10, 20):
        out[f"ret_{n}"] = c.pct_change(n)

    # --- Candle shape ---
    rng = (h - l).replace(0, np.nan)
    out["body_pct"] = _safe_div((c - o).abs(), rng, 0)
    out["upper_wick_pct"] = _safe_div(h - np.maximum(c, o), rng, 0)
    out["lower_wick_pct"] = _safe_div(np.minimum(c, o) - l, rng, 0)
    up_bar = (c > o).astype(int)
    # consecutive ups (resets to 0 on red bar)
    streak = up_bar.groupby((up_bar != up_bar.shift()).cumsum()).cumsum() * up_bar
    out["green_streak"] = streak

    # --- Trend strength ---
    adx_df = ta.adx(h, l, c, length=14)
    out["adx_14"] = adx_df.iloc[:, 0] if adx_df is not None and not adx_df.empty else pd.Series(np.nan, index=df.index)
    out["roc_10"] = ta.roc(c, length=10)

    # Assemble in canonical order
    feat_df = pd.DataFrame({k: pd.Series(out[k], index=df.index) for k in FEATURE_NAMES})
    return feat_df


def latest_features(df: pd.DataFrame) -> dict | None:
    """Compute features and return the last row as a dict.
    Returns None if the latest row has any NaN (not enough history)."""
    feats = compute_features(df)
    if feats.empty:
        return None
    last = feats.iloc[-1]
    if last.isna().any():
        return None
    return last.to_dict()
