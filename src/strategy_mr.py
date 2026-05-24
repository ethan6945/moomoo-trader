"""Mean-reversion strategy — complement to the trend-following 6-factor.

When trend strategy under-performs (range-bound markets, choppy regimes),
mean-reversion fills the gap.  Looks for oversold-bounce setups:

  • RSI(7) < 30          → oversold
  • Price ≤ lower BB     → 2σ below 20-bar mean
  • Today's candle closes green AND closes back above lower BB → reversal confirmed
  • Volume > 1.2× the 20-bar mean → real interest, not a quiet bleed
  • ADX < 25             → only fire in non-trending markets (mean-revert needs chop)

Sizing / stop / TP use the SAME Signal contract as the trend strategy so the
downstream funnel doesn't care which one fired — except the audit tag.

main.py runs both strategies in parallel and picks the higher-scoring one per
ticker.  This way the bot can earn in both regimes without manual switching.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pandas_ta_classic as ta

from .indicators import Signal
from .timeframe import TF, current as _tf


# Weights sum to 90 — same convention as trend (AI takes the remaining 10).
WEIGHTS = {
    "oversold":   25,    # RSI deep + BB touch
    "reversal":   25,    # green candle / close-above-lower-BB
    "volume":     20,    # confirming surge
    "non_trend":  20,    # ADX < 25 (mean-revert dies in trends)
}


def _score_oversold(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    rsi = ta.rsi(df["close"], length=tf.rsi_period).iloc[-1]
    bb = ta.bbands(df["close"], length=tf.bb_period, std=2)
    if bb is None or bb.empty:
        return 0, "BB unavailable"
    lower = bb.iloc[-1, 0]
    last = df["close"].iloc[-1]
    below_lower = last <= lower * 1.01     # within 1% of lower band
    deep_oversold = rsi < 25
    mildly_oversold = rsi < 35

    if deep_oversold and below_lower:
        return 100, f"RSI={rsi:.1f} deep oversold + below lower BB ({lower:.2f})"
    if mildly_oversold and below_lower:
        return 80, f"RSI={rsi:.1f} oversold + below lower BB"
    if deep_oversold:
        return 50, f"RSI={rsi:.1f} deep oversold (BB not touched)"
    return 0, f"RSI={rsi:.1f} not oversold"


def _score_reversal(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    o = df["open"].iloc[-1]
    h = df["high"].iloc[-1]
    l = df["low"].iloc[-1]
    c = df["close"].iloc[-1]
    bb = ta.bbands(df["close"], length=tf.bb_period, std=2)
    if bb is None or bb.empty:
        return 0, "BB n/a"
    lower = bb.iloc[-1, 0]
    prev_close = df["close"].iloc[-2]
    rng = max(h - l, 1e-9)
    lower_wick = (min(c, o) - l) / rng

    # Hammer / pin-bar: long lower wick + green body
    green = c > o
    hammer = lower_wick >= 0.5 and green
    closed_above_lower_bb = c > lower
    reclaim = prev_close <= lower and c > lower

    if hammer and closed_above_lower_bb:
        return 100, f"Hammer reversal, lower-wick {lower_wick:.0%}, reclaimed lower BB"
    if reclaim and green:
        return 90, f"Reclaimed lower BB on green candle ({lower:.2f})"
    if green and closed_above_lower_bb:
        return 60, "Green candle inside BB"
    if green:
        return 30, "Green candle (no BB reclaim)"
    return 0, "No reversal signal"


def _score_volume(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    vol_ma = df["volume"].rolling(tf.vol_ma).mean().iloc[-1]
    today = df["volume"].iloc[-1]
    ratio = today / vol_ma if vol_ma else 0
    if ratio >= 1.5:
        return 100, f"Vol surge ×{ratio:.2f} (capitulation+bounce)"
    if ratio >= 1.2:
        return 70, f"Vol confirming ×{ratio:.2f}"
    if ratio >= 0.9:
        return 30, f"Vol normal ×{ratio:.2f}"
    return 0, f"Vol weak ×{ratio:.2f} (no real interest)"


def _score_non_trend(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    """ADX < 25 → market is NOT trending → mean-reversion has edge."""
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=tf.adx_period)
    if adx_df is None or adx_df.empty:
        return 50, "ADX n/a — neutral"
    adx = adx_df.iloc[-1, 0]
    if adx < 18:
        return 100, f"ADX={adx:.0f} choppy — perfect for MR"
    if adx < 25:
        return 70, f"ADX={adx:.0f} weak trend — MR ok"
    if adx < 30:
        return 30, f"ADX={adx:.0f} trending — MR risky"
    return 0, f"ADX={adx:.0f} strong trend — avoid MR"


def evaluate(symbol: str, df: pd.DataFrame) -> Signal:
    """Score a mean-reversion setup.  Returns a Signal with strategy='mean_revert'."""
    tf = _tf()
    min_bars = max(tf.bb_period, tf.adx_period, tf.atr_period, 30) + 5
    if len(df) < min_bars:
        return Signal(symbol=symbol, price=float(df["close"].iloc[-1]) if len(df) else 0,
                      atr=0, score=0, reasons=["insufficient data"],
                      strategy="mean_revert")

    factors = {
        "oversold":  _score_oversold(df, tf),
        "reversal":  _score_reversal(df, tf),
        "volume":    _score_volume(df, tf),
        "non_trend": _score_non_trend(df, tf),
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
        strategy="mean_revert",
    )
