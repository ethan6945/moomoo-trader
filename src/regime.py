"""Market regime detection — bull / neutral / bear via SPY moving averages.

When SPY trades below its 200-day SMA, long-momentum systems statistically
underperform.  We classify the regime each scan and let main.py decide:
  • BULL    — trade normally
  • NEUTRAL — trade but with reduced size (handled via VIX usually)
  • BEAR    — pause new entries entirely

SMARTER LAYER (2026-06-26, additive + default-inert)
  The raw `label` / `block_new_entries` are UNCHANGED (backtest parity is
  load-bearing — both engines re-derive entry-day regime via assess()). On top
  we now compute, as pure telemetry:
    • vix            — passed in, so the regime KNOWS the vol environment
    • strength       — signed −1..+1 (how far SPY sits above/below its 200-SMA)
    • sub_label      — STRONG_BULL / BULL / HIGH_VOL_BULL / NEUTRAL / BEAR /
                       HIGH_VOL_BEAR / DEEP_BEAR (richer than the 3-way label)
    • confirmed_label— the label after HYSTERESIS (a deadband around the MAs +
                       the previous label), so a single close grazing the 200-MA
                       doesn't whipsaw the bot in and out of BEAR. Falls back to
                       the raw label when no prev_label is given (→ backtests and
                       the first scan are unaffected).
    • risk_mult      — an ADVISORY sizing suggestion (≤1.0); NOT auto-applied,
                       because calc_position_size already cuts size on VIX —
                       surfaced for the dashboard / future validated use only.
  Behavior only changes if SMART_REGIME_ENABLED is set, and then ONLY by letting
  main.py gate on `confirmed` instead of the raw label (whipsaw reduction). That
  path is validated with scripts/regime_diagnose.py before you turn it on.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pandas_ta_classic as ta


@dataclass
class Regime:
    label: str           # BULL | NEUTRAL | BEAR  (RAW — unchanged, backtest-parity)
    spy_price: float
    sma200: float
    sma50: float
    above_200: bool
    above_50: bool
    note: str
    # ── additive advisory fields (default-inert; positional callers unaffected) ──
    vix: float | None = None
    strength: float = 0.0          # signed −1..+1
    sub_label: str = ""            # richer classification
    confirmed_label: str = ""      # hysteresis-smoothed label ("" → use raw)
    risk_mult: float = 1.0         # advisory sizing suggestion (NOT auto-applied)

    @property
    def bullish(self) -> bool:
        return self.label == "BULL"

    @property
    def block_new_entries(self) -> bool:
        """RAW block flag — unchanged. main.py uses this by default."""
        return self.label == "BEAR"

    @property
    def confirmed(self) -> str:
        """Hysteresis-smoothed label, falling back to the raw label."""
        return self.confirmed_label or self.label

    @property
    def confirmed_block(self) -> bool:
        """Block flag using the smoothed label — used only when SMART_REGIME on."""
        return self.confirmed == "BEAR"


# Deadband (as a fraction of the SMA) the price must clear to FLIP the confirmed
# regime. 0.4% around the 200-SMA turns a hair-trigger crossing into a move that
# has to mean it — cutting the 1-day whipsaw that would otherwise thrash the
# cash-yield / inverse-sleeve sweeps in and out every time SPY grazes its MA.
_HYSTERESIS_BAND = 0.004


def _strength(price: float, sma200: float) -> float:
    """Signed −1..+1: SPY's % distance above/below its 200-SMA, ±10% → ±1."""
    if sma200 <= 0:
        return 0.0
    raw = (price / sma200 - 1.0) / 0.10
    return max(-1.0, min(1.0, raw))


def _sub_label(label: str, strength: float, vix: float | None) -> str:
    """Richer classification folding in trend strength + the vol environment."""
    hot = vix is not None and vix >= 28
    if label == "BULL":
        if hot:
            return "HIGH_VOL_BULL"
        return "STRONG_BULL" if strength >= 0.5 else "BULL"
    if label == "BEAR":
        if strength <= -0.5:
            return "DEEP_BEAR"
        return "HIGH_VOL_BEAR" if hot else "BEAR"
    return "NEUTRAL"


def _risk_mult(sub_label: str) -> float:
    """Advisory sizing suggestion (≤1.0). NOT auto-applied (VIX cut already does
    the de-risking in calc_position_size); surfaced for visibility/validation."""
    return {
        "STRONG_BULL": 1.0,
        "BULL": 1.0,
        "HIGH_VOL_BULL": 0.75,
        "NEUTRAL": 0.75,
        "BEAR": 0.5,
        "HIGH_VOL_BEAR": 0.5,
        "DEEP_BEAR": 0.5,
    }.get(sub_label, 1.0)


def _confirmed(raw_label: str, price: float, sma200: float, sma50: float,
               prev_label: str | None) -> str:
    """Apply hysteresis: only FLIP to a new bull/bear state when price clears the
    MA by the deadband; inside the band, hold the previous label. With no
    prev_label (backtests / first scan) this returns the raw label unchanged."""
    if not prev_label:
        return raw_label
    band_hi200 = sma200 * (1 + _HYSTERESIS_BAND)
    band_lo200 = sma200 * (1 - _HYSTERESIS_BAND)
    band_hi50 = sma50 * (1 + _HYSTERESIS_BAND)
    band_lo50 = sma50 * (1 - _HYSTERESIS_BAND)
    clearly_bull = price > band_hi200 and price > band_hi50
    clearly_bear = price < band_lo200 and price < band_lo50
    if clearly_bull:
        return "BULL"
    if clearly_bear:
        return "BEAR"
    # In the deadband on at least one MA → don't flip; hold the prior regime.
    return prev_label


def assess(spy_daily: pd.DataFrame, vix: float | None = None,
           prev_label: str | None = None) -> Regime:
    """Classify market regime from SPY daily bars (need ≥ 200 bars).

    `vix` and `prev_label` are optional and only feed the ADVISORY layer
    (sub_label / strength / confirmed_label); the raw `label` is computed exactly
    as before, so every existing caller (incl. both backtest engines) is
    unaffected when they pass neither.
    """
    if len(spy_daily) < 200:
        r = Regime("NEUTRAL", 0.0, 0.0, 0.0, False, False,
                   "insufficient SPY history — defaulting to NEUTRAL", vix=vix)
        r.confirmed_label = prev_label or "NEUTRAL"
        r.sub_label = "NEUTRAL"
        return r

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

    strength = _strength(price, sma200)
    sub = _sub_label(label, strength, vix)
    confirmed = _confirmed(label, price, sma200, sma50, prev_label)
    return Regime(label, price, sma200, sma50, above_200, above_50, note,
                  vix=vix, strength=round(strength, 3), sub_label=sub,
                  confirmed_label=confirmed, risk_mult=_risk_mult(sub))
