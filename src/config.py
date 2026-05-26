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
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

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

    root: Path = ROOT


settings = Settings()
