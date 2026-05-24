"""Market regime detection — bull / neutral / bear via SPY moving averages.

When SPY trades below its 200-day SMA, long-momentum systems statistically
underperform.  We classify the regime each scan and let main.py decide:
  • BULL    — trade normally
  • NEUTRAL — trade but with reduced size (handled via VIX usually)
  • BEAR    — pause new entries entirely
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pandas_ta_classic as ta


@dataclass
class Regime:
    label: str           # BULL | NEUTRAL | BEAR
    spy_price: float
    sma200: float
    sma50: float
    above_200: bool
    above_50: bool
    note: str

    @property
    def bullish(self) -> bool:
        return self.label == "BULL"

    @property
    def block_new_entries(self) -> bool:
        return self.label == "BEAR"


def assess(spy_daily: pd.DataFrame) -> Regime:
    """Classify market regime from SPY daily bars (need ≥ 200 bars)."""
    if len(spy_daily) < 200:
        return Regime("NEUTRAL", 0.0, 0.0, 0.0, False, False,
                      "insufficient SPY history — defaulting to NEUTRAL")

    close = spy_daily["close"]
    price = float(close.iloc[-1])
    sma200 = float(ta.sma(close, length=200).iloc[-1])
    sma50 = float(ta.sma(close, length=50).iloc[-1])
    above_200 = price > sma200
    above_50 = price > sma50

    if above_200 and above_50:
        label, note = "BULL", f"SPY ${price:.2f} above both MAs"
    elif not above_200 and not above_50:
        label, note = "BEAR", f"SPY ${price:.2f} below both MAs — pause longs"
    else:
        side = "above 200, below 50" if above_200 else "below 200, above 50"
        label, note = "NEUTRAL", f"SPY ${price:.2f} {side}"

    return Regime(label, price, sma200, sma50, above_200, above_50, note)
