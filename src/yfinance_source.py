"""yfinance data source for backtests — an independent-window alternative to
MooMoo OpenD.

WHY: OpenD's HOUR_1 history caps at ~150 calendar days, so every hourly backtest
is trapped in the same recent window (can't test overfitting / a different
regime). Yahoo Finance 1h data reaches back ~730 days, which gives a genuine
second/earlier window for out-of-sample validation and for the autopilot's
weekly self-check.

DROP-IN: exposes `get_kline(symbol, bars, ktype)` with the SAME return schema as
`MoomooClient.get_kline` — a tz-naive DatetimeIndex named 'time_key' (ascending)
and columns open/high/low/close/volume — so `backtest.prefetch_data` can swap the
source with no other changes. Default-OFF: the engine only uses this when
`BacktestConfig.data_source == "yfinance"`, so live/moomoo behavior is byte-identical.

CAVEAT: Yahoo 1h bars are ~7/day (the last is a 15:30–16:00 half-bar) and are NOT
tick-aligned with moomoo's bars. Treat a yfinance backtest as an INDEPENDENT
reference for "does the edge survive a different window", not a reproduction of
moomoo fills.
"""
from __future__ import annotations

import logging
import math

import pandas as pd

log = logging.getLogger(__name__)

# moomoo KLType.name -> yfinance interval string
_INTERVAL = {
    "K_1M": "1m", "K_5M": "5m", "K_15M": "15m", "K_30M": "30m",
    "K_60M": "1h", "K_DAY": "1d", "K_WEEK": "1wk", "K_MON": "1mo",
}
# Yahoo's hard history caps per interval, in calendar days.
_MAX_DAYS = {"1m": 7, "5m": 60, "15m": 60, "30m": 60, "1h": 730,
             "1d": 100_000, "1wk": 100_000, "1mo": 100_000}
# Approx candles per trading day, to size the fetch window from a `bars` count.
_BARS_PER_DAY = {"1m": 390, "5m": 78, "15m": 26, "30m": 13, "1h": 7,
                 "1d": 1, "1wk": 0.2, "1mo": 0.05}


def _interval_for(ktype) -> str:
    name = getattr(ktype, "name", None) or str(ktype)
    iv = _INTERVAL.get(name)
    if iv is None:
        raise ValueError(f"yfinance_source: unsupported ktype {ktype!r} (name={name})")
    return iv


class YFinanceSource:
    """Minimal MoomooClient look-alike: only get_kline, matching its schema."""

    def close(self) -> None:
        """No-op — yfinance holds no OpenD connection. Present so YFinanceSource
        is a drop-in wherever prefetch_data calls MoomooClient.close()."""
        return None

    def get_kline(self, symbol: str, bars: int = 120, ktype=None) -> pd.DataFrame:
        import yfinance as yf

        if ktype is None:
            from .timeframe import current as _tf
            ktype = _tf().kltype
        interval = _interval_for(ktype)

        # Size the fetch window to comfortably hold `bars` candles, capped by
        # Yahoo's per-interval history limit, then .tail(bars) like moomoo does.
        bpd = _BARS_PER_DAY.get(interval, 1) or 1
        trading_days = max(1, math.ceil(bars / bpd))
        cal_days = min(math.ceil(trading_days * 7 / 5) + 5, _MAX_DAYS.get(interval, 730))
        period = f"{cal_days}d"

        df = yf.Ticker(symbol).history(period=period, interval=interval,
                                       auto_adjust=False, actions=False)
        if df is None or df.empty:
            raise RuntimeError(
                f"yfinance returned no data for {symbol} ({interval}, {period})")

        # Some yfinance versions return MultiIndex columns — flatten to level 0.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Normalize columns → moomoo schema (lowercase o/h/l/c/v).
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Volume": "volume"})
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep].copy()

        # Index → tz-naive datetime named 'time_key', matching moomoo. Intraday
        # yahoo bars are tz-aware → render in US/Eastern then drop tz so they line
        # up with moomoo's ET-naive time_key; daily bars are already naive.
        idx = pd.to_datetime(df.index)
        if getattr(idx, "tz", None) is not None:
            try:
                idx = idx.tz_convert("America/New_York")
            except Exception:
                pass
            idx = idx.tz_localize(None)
        df.index = idx
        df.index.name = "time_key"

        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df = df.dropna(subset=[c for c in ("open", "high", "low", "close") if c in df.columns])
        return df.tail(bars)
