"""Multi-factor scoring with timeframe-aware indicators.

Timeframes (selected via env TIMEFRAME):
  • DAILY  — classic 6-factor swing: trend / momentum / volume / pattern / AI
  • HOUR_1 — same + ADX trend strength + VWAP filter + Stochastic momentum
             (hourly is noisier, extra confirmation reduces fake signals)
  • MIN_10 — intraday entry frame; requires 30-min direction pre-filter
             (call min30_trend_bullish() on MIN_30 bars before scoring)

The AI sub-score (10 pts) is filled by `ai_validator` / `local_ai` and added
on top of the technical score returned here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pandas_ta_classic as ta

from .timeframe import TF, current as _tf


# Weights sum to 90 — the remaining 10 is owned by ai_validator.
DAILY_WEIGHTS = {
    "trend":    25,
    "momentum": 25,
    "volume":   20,
    "pattern":  20,
}

HOUR_1_WEIGHTS = {
    "trend":      18,  # EMA cross + VWAP filter baked in
    "momentum":   18,  # MACD + RSI + Stochastic
    "volume":     14,
    "pattern":    14,
    "adx":        13,  # trend strength gate
    "vwap":       13,  # institutional benchmark
}

# 10-min intraday weights — VWAP and ADX are critical noise filters at this TF.
MIN_10_WEIGHTS = {
    "trend":    15,
    "momentum": 20,  # fast MACD cross is the primary entry trigger
    "volume":   15,
    "pattern":  10,
    "adx":      20,  # high weight — ADX<20 means chop, skip entirely
    "vwap":     10,  # price must be on the right side of VWAP
}


@dataclass
class Signal:
    symbol: str
    price: float
    atr: float
    score: float
    breakdown: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    strategy: str = "trend"   # "trend" | "mean_revert"

    @property
    def stop_loss(self) -> float:
        # Runtime override (owner-approved optimizer) beats .env, no restart;
        # falls back to the .env setting. Inline import avoids circular load.
        from . import runtime_config
        return round(self.price - runtime_config.sl_atr_mult() * self.atr, 2)

    @property
    def take_profit(self) -> float:
        from . import runtime_config
        return round(self.price + runtime_config.tp_atr_mult() * self.atr, 2)


# ---------- factor scorers ----------

def _score_trend(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    ema_f = ta.ema(df["close"], length=tf.ema_fast)
    ema_s = ta.ema(df["close"], length=tf.ema_slow)
    if ema_f.iloc[-1] > ema_s.iloc[-1]:
        slope = ema_f.iloc[-1] - ema_f.iloc[-5]
        if slope > 0:
            return 100, f"EMA{tf.ema_fast}>EMA{tf.ema_slow}, slope+"
        return 60, f"EMA{tf.ema_fast}>EMA{tf.ema_slow} but flat"
    return 0, f"EMA{tf.ema_fast}<=EMA{tf.ema_slow} (downtrend)"


def _score_momentum(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    macd = ta.macd(df["close"], fast=tf.macd_fast, slow=tf.macd_slow, signal=tf.macd_signal)
    rsi = ta.rsi(df["close"], length=tf.rsi_period).iloc[-1]
    if macd is None or macd.empty:
        return 0, "MACD unavailable"
    macd_line = macd.iloc[-1, 0]
    signal_line = macd.iloc[-1, 2]
    prev_macd = macd.iloc[-2, 0]
    prev_signal = macd.iloc[-2, 2]

    cross_up = prev_macd <= prev_signal and macd_line > signal_line

    # On HOUR_1 add Stochastic %K confirmation for momentum.
    stoch_ok = True
    stoch_note = ""
    if tf.name == "HOUR_1":
        stoch = ta.stoch(df["high"], df["low"], df["close"], k=tf.stoch_k, d=tf.stoch_d)
        if stoch is not None and not stoch.empty:
            k = stoch.iloc[-1, 0]
            stoch_ok = 20 <= k <= 80
            stoch_note = f", Stoch={k:.0f}"

    in_range = 40 <= rsi <= 70
    base = 0
    if cross_up and in_range and stoch_ok:
        base = 100
    elif macd_line > signal_line and in_range and stoch_ok:
        base = 70
    elif in_range:
        base = 40
    return base, f"RSI={rsi:.1f}, MACD={'cross↑' if cross_up else 'flat'}{stoch_note}"


def _score_volume(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    vol_ma = df["volume"].rolling(tf.vol_ma).mean().iloc[-1]
    today_vol = df["volume"].iloc[-1]
    ratio = today_vol / vol_ma if vol_ma else 0
    if ratio >= 1.5:
        return 100, f"Vol surge ×{ratio:.2f}"
    if ratio >= 1.0:
        return 50, f"Vol normal ×{ratio:.2f}"
    return 20, f"Vol weak ×{ratio:.2f}"


def _score_pattern(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    close = df["close"]
    high_n = df["high"].rolling(tf.breakout_lookback).max().iloc[-2]
    bb = ta.bbands(close, length=tf.bb_period, std=2)
    lower = bb.iloc[-1, 0]
    prev_close = close.iloc[-2]
    today_close = close.iloc[-1]
    if today_close > high_n:
        return 100, f"Breakout > {tf.breakout_lookback}-bar high {high_n:.2f}"
    if prev_close <= lower and today_close > prev_close:
        return 90, f"BB lower-band bounce from {lower:.2f}"
    if today_close > close.iloc[-2]:
        return 40, "Closing green"
    return 0, "No bullish pattern"


def _score_adx(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=tf.adx_period)
    if adx_df is None or adx_df.empty:
        return 0, "ADX unavailable"
    adx = adx_df.iloc[-1, 0]
    if adx >= 25:
        return 100, f"ADX={adx:.0f} strong trend"
    if adx >= 20:
        return 70, f"ADX={adx:.0f} mild trend"
    if adx >= 15:
        return 30, f"ADX={adx:.0f} weak"
    return 0, f"ADX={adx:.0f} choppy"


def _rolling_vwap(df: pd.DataFrame, length: int = 20) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    return pv.rolling(length).sum() / df["volume"].rolling(length).sum()


def _score_vwap(df: pd.DataFrame, tf: TF) -> tuple[float, str]:
    vwap20 = _rolling_vwap(df, length=20)
    last = df["close"].iloc[-1]
    v = vwap20.iloc[-1]
    if pd.isna(v):
        return 0, "VWAP n/a"
    diff_pct = (last - v) / v * 100
    if diff_pct >= 0.5:
        return 100, f"Price {diff_pct:+.2f}% vs VWAP (above)"
    if diff_pct >= 0:
        return 60, f"Price flat vs VWAP ({diff_pct:+.2f}%)"
    return 0, f"Price {diff_pct:+.2f}% below VWAP"


# ---------- multi-timeframe helper ----------

def check_gap(df_daily: pd.DataFrame, max_gap_pct: float = 3.0) -> tuple[bool, str]:
    """Overnight gap filter — refuse to chase/catch big moves.

    Compares today's open to yesterday's close on the DAILY chart.
    Blocks if |gap| > max_gap_pct.
      • Gap-up: chase risk (mean-reversion likely)
      • Gap-down: knife-catch (downtrend may continue)
    """
    if len(df_daily) < 2:
        return True, "insufficient data for gap check"
    prev_close = float(df_daily["close"].iloc[-2])
    curr_open = float(df_daily["open"].iloc[-1])
    if prev_close <= 0:
        return True, "invalid prev close"
    gap_pct = (curr_open - prev_close) / prev_close * 100
    if abs(gap_pct) > max_gap_pct:
        return False, f"overnight gap {gap_pct:+.2f}% > ±{max_gap_pct}%"
    return True, f"gap {gap_pct:+.2f}% OK"


def relative_strength(stock_daily: pd.DataFrame, bench_daily: pd.DataFrame,
                      lookback: int = 20) -> tuple[float, str]:
    """Relative strength = stock's N-day return minus benchmark's N-day return.

    Phase 3-A new-alpha factor. Positive RS ⇒ the name is outperforming the
    index over the window (classic momentum/RS edge). Both returns are computed
    from DAILY closes so the value is identical in live and backtest regardless
    of the intraday timeframe the rest of the funnel runs on.

    Returns (rs_pct, note) in percentage points. Degrades gracefully: if either
    series lacks `lookback + 1` bars we return (0.0, …) so the caller's
    `rs >= rs_min` gate passes (never block on missing data — same policy as the
    MTF / earnings gates).
    """
    def _ret(df: pd.DataFrame) -> float | None:
        if df is None or len(df) < lookback + 1:
            return None
        c = df["close"]
        past = float(c.iloc[-1 - lookback])
        if past <= 0:
            return None
        return (float(c.iloc[-1]) / past - 1) * 100

    s_ret = _ret(stock_daily)
    b_ret = _ret(bench_daily)
    if s_ret is None or b_ret is None:
        return 0.0, "RS n/a — insufficient daily bars (pass)"
    rs = s_ret - b_ret
    return rs, f"RS {rs:+.1f}pp ({lookback}d: stock {s_ret:+.1f}% vs bench {b_ret:+.1f}%)"


def sector_regime_bullish(etf_daily: pd.DataFrame, fast: int = 20,
                          slow: int = 50) -> tuple[bool, str]:
    """Fast sector-regime gate (Phase 3-B): EMA(fast) > EMA(slow) on a sector ETF.

    For a semiconductor-heavy book, SOXX flips its short-vs-medium EMA cross far
    sooner than SPY crosses its 200-day SMA, so this catches sector rolls the
    slow market-regime gate misses. Returns (ok, note); insufficient bars ⇒
    (True, …) so it never blocks on missing data.
    """
    if etf_daily is None or len(etf_daily) < slow + 5:
        return True, f"sector-regime n/a — <{slow + 5} bars (pass)"
    close = etf_daily["close"]
    ef = float(ta.ema(close, length=fast).iloc[-1])
    es = float(ta.ema(close, length=slow).iloc[-1])
    if ef > es:
        return True, f"EMA{fast}({ef:.2f}) > EMA{slow}({es:.2f}) — sector up"
    return False, f"EMA{fast}({ef:.2f}) <= EMA{slow}({es:.2f}) — sector down, pause"


def daily_trend_bullish(df_daily: pd.DataFrame) -> tuple[bool, str]:
    """Check daily-chart trend for MTF confirmation (called when TIMEFRAME=HOUR_1).

    Requires EMA20 > EMA50 on the daily chart.  Price being above EMA20 is
    a stronger signal but not strictly required — we still allow entries when
    price pulls back to EMA20 as long as the slope is up.
    """
    if len(df_daily) < 55:
        return True, "daily: not enough bars — skipping MTF gate"
    ema20 = ta.ema(df_daily["close"], length=20)
    ema50 = ta.ema(df_daily["close"], length=50)
    e20, e50 = float(ema20.iloc[-1]), float(ema50.iloc[-1])
    price = float(df_daily["close"].iloc[-1])
    if e20 > e50:
        note = "above" if price > e20 else "below"
        return True, f"DAILY EMA20({e20:.2f}) > EMA50({e50:.2f}), price {note} EMA20"
    return False, f"DAILY EMA20({e20:.2f}) < EMA50({e50:.2f}) — daily downtrend"


# ---------- entrypoint ----------

def evaluate(symbol: str, df: pd.DataFrame) -> Signal:
    """Compute weighted score. AI adds the remaining ~10 points later."""
    tf = _tf()
    min_bars = max(tf.ema_slow, tf.bb_period, tf.adx_period, tf.atr_period) + 5
    if len(df) < min_bars:
        return Signal(symbol=symbol, price=float(df["close"].iloc[-1]) if len(df) else 0,
                      atr=0, score=0, reasons=["insufficient data"])

    if tf.name in ("HOUR_1", "MIN_10"):
        weights = MIN_10_WEIGHTS if tf.name == "MIN_10" else HOUR_1_WEIGHTS
        scorers = {
            "trend":    _score_trend,
            "momentum": _score_momentum,
            "volume":   _score_volume,
            "pattern":  _score_pattern,
            "adx":      _score_adx,
            "vwap":     _score_vwap,
        }
    else:
        weights = DAILY_WEIGHTS
        scorers = {
            "trend":    _score_trend,
            "momentum": _score_momentum,
            "volume":   _score_volume,
            "pattern":  _score_pattern,
        }

    factors = {name: fn(df, tf) for name, fn in scorers.items()}
    weighted = sum(score * weights[name] / 100 for name, (score, _) in factors.items())
    atr = float(ta.atr(df["high"], df["low"], df["close"], length=tf.atr_period).iloc[-1])

    return Signal(
        symbol=symbol,
        price=float(df["close"].iloc[-1]),
        atr=atr,
        score=round(weighted, 1),
        breakdown={name: score for name, (score, _) in factors.items()},
        reasons=[f"{name}: {reason}" for name, (_, reason) in factors.items()],
    )
