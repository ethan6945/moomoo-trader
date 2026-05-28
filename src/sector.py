"""Sector / correlation exposure control.

Prevents the bot from over-concentrating in one sector.
ETFs that ARE a sector (XLK, XLF, XLE) count toward that sector's bucket.
Broad market ETFs (SPY, QQQ, IWM) get their own solo bucket — holding all
three would be unusual but is technically allowed.
"""
from __future__ import annotations

import pandas as pd

SECTOR_MAP: dict[str, str] = {
    # ─── Technology — software, cloud, networking, large-cap internet ───
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "GOOGL": "tech",
    "META": "tech", "AMD": "tech", "AVGO": "tech", "CRM": "tech",
    "ORCL": "tech", "IBM": "tech", "NOW": "tech",
    "CSCO": "tech",                  # Cisco — enterprise networking
    "DDOG": "tech",                  # Datadog — observability cloud
    "FTNT": "tech", "PANW": "tech",  # Fortinet / Palo Alto — cybersecurity
    "GEN": "tech",                   # Gen Digital — Norton / consumer security
    "AKAM": "tech",                  # Akamai — CDN / edge
    "DELL": "tech", "HPQ": "tech", "HPE": "tech",   # PC / enterprise hardware
    "PLTR": "tech", "RDDT": "tech",  # Palantir, Reddit
    "XLK":  "tech",                  # tech sector ETF

    # ─── Semiconductors (separate bucket — high intra-sector correlation) ───
    "MU":   "semis", "TSM":  "semis", "ASML": "semis", "SMH": "semis",
    "INTC": "semis",                 # Intel
    "QCOM": "semis",                 # Qualcomm — wireless chips
    "TXN":  "semis",                 # Texas Instruments
    "SWKS": "semis",                 # Skyworks — RF chips
    "ON":   "semis",                 # ON Semiconductor
    "ARM":  "semis",                 # ARM Holdings
    "SNDK": "semis",                 # SanDisk — NAND flash storage
    "WDC":  "semis",                 # Western Digital — HDD/SSD storage
    "STX":  "semis",                 # Seagate — storage
    "SMCI": "semis",                 # Super Micro — AI servers tied to chip demand
    "LRCX": "semis",                 # Lam Research — semi equipment (added 2026-05-29)
    "MCHP": "semis",                 # Microchip Technology (added 2026-05-29)
    "AMAT": "semis",                 # Applied Materials — semi equipment
    "KLAC": "semis",                 # KLA Corporation — semi equipment
    "MRVL": "semis",                 # Marvell — chip design
    "NXPI": "semis",                 # NXP Semiconductors
    "ADI":  "semis",                 # Analog Devices

    # ─── Healthcare / biotech ───
    "MRNA": "healthcare",            # Moderna
    "CNC":  "healthcare",            # Centene — health insurance
    "DXCM": "healthcare",            # DexCom — CGM medical devices
    "LLY":  "healthcare", "UNH": "healthcare", "JNJ": "healthcare",
    "ABBV": "healthcare", "MRK": "healthcare", "ABT": "healthcare",
    "XLV":  "healthcare",

    # ─── Consumer (discretionary + staples — high macro correlation) ───
    "AMZN": "consumer", "NFLX": "consumer", "DIS": "consumer",
    "COST": "consumer", "WMT": "consumer", "TGT": "consumer", "LOW": "consumer",
    "SBUX": "consumer", "MCD": "consumer", "NKE": "consumer",
    "PEP":  "consumer", "KO": "consumer", "PG": "consumer",
    "XLP":  "consumer", "XLY": "consumer",

    # ─── Mobility / travel ───
    "UBER": "mobility", "ABNB": "mobility",

    # ─── Auto / EV ───
    "TSLA": "auto", "F": "auto", "GM": "auto",

    # ─── Finance + crypto exchanges (high correlation to rates) ───
    "JPM":  "finance", "V": "finance", "MA": "finance",
    "BAC":  "finance", "WFC": "finance", "PYPL": "finance",
    "COIN": "finance",               # Coinbase — crypto exchange
    "XLF":  "finance",

    # ─── Industrials ───
    "BA":   "industrial", "CAT": "industrial", "GE": "industrial",
    "XLI":  "industrial",

    # ─── Energy ───
    "XOM":  "energy", "CVX": "energy", "XLE": "energy",
    "TPL":  "energy",                # Texas Pacific Land — Permian Basin

    # ─── Broad ETFs — each is its own bucket (uncorrelated by design) ───
    "SPY":  "etf_spy", "QQQ": "etf_qqq", "IWM": "etf_iwm",
    "DIA":  "etf_dia", "VTI": "etf_vti",
}

# At most this many positions in the same sector bucket at once.
# 2026-05-28: bumped to 5 (was 4) because MAX_POSITIONS dropped to 5 and our
# top winners are semis-heavy (SNDK, MU, INTC, LRCX, AMD, SWKS, SNDK, WDC).
# A cap of 4 blocked the 5th semis pick even when we wanted to be fully loaded.
MAX_PER_SECTOR = 5


# Sector → tracking-ETF map. Used by:
#   • signal_reporter for "vs sector strength" context line
#   • ml.features for the rs_vs_sector_5d feature
# Sectors without a clean single-ETF proxy fall back to a near-cousin
# (auto/mobility → XLY consumer discretionary).
SECTOR_TO_ETF: dict[str, str] = {
    "tech":        "XLK",
    "semis":       "SMH",
    "consumer":    "XLY",
    "finance":     "XLF",
    "healthcare":  "XLV",
    "energy":      "XLE",
    "industrial":  "XLI",
    "auto":        "XLY",   # auto is index-level consumer discretionary
    "mobility":    "XLY",   # Uber/ABNB land here too
}


def get_sector_etf(symbol: str) -> str | None:
    """Return the sector-tracking ETF for `symbol`, or None if unknown."""
    sector = get_sector(symbol)
    return SECTOR_TO_ETF.get(sector)


def get_sector(symbol: str) -> str:
    """Look up a symbol's sector. Logs once per unknown so missing entries
    are visible without spamming the log on every scan."""
    sym = symbol.upper()
    if sym in SECTOR_MAP:
        return SECTOR_MAP[sym]
    # Track unknowns just once; helpful when watchlist auto-updates pulls in
    # a new ticker the map doesn't cover yet.
    if sym not in _logged_unknowns:
        _logged_unknowns.add(sym)
        import logging as _log
        _log.getLogger(__name__).warning(
            "sector unknown for %s — add to SECTOR_MAP in src/sector.py", sym
        )
    return "unknown"


_logged_unknowns: set[str] = set()


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
