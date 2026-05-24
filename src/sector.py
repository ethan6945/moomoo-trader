"""Sector / correlation exposure control.

Prevents the bot from over-concentrating in one sector.
ETFs that ARE a sector (XLK, XLF, XLE) count toward that sector's bucket.
Broad market ETFs (SPY, QQQ, IWM) get their own solo bucket — holding all
three would be unusual but is technically allowed.
"""
from __future__ import annotations

import pandas as pd

SECTOR_MAP: dict[str, str] = {
    # Technology (software + hardware + large-cap internet)
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "GOOGL": "tech",
    "META": "tech", "AMD": "tech", "AVGO": "tech", "CRM": "tech",
    "XLK": "tech",
    # Semiconductors (high intra-sector correlation, separate from broad tech)
    "MU": "semis", "TSM": "semis", "ASML": "semis", "SMH": "semis",
    # Consumer (discretionary + staples combined — high macro correlation)
    "AMZN": "consumer", "NFLX": "consumer", "DIS": "consumer",
    "COST": "consumer", "WMT": "consumer",
    # Mobility / travel
    "UBER": "mobility", "ABNB": "mobility",
    # Auto / EV
    "TSLA": "auto", "F": "auto",
    # Finance
    "JPM": "finance", "V": "finance", "MA": "finance", "XLF": "finance",
    # Industrials
    "BA": "industrial", "CAT": "industrial", "GE": "industrial",
    # Energy
    "XLE": "energy",
    # Broad ETFs — each is its own bucket (they're uncorrelated by design)
    "SPY": "etf_spy", "QQQ": "etf_qqq", "IWM": "etf_iwm",
}

# At most this many positions in the same sector bucket at once.
# With MAX_POSITIONS=5 this caps any one sector at 40%.
MAX_PER_SECTOR = 2


def get_sector(symbol: str) -> str:
    return SECTOR_MAP.get(symbol.upper(), "unknown")


def check_sector_exposure(
    symbol: str,
    positions: pd.DataFrame,
    pending_symbols: set[str],
) -> tuple[bool, str]:
    """Return (allowed, reason).

    Counts filled positions + pending orders in the same sector bucket.
    Unknown-sector tickers always pass (no false negatives for unlisted stocks).
    """
    sector = get_sector(symbol)
    if sector == "unknown":
        return True, "sector unknown — skipping check"

    # Symbols currently held (qty > 0)
    held_syms: list[str] = []
    if not positions.empty:
        held = positions[positions["qty"].astype(float) > 0]
        held_syms = [c.split(".")[-1] for c in held["code"].tolist()]

    same_sector = [
        s for s in held_syms + list(pending_symbols)
        if get_sector(s) == sector and s.upper() != symbol.upper()
    ]

    if len(same_sector) >= MAX_PER_SECTOR:
        return False, (
            f"sector '{sector}' already has {len(same_sector)} position(s) "
            f"({', '.join(same_sector)}), max={MAX_PER_SECTOR}"
        )
    return True, "ok"
