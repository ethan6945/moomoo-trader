"""Momentum-breakout strategy — variant of trend with stricter confirmation.

Why this complements `indicators.evaluate` (trend) and `strategy_mr` (mean-revert):

  trend           — broad EMA cross + MACD + volume + price patterns
  mean_revert     — RSI oversold + BB lower bounce (works in chop)
  momentum_break  — narrower: ONLY fires on confirmed 20-bar high breakouts
                    with explosive volume + strong ADX trend

The trend scorer is "good setup". Momentum-breakout is "is this the rare
fat-tail trade?" — fires less, but when it does, the run is usually large.

Convention:
  • Score uses the same 0-100 scale as the other strategies.
  • Returns a `Signal` with `strategy="momentum_break"` so audit & sizing can
    tag and (optionally) treat it differently.
"""
from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from .indicators import Signal
from .timeframe import TF, current as _tf


# Weights sum to 90 (AI keeps the remaining 10). Tighter setup = heavier weights
# on confirmation factors (volume, ADX) rather than on the breakout itself.
WEIGHTS = {
    "breakout":   30,    # MUST clear a 20-bar high decisively
    "volume":     25,    # 2× normal volume — bait the breakout-shorts
    "adx_trend":  20,    # ADX ≥ 30 (real trend, not chop)
    "structure":  15,    # EMA9 > EMA21 > EMA50 (all-aligned trend)
}


def _score_breakout(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    """100 only if current close decisively clears the prior 20-bar high."""
    lookback = tf.breakout_lookback
    high_n = df["high"].iloc[-(lookback + 1):-1].max()
    atr = ta.atr(df["high"], df["low"], df["close"], length=tf.atr_period).iloc[-1]
    last = df["close"].iloc[-1]
    if pd.isna(high_n) or pd.isna(atr) or atr <= 0:
        return 0, "insufficient data"
    over_by = last - high_n
    over_atr = over_by / atr
    if over_atr >= 0.3:
        return 100, f"breakout {over_atr:.2f}×ATR above {lookback}-bar high"
    if over_atr >= 0.0:
        return 60, f"marginal breakout {over_atr:.2f}×ATR"
    return 0, f"below {lookback}-bar high"


def _score_volume(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    """2× normal volume — true breakouts need conviction, not drift."""
    vol_ma = df["volume"].rolling(tf.vol_ma).mean().iloc[-1]
    today = df["volume"].iloc[-1]
    ratio = today / vol_ma if vol_ma else 0
    if ratio >= 2.0:
        return 100, f"vol surge ×{ratio:.2f}"
    if ratio >= 1.5:
        return 60, f"vol elevated ×{ratio:.2f}"
    if ratio >= 1.0:
        return 30, f"vol normal ×{ratio:.2f}"
    return 0, f"vol weak ×{ratio:.2f}"


def _score_adx(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    """ADX ≥ 30 — strong directional trend, not chop. Higher bar than trend."""
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=tf.adx_period)
    if adx_df is None or adx_df.empty:
        return 0, "ADX n/a"
    adx = adx_df.iloc[-1, 0]
    if adx >= 30:
        return 100, f"ADX={adx:.0f} strong"
    if adx >= 25:
        return 60, f"ADX={adx:.0f} moderate"
    if adx >= 20:
        return 20, f"ADX={adx:.0f} weak"
    return 0, f"ADX={adx:.0f} chop"


def _score_structure(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    """EMA9 > EMA21 > EMA50 — full stacked uptrend."""
    ema9 = ta.ema(df["close"], length=tf.ema_fast).iloc[-1]
    ema21 = ta.ema(df["close"], length=tf.ema_slow).iloc[-1]
    ema50 = ta.ema(df["close"], length=50).iloc[-1]
    if pd.isna(ema50):
        return 0, "EMA50 n/a"
    if ema9 > ema21 > ema50:
        return 100, "EMAs stacked: 9>21>50"
    if ema9 > ema21:
        return 50, "9>21 but 21 below 50"
    return 0, "EMA structure broken"


def evaluate(symbol: str, df: pd.DataFrame) -> Signal:
    """Score a momentum-breakout setup. Returns Signal with strategy='momentum_break'."""
    tf = _tf()
    min_bars = max(tf.ema_slow, tf.adx_period, tf.breakout_lookback, 50) + 5
    if len(df) < min_bars:
        return Signal(
            symbol=symbol,
            price=float(df["close"].iloc[-1]) if len(df) else 0,
            atr=0, score=0, reasons=["insufficient data"],
            strategy="momentum_break",
        )

    factors = {
        "breakout":  _score_breakout(df, tf),
        "volume":    _score_volume(df, tf),
        "adx_trend": _score_adx(df, tf),
        "structure": _score_structure(df, tf),
    }
    weighted = sum(score * WEIGHTS[name] / 100 for name, (score, _) in factors.items())
    atr = float(ta.atr(df["high"], df["low"], df["close"], length=tf.atr_period).iloc[-1])

    return Signal(
        symbol=symbol,
        price=float(df["close"].iloc[-1]),
        atr=atr,
        score=round(weighted, 1),
        breakdown={name: score for name, (score, _) in factors.items()},
        reasons=[f"{name}: {reason}" for name, (_, reason) in factors.items()],
        strategy="momentum_break",
    )
