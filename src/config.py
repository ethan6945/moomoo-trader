import logging
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


def _timeframe(default: str = "HOUR_1") -> str:
    # DAILY trading mode removed 2026-06-07 — coerce any legacy value to HOUR_1
    # so no string-keyed branch (MTF/gap/scoring) ever sees a stale "DAILY".
    tf = os.getenv("TIMEFRAME", default).upper()
    return "HOUR_1" if tf == "DAILY" else tf


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
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

    tavily_key: str = os.getenv("TAVILY_API_KEY", "")

    # (DeepSeek removed 2026-06-08 — the whole system is unified on Gemini 3.5
    # Flash. The autonomous optimizer now asks Gemini for proposals via
    # gemini_model below; no second provider/key to manage.)

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
    # Position-slot cap scales with capital (req#1: change budget → recompute
    # params). max_positions acts as a FLOOR; larger accounts open more slots up
    # to max_positions_cap (≈ universe size). At the $4.5k default round(4500/1000)
    # = 4 (banker's rounding), which the max_positions FLOOR of 5 rescues — so the
    # derived value == max_positions and behaviour is unchanged until capital grows
    # past ~$5.5k. The cash wall (sizing_capital budget cap) stays the real
    # constraint at small budgets.
    slot_target_usd: float = _float("SLOT_TARGET_USD", 1000.0)
    max_positions_cap: int = _int("MAX_POSITIONS_CAP", 10)
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
    timeframe: str = _timeframe()   # DAILY trading mode removed 2026-06-07 (coerced → HOUR_1)

    # Optuna-tuned exit knobs — moved out of code so .env can override.
    # TP = entry + TP_ATR_MULT × ATR ; SL = entry - SL_ATR_MULT × ATR.
    tp_atr_mult: float = _float("TP_ATR_MULT", 1.5)
    sl_atr_mult: float = _float("SL_ATR_MULT", 2.0)
    max_gap_pct: float = _float("MAX_GAP_PCT", 3.0)   # overnight gap filter

    # Regime up-scaling (2026-06-08): press more size ONLY in a confirmed strong
    # bull (regime.bullish, i.e. SPY > 50MA > 200MA) AND a calm tape (VIX <
    # regime_vix_calm). Mirrors the honest engine's use_regime_scaling so live ↔
    # backtest stay in parity. DEFAULT 1.0 = INERT (no behaviour change); set
    # REGIME_BULL_MULT=1.4 in .env to activate the owner-approved tailwind boost.
    # It only scales the risk-based qty and still obeys the max_position_pct cap,
    # so it can never push a single name past its concentration limit. Backtests
    # (140d, $5k) showed 1.35-1.5× lifts $/day a few % with DD flat; higher
    # multipliers (2×, 2.5×) were over-fit noise on ~40 trades — do not chase them.
    regime_bull_mult: float = _float("REGIME_BULL_MULT", 1.0)
    regime_vix_calm: float = _float("REGIME_VIX_CALM", 20.0)

    # Gap-risk sentinel (2026-06-08): for HELD positions, exit DURING regular hours
    # before a likely overnight gap (a stop can't catch a gap — it fills past the
    # stop). Two layers: (1) deterministic — exit if earnings is within
    # GAP_EXIT_EARNINGS_DAYS; (2) AI — Gemini judges fresh PUBLIC bad news and only
    # sells on a high-confidence verdict (≥ GAP_SENTINEL_AI_MIN_CONF), fail-safe to
    # HOLD on any error. Default OFF (inert) — set GAP_SENTINEL_ENABLED=true to arm.
    # The AI decision overrides the strategy's hold; every exit fires a notification.
    gap_sentinel_enabled: bool = os.getenv("GAP_SENTINEL_ENABLED", "false").lower() in ("1", "true", "yes")
    gap_exit_earnings_days: int = _int("GAP_EXIT_EARNINGS_DAYS", 1)
    gap_sentinel_ai: bool = os.getenv("GAP_SENTINEL_AI", "true").lower() in ("1", "true", "yes")
    gap_sentinel_ai_min_conf: int = _int("GAP_SENTINEL_AI_MIN_CONF", 70)
    # Cost control: the AI gap layer uses ONE fixed model (not the entry cascade),
    # runs at PRE-MARKET only by default (gap_sentinel_ai_intraday=False skips the
    # ~11 per-scan AI calls/day — the deterministic earnings layer still runs every
    # scan for free), and skips the Gemini call entirely when there's no fresh news.
    gap_sentinel_model: str = os.getenv("GAP_SENTINEL_MODEL", "gemini-2.5-flash-lite")
    gap_sentinel_ai_intraday: bool = os.getenv("GAP_SENTINEL_AI_INTRADAY", "false").lower() in ("1", "true", "yes")

    # 3-tranche scale-out (2026-05-30 cash-frontier finding: banking partials and
    # recycling the cash is the best NO-LEVERAGE lever for a real $5k account —
    # it lifts cash-on $/day on both 180d & 360d while keeping MTM-DD < the halt).
    # OFF by default so live behaviour is identical until explicitly switched on.
    # When on, the soft manager closes 1/3 of the ORIGINAL qty at +TP1_R, another
    # 1/3 at +TP2_R, and trails the final ~1/3. R = entry − initial_stop
    # (= SL_ATR_MULT × ATR). NOTE: like the legacy TP_HALF, this runs on the
    # soft-management path (SIMULATE, or REAL when the OCO bracket attach failed);
    # in REAL with a live bracket the broker owns the single TP.
    use_scale_out: bool = os.getenv("USE_SCALE_OUT", "false").lower() in ("1", "true", "yes")
    tp1_r: float = _float("TP1_R", 3.0)
    tp2_r: float = _float("TP2_R", 6.0)

    # Breakeven stop (2026-06-11 exit audit): once price has been
    # +BREAKEVEN_TRIGGER_R × R in profit (R = entry − initial stop), ratchet
    # the stop up to entry — a former winner can no longer turn into a loser.
    # Engine-measured (dynamic top-15, honest lens): PnL-neutral on 140d
    # ($18.0 vs $18.8/day) but PF 3.15→3.44, MTM-DD 11.3%→9.8%, and the choppy
    # recent window flips −$8.5→+$37.8/day. 72% of trades reach +1R.
    use_breakeven_stop: bool = os.getenv("USE_BREAKEVEN_STOP", "true").lower() in ("1", "true", "yes")
    breakeven_trigger_r: float = _float("BREAKEVEN_TRIGGER_R", 1.0)

    # Stall-out (close positions that go nowhere for 3 business days).
    # 2026-06-11: DEFAULT OFF for exit parity — the validated engine has no
    # stall-out and its MAX_HOLD bucket is net positive; live stall-outs were
    # 2-for-2 losers. Turn on only after the engine models it and it passes
    # the dual-window gate.
    stall_out_enabled: bool = os.getenv("STALL_OUT_ENABLED", "false").lower() in ("1", "true", "yes")

    # ── Autopilot (2026-06-11): bounded autonomy for parameter changes ──
    # When ON, an optimizer proposal that PASSED the dual-window honest-engine
    # gate AND sits inside runtime_config.ALLOWED_PARAMS bounds is applied
    # immediately (db-state override, no restart) with a Telegram notification;
    # an auto-rollback watcher reverts it if live results degrade. When OFF
    # (default), every change still queues for owner approval.
    auto_apply_params: bool = os.getenv("AUTO_APPLY_PARAMS", "false").lower() in ("1", "true", "yes")

    # ── Phase 1 (2026-06-11): rule-based dynamic universe ──
    # Weekly: watchlist := top N of the liquidity pool (config/universe_pool.json)
    # by 6-1 momentum (src/universe.py). OFF by default — flip
    # DYNAMIC_UNIVERSE_ENABLED in .env to activate; every refresh that changes
    # the list is Telegram-notified (no silent universe drift).
    dynamic_universe_enabled: bool = os.getenv("DYNAMIC_UNIVERSE_ENABLED",
                                               "false").lower() in ("1", "true", "yes")
    universe_top_n: int = int(os.getenv("UNIVERSE_TOP_N", "15"))

    # Drawdown circuit breaker — discovered from the 142-day backtest where
    # Nov 2025 alone lost -$761 (17% of account) before the strategy recovered.
    # When account-level DD breaches these thresholds, we either halve qty or
    # halt new entries entirely until equity recovers above the soft-cut line.
    dd_size_cut_pct: float = _float("DD_SIZE_CUT_PCT", 10.0)   # half qty
    dd_halt_pct: float = _float("DD_HALT_PCT", 15.0)           # no new entries

    # (ML alpha engine removed 2026-06-03 — proven inert, AUC ~0.5.)

    # 2026-05-29 combo-sweep finding: mean-revert strategy was net-negative in
    # current bull-skewed watchlist (cost ~$4/day vs trend+momentum only).
    # Disable by default. Set MR_ENABLED=true to re-enable for sideways regimes.
    mr_enabled: bool = os.getenv("MR_ENABLED", "false").lower() in ("1", "true", "yes")

    # Fidelity fix (2026-06-03): when REAL, use the SAME soft-managed exits as
    # SIMULATE (scale-out + trailing + soft stop) instead of a broker OCO
    # bracket. This closes the backtest↔live gap (the honest engine models
    # scale-out, which the REAL bracket path didn't do). Tradeoff: no broker-side
    # hard stop if the process dies (same as SIMULATE today). Default OFF —
    # verify on a small REAL position before enabling.
    real_use_soft_exits: bool = os.getenv("REAL_USE_SOFT_EXITS", "false").lower() in ("1", "true", "yes")

    # Live↔backtest parity (2026-06-03): the honest backtest does NOT model the
    # Gemini AI veto, so leaving it BLOCKING makes live take different trades than
    # the backtest that the $/day figure is based on. Default False = AI runs as
    # advisory (logged + shown in the buy card) but never blocks an entry, so the
    # set of trades matches the backtest. Set true to let AI veto block again.
    ai_veto_blocking: bool = os.getenv("AI_VETO_BLOCKING", "false").lower() in ("1", "true", "yes")

    root: Path = ROOT


settings = Settings()

# Phase 0 guard (2026-06-10): with R = sl_atr_mult × ATR, the first scale-out
# partial sits at tp1_r × sl_atr_mult ATR above entry. If that is at/beyond the
# full TP (tp_atr_mult × ATR), the whole position always closes first and
# USE_SCALE_OUT=true is a silent no-op (exactly what shipped: TP1 = 3.0 × 3.5
# = +10.5 ATR vs TP at +8 ATR). Surface it and disable cleanly so backtests
# and live agree on what the exit actually is.
if settings.use_scale_out and \
        settings.tp1_r * settings.sl_atr_mult >= settings.tp_atr_mult:
    logging.getLogger(__name__).warning(
        "USE_SCALE_OUT=true but TP1 (%.1fR × %.1f ATR/R = +%.1f ATR) is at/"
        "beyond the full TP (+%.1f ATR) — scale-out can never fire; treating "
        "as DISABLED. Lower TP1_R/TP2_R or raise TP_ATR_MULT to activate it.",
        settings.tp1_r, settings.sl_atr_mult,
        settings.tp1_r * settings.sl_atr_mult, settings.tp_atr_mult)
    object.__setattr__(settings, "use_scale_out", False)


def derive_max_positions(capital: float) -> int:
    """Position-slot cap derived from allocated capital, so changing the budget
    recomputes how many concurrent names the bot may hold (req#1) instead of a
    hardcoded number. `max_positions` is the floor (never fewer slots than today);
    slots scale by capital / SLOT_TARGET_USD up to `max_positions_cap` (≈ the
    watchlist size, to avoid over-diversification). At the $4.5k default the raw
    slot math gives round(4500/1000)=4, which the max_positions floor clamps back
    up to 5 — so this returns exactly settings.max_positions and live + backtest
    behaviour is unchanged until capital grows past ~$5.5k. The cash wall stays
    binding at small budgets."""
    slot = settings.slot_target_usd
    if slot <= 0:
        return settings.max_positions
    n = round(capital / slot)
    return max(settings.max_positions, min(settings.max_positions_cap, n))

# Model cascade: GEMINI_MODEL is tried first; on 429/quota it continues downward
# to a cheaper fallback so a transient quota hit doesn't blank the AI. The system
# is unified on Gemini 3.5 Flash (2026-06-08); the lite tier is only an emergency
# quota fallback. Override the starting model via the GEMINI_MODEL env var.
GEMINI_FREE_CASCADE = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]


def gemini_cascade() -> list[str]:
    """Full cascade starting from the configured model, deduped."""
    primary = settings.gemini_model or "gemini-3.5-flash"
    seen: set[str] = set()
    result = []
    for m in [primary] + GEMINI_FREE_CASCADE:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result
