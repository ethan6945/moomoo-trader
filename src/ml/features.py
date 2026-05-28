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
# 2026-05-28: added 5 macro/time features (vix_level, vix_change_5, hour_of_day,
# is_opening_hour, is_closing_hour). Models trained before this date are
# incompatible — retrain via `scripts/train_lgbm_compare.py`.
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
    # ── 2026-05-28 keep: macro (VIX) ──
    # Provides 36% of model gain — by far the biggest alpha source.
    "vix_level",             # current VIX level (forward-filled if intraday)
    "vix_change_5",          # 5-bar VIX % change (rising VIX = rising fear)
    # ── Time-of-day (kept for active-model compat; importance ~0.1%) ──
    # 2026-05-29 review showed these scored near-zero in LightGBM gain. They
    # stay in FEATURE_NAMES so the currently-saved model.joblib (trained with
    # 35 features) still loads. Next clean retrain can drop them safely.
    "hour_of_day",
    "is_opening_hour",
    "is_closing_hour",
    # ── 2026-05-29: SEC EDGAR Form 4 insider activity ──
    # `insider_30d_net_log` is sign(net_$) × log1p(|net_$|), so a huge
    # exec sale and a small one are on the same scale. Net positive = recent
    # buying (rare for big-cap tech, strong signal when it happens), net
    # negative = recent selling (mostly stock comp diversification, weaker
    # signal but still informative).
    "insider_30d_net_log",
    # ── 2026-05-28 v2 attempt (REMOVED 2026-05-28): gap + atr-ratio + sector RS ──
    # Tried `daily_open_gap_pct`, `atr_5_over_atr_20`, `rs_vs_sector_5d`.
    # AUC went up slightly (0.630 → 0.640) but BACKTEST $/day DROPPED from
    # $31.63 → $28.51 (-10%) and May 2026 lost $700. The features added noise
    # at the edges (high-conviction setups got vetoed). The compute code is
    # still in compute_features so the columns are produced — they're just not
    # in FEATURE_NAMES, so models won't read them. Re-enable individually if
    # you want to A/B-test each feature alone.
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

    # --- Macro: VIX level + slope ---
    # The 'vix' column must be pre-joined onto df by the caller (training scripts
    # fetch VIX history via yfinance and forward-fill into HOUR_1 bars; predict.py
    # injects the live VIX snapshot). When absent we fall back to 15.0 (neutral)
    # so feature pipeline never crashes — model still gets a sensible default.
    if "vix" in df.columns:
        vix_series = df["vix"].astype(float)
    else:
        vix_series = pd.Series(15.0, index=df.index)
    out["vix_level"] = vix_series
    out["vix_change_5"] = vix_series.pct_change(5).fillna(0.0)

    # --- Time-of-day ---
    # For HOUR_1 bars on US equities, df.index hours range 9–15 (ET). Models
    # learn time-of-day biases: opening hours are noisy, midday quieter, close
    # often trend-continuation. Daily bars don't have meaningful intraday hours
    # so we default the columns to 12 (midday) when not a datetime index or
    # daily timeframe.
    if isinstance(df.index, pd.DatetimeIndex):
        hours = df.index.hour
        out["hour_of_day"] = pd.Series(hours, index=df.index, dtype=float)
        out["is_opening_hour"] = pd.Series((hours <= 10).astype(float), index=df.index)
        out["is_closing_hour"] = pd.Series((hours >= 15).astype(float), index=df.index)
    else:
        out["hour_of_day"] = pd.Series(12.0, index=df.index)
        out["is_opening_hour"] = pd.Series(0.0, index=df.index)
        out["is_closing_hour"] = pd.Series(0.0, index=df.index)

    # --- Daily open gap (proxy for pre-market move) ---
    # For each bar, look up that day's first-bar open vs the prior day's last
    # close. Every hourly bar within the same day sees the SAME gap value —
    # which is the right semantics (the gap is a daily-level event the model
    # should know about regardless of which intraday bar it's looking at).
    #
    # We deliberately use ALL bars (including the first) to compute "today's"
    # open; on hourly data the very first row is the 09:30 ET bar, which is
    # what we want.
    if isinstance(df.index, pd.DatetimeIndex):
        # Group by trading date to find first-open and last-close per day.
        bar_dates = pd.Series(df.index.date, index=df.index)
        daily_first_open = o.groupby(bar_dates).transform("first")
        daily_last_close_prior = c.groupby(bar_dates).transform("last").shift()
        # Map back: every bar of a day gets that day's first open and the
        # previous day's last close (same for all bars within the day).
        # We need the PRIOR day's close — get it via per-day reduction.
        per_day_close = c.groupby(bar_dates).last()
        per_day_open = o.groupby(bar_dates).first()
        prev_close = per_day_close.shift(1)
        gap = (per_day_open - prev_close) / prev_close.where(prev_close > 0, 1)
        # Broadcast back to bar level via date map.
        gap_by_date = gap.to_dict()
        out["daily_open_gap_pct"] = pd.Series(
            [gap_by_date.get(d, np.nan) for d in df.index.date],
            index=df.index,
        )
    else:
        # Daily timeframe: gap = today's open vs yesterday's close.
        out["daily_open_gap_pct"] = (o - c.shift(1)) / c.shift(1)

    # --- ATR ratio (volatility regime) ---
    atr5 = ta.atr(h, l, c, length=5)
    atr20 = ta.atr(h, l, c, length=20)
    out["atr_5_over_atr_20"] = _safe_div(atr5, atr20, 1.0)

    # --- Sector relative strength (5-bar) ---
    # Caller pre-joins the sector ETF close as 'sector_close' column. Computed
    # as: ticker 5-bar return MINUS sector ETF 5-bar return. Positive = ticker
    # outperforming its sector. Default 0.0 when column is missing — the
    # feature becomes a non-discriminator (model treats it as noise).
    if "sector_close" in df.columns:
        sector = df["sector_close"].astype(float)
        out["rs_vs_sector_5d"] = c.pct_change(5) - sector.pct_change(5)
        out["rs_vs_sector_5d"] = pd.Series(out["rs_vs_sector_5d"], index=df.index).fillna(0.0)
    else:
        out["rs_vs_sector_5d"] = pd.Series(0.0, index=df.index)

    # --- SEC EDGAR insider activity (rolling 30-day signed log dollars) ---
    # Caller pre-joins `insider_30d_net_log` per bar by forward-filling the
    # ticker's daily insider rolling series onto the HOUR_1 index. Missing
    # column → 0.0 (neutral).
    if "insider_30d_net_log" in df.columns:
        out["insider_30d_net_log"] = (
            pd.Series(df["insider_30d_net_log"], index=df.index)
            .astype(float)
            .fillna(0.0)
        )
    else:
        out["insider_30d_net_log"] = pd.Series(0.0, index=df.index)

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
