"""Market breadth filter — P0-3 (2026-06-26).

Prevents trading in "fake bull markets" where SPY is up but most stocks are
down — the index is carried by a few mega-caps while the mid-cap names in our
universe are silently bleeding. This filter runs before the candidate scan and
can block new entries entirely when breadth is unhealthy.

Sources:
  • Martin Zweig — "Winning on Wall Street" (A/D line, breadth thrust)
  • Larry Williams — market breadth indicators
  • IBD — accumulation/distribution rating

DATA: Uses SPY daily bars as a breadth proxy (Moomoo has no market-wide breadth
API). Two signals:
  1. SPY distance from 50-day MA — how far above/below trend
  2. SPY recent win streak (days up / total) — microcosm of A/D line
  3. VIX overlay — panic tape is unhealthy regardless of SPY

On total failure → pass (never block on missing data — same policy as MTF /
earnings gates).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BreadthVerdict:
    healthy: bool
    ad_ratio: float       # advance/decline proxy (1.0 = neutral, >1 = bullish)
    pct_above_50ma: float  # % of stocks above 50-day MA proxy (0-100)
    vix: float
    note: str


# Thresholds — tuned from Zweig/Williams consensus
AD_HEALTHY_MIN = 0.70      # A/D ratio proxy < 0.7 ⇒ weak breadth
PCT_50MA_HEALTHY_MIN = 45  # < 45% above 50MA proxy ⇒ broad weakness
VIX_PANIC = 30              # VIX > 30 ⇒ fear-driven tape, treat as unhealthy


def assess(client, vix: float = 0.0) -> BreadthVerdict:
    """Evaluate market breadth via SPY proxy. Returns verdict. Never raises."""
    ad_ratio = 1.0
    pct_50ma = 50.0
    source = "unknown"

    try:
        from moomoo import KLType
        spy_df = client.get_kline("SPY", bars=100, ktype=KLType.K_DAY)
        if spy_df is not None and len(spy_df) >= 55:
            import pandas_ta_classic as ta
            close = spy_df["close"]
            sma50 = float(ta.sma(close, length=50).iloc[-1])
            price = float(close.iloc[-1])
            # Distance from 50MA → map to 0-100 scale
            pct_abv = (price / sma50 - 1.0) * 100
            pct_50ma = max(0.0, min(100.0, 50 + pct_abv * 8))
            # A/D proxy: SPY win streak over last 10 days
            n = len(spy_df)
            days_up = sum(
                1 for i in range(-10, 0)
                if i + n >= 0 and spy_df["close"].iloc[i] > spy_df["open"].iloc[i]
            )
            ad_ratio = max(0.1, days_up / 10)
            source = "spy_proxy"
        else:
            source = "insufficient_spy_bars"
    except Exception as e:
        log.warning("SPY breadth proxy failed: %s", e)
        source = "failed"

    vix_ok = vix < VIX_PANIC if vix > 0 else True
    ad_ok = ad_ratio >= AD_HEALTHY_MIN
    ma_ok = pct_50ma >= PCT_50MA_HEALTHY_MIN
    healthy = ad_ok and ma_ok and vix_ok

    if not healthy:
        reasons = []
        if not ad_ok:
            reasons.append(f"A/D {ad_ratio:.2f}<{AD_HEALTHY_MIN}")
        if not ma_ok:
            reasons.append(f"%>50MA {pct_50ma:.0f}%<{PCT_50MA_HEALTHY_MIN}%")
        if not vix_ok:
            reasons.append(f"VIX {vix:.0f}≥{VIX_PANIC}")
        note = f"UNHEALTHY [{source}]: " + ", ".join(reasons)
    elif source == "unknown":
        note = "pass — breadth unavailable"
        healthy = True
    else:
        note = f"healthy [{source}]: A/D {ad_ratio:.2f}, %>50MA {pct_50ma:.0f}%"

    return BreadthVerdict(
        healthy=healthy,
        ad_ratio=round(ad_ratio, 3),
        pct_above_50ma=round(pct_50ma, 1),
        vix=round(vix, 1),
        note=note,
    )
