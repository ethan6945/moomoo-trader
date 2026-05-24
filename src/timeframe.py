"""Timeframe presets — one place to tune indicator periods.

DAILY (default) is conservative swing trading (2-10 day holds).
HOUR_1 is faster swing on 60-min bars (1-5 day holds), with VWAP/ADX/Stoch
added because hourly action is noisier than daily and needs extra confirmation.
MIN_10 is intraday day-trading on 10-min bars; MIN_30 is the direction filter
used alongside MIN_10 (fetch 30-min trend → enter on 10-min signal).
"""
from __future__ import annotations

from dataclasses import dataclass

from moomoo import KLType

from .config import settings


@dataclass(frozen=True)
class TF:
    name: str
    kltype: KLType
    bars: int            # how many bars to fetch for indicator warm-up
    ema_fast: int        # EMA fast period (was 20 daily)
    ema_slow: int        # EMA slow period (was 50 daily)
    rsi_period: int      # RSI lookback
    macd_fast: int
    macd_slow: int
    macd_signal: int
    bb_period: int
    atr_period: int
    vol_ma: int          # volume moving average lookback
    breakout_lookback: int  # bars used for "N-bar high" breakout pattern
    adx_period: int
    stoch_k: int
    stoch_d: int


DAILY = TF(
    name="DAILY",
    kltype=KLType.K_DAY,
    bars=120,
    ema_fast=20, ema_slow=50,
    rsi_period=14,
    macd_fast=12, macd_slow=26, macd_signal=9,
    bb_period=20,
    atr_period=14,
    vol_ma=20,
    breakout_lookback=20,
    adx_period=14,
    stoch_k=14, stoch_d=3,
)


HOUR_1 = TF(
    name="HOUR_1",
    kltype=KLType.K_60M,
    bars=120,  # ~18 trading days of 1h bars (6.5/day)
    ema_fast=9, ema_slow=21,
    rsi_period=7,
    macd_fast=5, macd_slow=13, macd_signal=5,  # Raschke fast setup
    bb_period=20,
    atr_period=14,
    vol_ma=20,
    breakout_lookback=20,
    adx_period=14,
    stoch_k=14, stoch_d=3,
)

# 30-min bars: direction filter for the intraday dual-TF strategy.
# Uses the same Raschke MACD as HOUR_1 — just on shorter candles.
MIN_30 = TF(
    name="MIN_30",
    kltype=KLType.K_30M,
    bars=100,  # ~10 trading days of 30-min bars (13/day)
    ema_fast=9, ema_slow=21,
    rsi_period=7,
    macd_fast=5, macd_slow=13, macd_signal=5,
    bb_period=20,
    atr_period=14,
    vol_ma=20,
    breakout_lookback=12,
    adx_period=14,
    stoch_k=14, stoch_d=3,
)

# 10-min bars: entry-timing frame for the intraday strategy.
# Faster MACD (3/10/16) is commonly used for sub-30-min scalp setups.
MIN_10 = TF(
    name="MIN_10",
    kltype=KLType.K_10M,
    bars=120,  # ~3 trading days of 10-min bars (39/day)
    ema_fast=9, ema_slow=21,
    rsi_period=7,
    macd_fast=3, macd_slow=10, macd_signal=16,
    bb_period=20,
    atr_period=10,
    vol_ma=20,
    breakout_lookback=6,   # 1-hour high on 10-min bars
    adx_period=14,
    stoch_k=5, stoch_d=3,
)


def current() -> TF:
    tf = settings.timeframe.upper()
    if tf == "HOUR_1":
        return HOUR_1
    if tf == "MIN_10":
        return MIN_10
    if tf == "MIN_30":
        return MIN_30
    return DAILY
