import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    moomoo_host: str = os.getenv("MOOMOO_HOST", "127.0.0.1")
    moomoo_port: int = _int("MOOMOO_PORT", 11111)
    moomoo_trade_pwd: str = os.getenv("MOOMOO_TRADE_PWD", "")
    moomoo_trade_env: str = os.getenv("MOOMOO_TRADE_ENV", "SIMULATE")
    moomoo_market: str = os.getenv("MOOMOO_MARKET", "US")
    moomoo_security_firm: str = os.getenv("MOOMOO_SECURITY_FIRM", "FUTUMY")

    gemini_keys: tuple = tuple(
        k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()
    )
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    tavily_key: str = os.getenv("TAVILY_API_KEY", "")

    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Signal reporter watchlist — tickers to analyse and push as signal cards.
    # Separate from the trading watchlist; can overlap. Set via SIGNAL_WATCHLIST env var.
    signal_watchlist: tuple = tuple(
        s.strip().upper()
        for s in os.getenv("SIGNAL_WATCHLIST", "").split(",")
        if s.strip()
    )

    account_usd: float = _float("ACCOUNT_USD", 4500)
    risk_per_trade: float = _float("RISK_PER_TRADE", 0.02)
    max_positions: int = _int("MAX_POSITIONS", 5)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.20)
    daily_drawdown_stop: float = _float("DAILY_DRAWDOWN_STOP", 0.03)

    # Concentration mode — open fewer new names per scan, pyramid into winners.
    # Per-scan cap on brand-new tickers (does NOT limit add-ons to existing names).
    max_new_names_per_scan: int = _int("MAX_NEW_NAMES_PER_SCAN", 2)
    # How many entries (incl. initial) one symbol may stack. 1 = stacking off.
    max_stacks_per_symbol: int = _int("MAX_STACKS_PER_SYMBOL", 5)
    # Add-on gate: existing position must be at least this many R in profit
    # before a new stack entry is allowed. Prevents averaging-down on losers.
    stack_min_r_multiple: float = _float("STACK_MIN_R_MULTIPLE", 0.5)

    entry_threshold: float = _float("ENTRY_SCORE_THRESHOLD", 70)
    scan_interval_min: int = _int("SCAN_INTERVAL_MIN", 15)
    max_hold_days: int = _int("MAX_HOLD_DAYS", 10)
    timeframe: str = os.getenv("TIMEFRAME", "DAILY")

    # Optuna-tuned exit knobs — moved out of code so .env can override.
    # TP = entry + TP_ATR_MULT × ATR ; SL = entry - SL_ATR_MULT × ATR.
    tp_atr_mult: float = _float("TP_ATR_MULT", 1.5)
    sl_atr_mult: float = _float("SL_ATR_MULT", 2.0)
    max_gap_pct: float = _float("MAX_GAP_PCT", 3.0)   # overnight gap filter

    # Drawdown circuit breaker — discovered from the 142-day backtest where
    # Nov 2025 alone lost -$761 (17% of account) before the strategy recovered.
    # When account-level DD breaches these thresholds, we either halve qty or
    # halt new entries entirely until equity recovers above the soft-cut line.
    dd_size_cut_pct: float = _float("DD_SIZE_CUT_PCT", 10.0)   # half qty
    dd_halt_pct: float = _float("DD_HALT_PCT", 15.0)           # no new entries

    # ML alpha engine (Phase 4)
    ml_enabled: bool = os.getenv("ML_ENABLED", "true").lower() in ("1", "true", "yes")
    ml_blend_weight: float = _float("ML_BLEND_WEIGHT", 0.30)   # 0=ignore ML, 1=ML-only

    # 2026-05-29 combo-sweep finding: mean-revert strategy was net-negative in
    # current bull-skewed watchlist (cost ~$4/day vs trend+momentum only).
    # Disable by default. Set MR_ENABLED=true to re-enable for sideways regimes.
    mr_enabled: bool = os.getenv("MR_ENABLED", "false").lower() in ("1", "true", "yes")

    root: Path = ROOT


settings = Settings()

# Free-tier model cascade: strongest → lightest.
# GEMINI_MODEL is tried first; on 429 the cascade continues downward.
# User can override the starting model via GEMINI_MODEL env var
# (e.g. "gemini-2.5-pro" if they want the strongest available).
GEMINI_FREE_CASCADE = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]


def gemini_cascade() -> list[str]:
    """Full cascade starting from the configured model, deduped."""
    primary = settings.gemini_model or "gemini-2.5-flash"
    seen: set[str] = set()
    result = []
    for m in [primary] + GEMINI_FREE_CASCADE:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result
