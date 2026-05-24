"""Walk-forward backtester using the live 6-factor scoring pipeline.

Fetches historical K-lines from MooMoo, replays the exact same indicator
scoring used in production, simulates entries/exits, and emits a
performance report.

Usage:
    python -m src.backtest                          # watchlist, 180 days, current TF
    python -m src.backtest --days 90
    python -m src.backtest --tickers AAPL MSFT NVDA
    python -m src.backtest --timeframe DAILY
    python -m src.backtest --threshold 65           # lower entry bar to get more trades
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

WATCHLIST_FILE = Path(__file__).parent.parent / "config" / "watchlist.json"
RESULTS_FILE = Path(__file__).parent.parent / "data" / "backtest_results.json"


# ---------- data classes ----------

@dataclass
class Trade:
    symbol: str
    entry_bar: int
    entry_date: str
    entry_price: float
    stop_loss: float
    take_profit: float
    qty: int
    score: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""   # SL | TP | MAX_HOLD | EOD
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class BacktestConfig:
    days: int = 180
    timeframe: str = "HOUR_1"
    threshold: float = 70.0
    tickers: list[str] = field(default_factory=list)
    account_usd: float = 4500.0
    risk_per_trade: float = 0.02
    max_position_pct: float = 0.20
    max_hold_days: int = 10
    # ATR multiples — exposed for optimizer
    tp_atr_mult: float = 1.5            # take-profit = entry + N × ATR
    sl_atr_mult: float = 2.0            # stop-loss   = entry - N × ATR
    # Realism knobs
    base_slip_bp: float = 2.0           # baseline one-side slippage in bps
    atr_slip_k: float = 0.5             # +k × (atr_pct * 100) bps; ATR 2% → +1bp
    sl_breakaway_mult: float = 2.0      # on SL hit, slip × this (gap-thru exits hurt more)
    commission_per_trade: float = 1.0   # $1 round-trip approx
    realistic_limit_fills: bool = True  # if True: limit only fills if next bar low ≤ limit
    apply_mtf_gate: bool = True
    apply_gap_gate: bool = True
    max_gap_pct: float = 3.0
    # Live-funnel gates
    apply_regime_gate: bool = True  # need SPY > 200SMA at entry bar (BULL/NEUTRAL)
    apply_sector_gate: bool = True  # cap concurrent positions per sector (MAX_PER_SECTOR)
    apply_ml_gate: bool = True      # require ML proba ≥ ML_VETO_THRESHOLD if model exists
    apply_mr_strategy: bool = True  # also run mean-reversion strategy in parallel
    # DD circuit breaker — exposed for backtest realism + the live risk_manager
    # uses the same knobs. Per the 142-day backtest (Nov 2025 –22% peak DD),
    # cutting size when DD breaches 10% materially softens the regime-change
    # disaster month.
    dd_size_cut_pct: float = 10.0   # DD ≥ this → halve qty
    dd_halt_pct: float = 15.0       # DD ≥ this → no new entries until recovered
    apply_dd_breaker: bool = True
    # Portfolio simulator (time-stepped) — when True, the simulator enforces
    # MAX_POSITIONS like the live system. Off by default so old runs stay
    # comparable; flip on for true live-parity backtests.
    apply_max_positions: bool = True
    # Diagnostics — counts get returned in metrics so we know what filtered
    track_skip_reasons: bool = True


# ---------- shared portfolio state (DD breaker) ----------

@dataclass
class PortfolioState:
    """Shared across all tickers in a backtest run.

    Lets the DD circuit breaker measure account-level drawdown — same as the
    live `risk_manager.current_drawdown_pct()` — instead of per-ticker pretend-
    DD which over-fires when one ticker happens to lose money even if the
    whole portfolio is up.

    Caveat: the backtester still iterates per-ticker sequentially, so when
    ticker N's loop is mid-window, only tickers 1..N-1's complete trade
    history has been booked. This is an approximation — a true time-stepped
    portfolio simulator would interleave bars across tickers — but it's
    materially closer to live behaviour than the per-ticker version.
    """
    starting_capital: float
    realized_pnl: float = 0.0
    peak_equity: float = 0.0

    def __post_init__(self) -> None:
        # Peak starts at the seed capital — DD is always measured from a
        # high-water mark ≥ starting balance.
        self.peak_equity = max(self.peak_equity, self.starting_capital)

    @property
    def equity(self) -> float:
        return self.starting_capital + self.realized_pnl

    @property
    def dd_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100)

    def record(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.peak_equity = max(self.peak_equity, self.equity)


# ---------- helpers ----------

def _load_watchlist() -> list[str]:
    return json.loads(WATCHLIST_FILE.read_text())["tickers"]


def _position_size(entry: float, stop: float, cfg: BacktestConfig) -> int:
    dist = entry - stop
    if dist <= 0:
        return 0
    by_risk = int(cfg.account_usd * cfg.risk_per_trade / dist)
    by_cap = int(cfg.account_usd * cfg.max_position_pct / entry)
    return max(0, min(by_risk, by_cap))


# Bars-per-trading-day by timeframe — used for sizing data fetches and
# converting max-hold days to bars.
_BARS_PER_DAY = {
    "DAILY":  1,
    "HOUR_1": 7,    # 6.5 trading hours, rounded up
    "MIN_30": 13,
    "MIN_10": 39,
}

# Warm-up bars per timeframe — enough to seed all indicators (EMA50, ADX14,
# BB20, ATR14). Intraday frames need a deeper warm-up because the EMA/ADX
# horizons are shorter but the noise is higher.
_WARM_UP_BARS = {
    "DAILY":  70,
    "HOUR_1": 80,
    "MIN_30": 100,
    "MIN_10": 120,
}


def _max_hold_bars(cfg: BacktestConfig) -> int:
    """Convert max_hold_days to bars based on timeframe."""
    return cfg.max_hold_days * _BARS_PER_DAY.get(cfg.timeframe.upper(), 1)


def _bars_needed(cfg: BacktestConfig) -> int:
    """Total bars to fetch: backtest window + warm-up buffer."""
    bpd = _BARS_PER_DAY.get(cfg.timeframe.upper(), 1)
    test_bars = int(cfg.days * bpd) + max(20, bpd)
    return test_bars + _warm_up(cfg)


def _warm_up(cfg: BacktestConfig) -> int:
    return _WARM_UP_BARS.get(cfg.timeframe.upper(), 70)


# ---------- single-ticker simulation ----------

def backtest_ticker(
    df_full: pd.DataFrame,
    symbol: str,
    cfg: BacktestConfig,
    tf,
    daily_df: Optional[pd.DataFrame] = None,    # for MTF + gap + regime gates
    spy_daily: Optional[pd.DataFrame] = None,   # for regime gate
    portfolio: Optional["PortfolioState"] = None,  # shared DD tracker
) -> list[Trade]:
    """Walk-forward simulation on pre-fetched full DataFrame.

    Realism additions:
      • Slippage: entry +slippage, exits −slippage (long-only model)
      • Commission: $cfg.commission_per_trade per trade
      • MTF gate (HOUR_1 only): require daily EMA20>EMA50 at the entry bar
      • Gap gate: block when |overnight gap| exceeds threshold
      • Regime gate: SPY 200-SMA — only enter when bullish/neutral (skip BEAR)
      • ML gate: skip entry if model proba < ML_VETO_THRESHOLD
      • Mean-reversion strategy: scored in parallel, winner used per bar
    """
    from .indicators import check_gap, daily_trend_bullish, evaluate
    from . import regime as regime_mod
    if cfg.apply_mr_strategy:
        from . import strategy_mr
    ml_pred = None
    if cfg.apply_ml_gate:
        try:
            from .ml import predict as ml_pred
            if not ml_pred.is_available():
                ml_pred = None
        except Exception:
            ml_pred = None

    warm_up = _warm_up(cfg)
    max_hold = _max_hold_bars(cfg)
    commission = cfg.commission_per_trade
    trades: list[Trade] = []
    active: Optional[Trade] = None

    # DD circuit breaker — uses the SHARED PortfolioState so DD is measured
    # at the account level (same definition as live risk_manager). Falls back
    # to a local state if the caller didn't pass one (CLI single-ticker test).
    if portfolio is None:
        portfolio = PortfolioState(starting_capital=cfg.account_usd)

    def _slip_for(sig_obj) -> float:
        """ATR-scaled one-side slippage as a decimal fraction (e.g. 0.0003 = 3bp).

        Volatile names get fatter slippage to reflect wider spreads + worse fills.
        """
        if sig_obj.price <= 0:
            return cfg.base_slip_bp / 10000.0
        atr_pct = sig_obj.atr / sig_obj.price
        bp = cfg.base_slip_bp + cfg.atr_slip_k * (atr_pct * 100)
        return bp / 10000.0

    # Pre-compute daily-bar lookup table by date (for MTF / gap gates).
    daily_by_date: dict = {}
    if daily_df is not None and not daily_df.empty:
        for ix, row in daily_df.iterrows():
            daily_by_date[str(ix.date())] = row

    def _finalise(t: Trade) -> None:
        t.pnl = round((t.exit_price - t.entry_price) * t.qty - commission, 2)
        t.pnl_pct = round(
            (t.exit_price - t.entry_price) / t.entry_price * 100, 2
        )

    for i in range(warm_up, len(df_full) - 1):
        window = df_full.iloc[: i + 1]
        bar = df_full.iloc[i]
        bar_date = str(df_full.index[i].date())
        hi = float(bar["high"])
        lo = float(bar["low"])

        # --- manage open position with realistic exit fills ---
        if active is not None:
            bars_held = i - active.entry_bar
            active_slip = active.exit_price  # placeholder; reuse field below
            exit_slip = (cfg.base_slip_bp + cfg.atr_slip_k * 0) / 10000.0  # base only for exits
            # If we have ATR info from entry, scale; we stored it via stop_loss distance.
            atr_est = (active.entry_price - active.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
            if active.entry_price > 0:
                exit_slip = (cfg.base_slip_bp
                             + cfg.atr_slip_k * (atr_est / active.entry_price * 100)) / 10000.0

            if lo <= active.stop_loss:
                # Stop got hit. If the bar opened below the stop (gap-thru),
                # we slipped further — model worse fill.
                open_price = float(bar["open"])
                trigger = min(open_price, active.stop_loss)
                worsened = exit_slip * cfg.sl_breakaway_mult
                active.exit_price = round(trigger * (1 - worsened), 2)
                active.exit_date = bar_date
                active.exit_reason = "SL"
            elif hi >= active.take_profit:
                # TP is a SELL LIMIT — we get our price (no negative slip).
                active.exit_price = round(active.take_profit, 2)
                active.exit_date = bar_date
                active.exit_reason = "TP"
            elif bars_held >= max_hold:
                # Time-based market exit at the close — symmetric slippage.
                active.exit_price = round(float(bar["close"]) * (1 - exit_slip), 2)
                active.exit_date = bar_date
                active.exit_reason = "MAX_HOLD"

            if active.exit_price:
                _finalise(active)
                trades.append(active)
                # DD tracker — book the realised PnL into shared portfolio state.
                portfolio.record(active.pnl)
                active = None
            continue

        # --- score current bar (trend + mean-revert in parallel) ---
        try:
            sig_trend = evaluate(symbol, window)
            if cfg.apply_mr_strategy:
                sig_mr = strategy_mr.evaluate(symbol, window)
                sig = sig_trend if sig_trend.score >= sig_mr.score else sig_mr
            else:
                sig = sig_trend
        except Exception:
            continue
        if sig.score < cfg.threshold or sig.atr <= 0:
            continue

        # MTF + gap + regime gates using daily df at the entry day
        if daily_df is not None and not daily_df.empty:
            d_until = daily_df.loc[daily_df.index <= df_full.index[i]]
            if len(d_until) >= 2:
                if cfg.apply_mtf_gate and cfg.timeframe == "HOUR_1":
                    ok, _ = daily_trend_bullish(d_until)
                    if not ok:
                        continue
                if cfg.apply_gap_gate:
                    ok, _ = check_gap(d_until, max_gap_pct=cfg.max_gap_pct)
                    if not ok:
                        continue

        # Regime gate (SPY 200-SMA) — block entries when SPY in BEAR
        if cfg.apply_regime_gate and spy_daily is not None and not spy_daily.empty:
            s_until = spy_daily.loc[spy_daily.index <= df_full.index[i]]
            if len(s_until) >= 200:
                regime = regime_mod.assess(s_until)
                if regime.block_new_entries:
                    continue

        # ML gate (using current trained model — same one live uses)
        if ml_pred is not None:
            try:
                proba = ml_pred.predict_proba(window)
                if proba is not None and proba < ml_pred.ML_VETO_THRESHOLD:
                    continue
            except Exception:
                pass

        # --- realistic limit-buy fill simulation ---
        # Live behaviour: we place a limit @ sig.price (current bar close).
        # Backtest must honour that:
        #   • If next_bar.open ≤ limit → fill at the open (better than our limit)
        #   • Else if next_bar.low ≤ limit → fill at our limit (queue slip applies)
        #   • Else → no fill, signal expires
        next_bar = df_full.iloc[i + 1]
        limit_price = float(sig.price)
        next_open = float(next_bar["open"])
        next_low = float(next_bar["low"])
        slip = _slip_for(sig)

        if cfg.realistic_limit_fills:
            if next_open <= limit_price:
                # Marketable on open — better fill, but still pay base slip (spread).
                entry_price = next_open * (1 + slip)
            elif next_low <= limit_price:
                # Bar traded down to our limit — we filled at the limit price.
                entry_price = limit_price * (1 + slip)
            else:
                # Limit never touched, no trade.
                continue
        else:
            # Legacy: always fill at next bar open (optimistic).
            entry_price = next_open * (1 + slip)
        if entry_price <= 0:
            continue

        stop_loss = round(entry_price - cfg.sl_atr_mult * sig.atr, 2)
        take_profit = round(entry_price + cfg.tp_atr_mult * sig.atr, 2)
        qty = _position_size(entry_price, stop_loss, cfg)
        if qty == 0:
            continue

        # DD circuit breaker — read from shared portfolio state (account-level).
        if cfg.apply_dd_breaker:
            dd_pct = portfolio.dd_pct
            if dd_pct >= cfg.dd_halt_pct:
                continue  # halt: no new entries until equity recovers
            if dd_pct >= cfg.dd_size_cut_pct:
                qty = max(1, qty // 2)

        active = Trade(
            symbol=symbol,
            entry_bar=i + 1,
            entry_date=str(df_full.index[i + 1].date()),
            entry_price=round(entry_price, 2),
            stop_loss=stop_loss,
            take_profit=take_profit,
            qty=qty,
            score=sig.score,
        )

    if active is not None:
        last = df_full.iloc[-1]
        # Approximate ATR from the stop distance we stored at entry.
        atr_est = (active.entry_price - active.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
        eod_slip = (cfg.base_slip_bp + cfg.atr_slip_k
                    * (atr_est / max(active.entry_price, 1e-9) * 100)) / 10000.0
        active.exit_price = round(float(last["close"]) * (1 - eod_slip), 2)
        active.exit_date = str(df_full.index[-1].date())
        active.exit_reason = "EOD"
        _finalise(active)
        trades.append(active)
        portfolio.record(active.pnl)

    return trades


# ---------- time-stepped portfolio simulator (live-parity) ----------

def simulate_time_stepped(cfg: BacktestConfig, cache: dict) -> dict:
    """Portfolio-level backtest — processes all tickers' bars by timestamp.

    Unlike `backtest_ticker` (per-ticker sequential), this iterates a single
    chronologically-sorted event stream across the entire watchlist. The
    `PortfolioState`, open-trades dict, and `MAX_POSITIONS` cap all behave
    the way the live `risk_manager` sees them — so DD breaker, sector caps,
    and concurrent-position limits actually fire in the right moments.

    Returns the same dict shape as `simulate_with_cache` for drop-in use.
    """
    from .indicators import check_gap, daily_trend_bullish, evaluate
    from . import regime as regime_mod
    from .config import settings as _settings
    if cfg.apply_mr_strategy:
        from . import strategy_mr
    ml_pred = None
    if cfg.apply_ml_gate:
        try:
            from .ml import predict as ml_pred
            if not ml_pred.is_available():
                ml_pred = None
        except Exception:
            ml_pred = None

    tf = cache["tf"]
    spy_daily = cache["spy_daily"]
    per_ticker = cache["per_ticker"]
    warm_up = _warm_up(cfg)
    max_hold = _max_hold_bars(cfg)
    commission = cfg.commission_per_trade

    # ---------- build the chronological event stream ----------
    events: list[tuple] = []
    for sym, bundle in per_ticker.items():
        df = bundle["intraday"]
        for i in range(warm_up, len(df) - 1):
            events.append((df.index[i], sym, i))
    events.sort(key=lambda x: x[0])
    log.info("[time-step] event stream: %d bars across %d tickers",
             len(events), len(per_ticker))

    # ---------- portfolio & open positions ----------
    portfolio = PortfolioState(starting_capital=cfg.account_usd)
    open_trades: dict[str, Trade] = {}
    closed_trades: list[Trade] = []

    # Cache spy/regime lookups & daily-by-date lookups to avoid recomputing.
    daily_lookup: dict[str, dict] = {}
    for sym, bundle in per_ticker.items():
        d = bundle.get("daily")
        if d is not None and not d.empty:
            daily_lookup[sym] = {str(ix.date()): row for ix, row in d.iterrows()}
        else:
            daily_lookup[sym] = {}

    # ---------- helpers (mirror backtest_ticker logic exactly) ----------
    def _slip_for(sig_obj) -> float:
        if sig_obj.price <= 0:
            return cfg.base_slip_bp / 10000.0
        atr_pct = sig_obj.atr / sig_obj.price
        return (cfg.base_slip_bp + cfg.atr_slip_k * (atr_pct * 100)) / 10000.0

    def _finalise(t: Trade) -> None:
        t.pnl = round((t.exit_price - t.entry_price) * t.qty - commission, 2)
        t.pnl_pct = round(
            (t.exit_price - t.entry_price) / t.entry_price * 100, 2
        )

    def _close(active: Trade, sym: str) -> None:
        _finalise(active)
        closed_trades.append(active)
        portfolio.record(active.pnl)
        open_trades.pop(sym, None)

    # ---------- main loop ----------
    for ts, sym, i in events:
        df = per_ticker[sym]["intraday"]
        daily_df = per_ticker[sym]["daily"]
        bar = df.iloc[i]
        bar_date = str(df.index[i].date())
        hi = float(bar["high"])
        lo = float(bar["low"])

        # --- (A) manage an open position on this symbol ---
        active = open_trades.get(sym)
        if active is not None:
            bars_held = i - active.entry_bar
            atr_est = (active.entry_price - active.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
            exit_slip = (cfg.base_slip_bp + cfg.atr_slip_k
                         * (atr_est / max(active.entry_price, 1e-9) * 100)) / 10000.0

            if lo <= active.stop_loss:
                open_price = float(bar["open"])
                trigger = min(open_price, active.stop_loss)
                worsened = exit_slip * cfg.sl_breakaway_mult
                active.exit_price = round(trigger * (1 - worsened), 2)
                active.exit_date = bar_date
                active.exit_reason = "SL"
            elif hi >= active.take_profit:
                active.exit_price = round(active.take_profit, 2)
                active.exit_date = bar_date
                active.exit_reason = "TP"
            elif bars_held >= max_hold:
                active.exit_price = round(float(bar["close"]) * (1 - exit_slip), 2)
                active.exit_date = bar_date
                active.exit_reason = "MAX_HOLD"

            if active.exit_price:
                _close(active, sym)
            continue   # already has (or had) position on this bar — no new entry

        # --- (B) try to open a new position ---
        # Portfolio max_positions cap (true portfolio-level — what live enforces).
        if cfg.apply_max_positions and len(open_trades) >= _settings.max_positions:
            continue

        # --- score the bar (trend + optional MR) ---
        window = df.iloc[: i + 1]
        try:
            sig_trend = evaluate(sym, window)
            if cfg.apply_mr_strategy:
                sig_mr = strategy_mr.evaluate(sym, window)
                sig = sig_trend if sig_trend.score >= sig_mr.score else sig_mr
            else:
                sig = sig_trend
        except Exception:
            continue
        if sig.score < cfg.threshold or sig.atr <= 0:
            continue

        # --- gates: MTF + gap + regime + ML ---
        if daily_df is not None and not daily_df.empty:
            d_until = daily_df.loc[daily_df.index <= df.index[i]]
            if len(d_until) >= 2:
                if cfg.apply_mtf_gate and cfg.timeframe == "HOUR_1":
                    ok, _ = daily_trend_bullish(d_until)
                    if not ok:
                        continue
                if cfg.apply_gap_gate:
                    ok, _ = check_gap(d_until, max_gap_pct=cfg.max_gap_pct)
                    if not ok:
                        continue

        if cfg.apply_regime_gate and spy_daily is not None and not spy_daily.empty:
            s_until = spy_daily.loc[spy_daily.index <= df.index[i]]
            if len(s_until) >= 200:
                regime = regime_mod.assess(s_until)
                if regime.block_new_entries:
                    continue

        if ml_pred is not None:
            try:
                proba = ml_pred.predict_proba(window)
                if proba is not None and proba < ml_pred.ML_VETO_THRESHOLD:
                    continue
            except Exception:
                pass

        # --- DD circuit breaker (TRUE portfolio-level now that we're chronological) ---
        qty_mult = 1.0
        if cfg.apply_dd_breaker:
            dd_pct = portfolio.dd_pct
            if dd_pct >= cfg.dd_halt_pct:
                continue   # halt new entries until equity recovers
            if dd_pct >= cfg.dd_size_cut_pct:
                qty_mult = 0.5

        # --- realistic limit-buy fill ---
        next_bar = df.iloc[i + 1]
        limit_price = float(sig.price)
        next_open = float(next_bar["open"])
        next_low = float(next_bar["low"])
        slip = _slip_for(sig)

        if cfg.realistic_limit_fills:
            if next_open <= limit_price:
                entry_price = next_open * (1 + slip)
            elif next_low <= limit_price:
                entry_price = limit_price * (1 + slip)
            else:
                continue
        else:
            entry_price = next_open * (1 + slip)
        if entry_price <= 0:
            continue

        stop_loss = round(entry_price - cfg.sl_atr_mult * sig.atr, 2)
        take_profit = round(entry_price + cfg.tp_atr_mult * sig.atr, 2)
        qty = _position_size(entry_price, stop_loss, cfg)
        qty = max(0, int(qty * qty_mult))
        if qty == 0:
            continue

        open_trades[sym] = Trade(
            symbol=sym,
            entry_bar=i + 1,
            entry_date=str(df.index[i + 1].date()),
            entry_price=round(entry_price, 2),
            stop_loss=stop_loss,
            take_profit=take_profit,
            qty=qty,
            score=sig.score,
        )

    # ---------- close anything still open at the very end ----------
    for sym, active in list(open_trades.items()):
        df = per_ticker[sym]["intraday"]
        last = df.iloc[-1]
        atr_est = (active.entry_price - active.stop_loss) / max(cfg.sl_atr_mult, 1e-9)
        eod_slip = (cfg.base_slip_bp + cfg.atr_slip_k
                    * (atr_est / max(active.entry_price, 1e-9) * 100)) / 10000.0
        active.exit_price = round(float(last["close"]) * (1 - eod_slip), 2)
        active.exit_date = str(df.index[-1].date())
        active.exit_reason = "EOD"
        _close(active, sym)

    metrics = compute_metrics(closed_trades, cfg)
    log.info("[time-step] done — %d trades, final DD=%.2f%%, equity=$%.2f",
             len(closed_trades), portfolio.dd_pct, portfolio.equity)
    return {
        "config": {
            "days": cfg.days, "timeframe": cfg.timeframe,
            "threshold": cfg.threshold,
            "tickers": list(per_ticker.keys()),
            "account_usd": cfg.account_usd,
            "simulator": "time_stepped",
        },
        "metrics": metrics,
        "trades": [asdict(t) for t in sorted(closed_trades, key=lambda t: t.entry_date)],
        "errors": [],
        "generated_at": str(date.today()),
    }


# ---------- metrics ----------

def compute_metrics(trades: list[Trade], cfg: Optional[BacktestConfig] = None) -> dict:
    """Risk-adjusted metrics + Monte Carlo. Delegates to src.metrics.

    `cfg` provides starting_capital + horizon — falls back to defaults when None
    so legacy callers (older saved results) still work.
    """
    from .metrics import compute_full_metrics

    if not trades:
        return {"total_trades": 0, "note": "no trades generated"}

    starting_capital = cfg.account_usd if cfg else 4500.0
    n_days = cfg.days if cfg else 180
    trade_dicts = [asdict(t) for t in trades]

    # Core risk-adjusted block (Sharpe daily, Sortino, Calmar, MAR, Ulcer, MC).
    metrics = compute_full_metrics(trade_dicts, starting_capital, n_days)

    # Per-trade-style add-ons the optimizer + GUI still want.
    sorted_t = sorted(trades, key=lambda t: t.exit_date)
    monthly: dict[str, float] = {}
    reasons: dict[str, int] = {}
    by_sym: dict[str, dict] = {}
    avg_win_pct = avg_loss_pct = 0.0
    wins_pct, losses_pct = [], []
    for t in sorted_t:
        monthly[t.exit_date[:7]] = round(monthly.get(t.exit_date[:7], 0.0) + t.pnl, 2)
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        s = by_sym.setdefault(t.symbol, {"trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if t.pnl > 0 else 0
        s["pnl"] = round(s["pnl"] + t.pnl, 2)
        if t.pnl > 0:
            wins_pct.append(t.pnl_pct)
        elif t.pnl < 0:
            losses_pct.append(t.pnl_pct)
    if wins_pct:
        avg_win_pct = round(sum(wins_pct) / len(wins_pct), 2)
    if losses_pct:
        avg_loss_pct = round(sum(losses_pct) / len(losses_pct), 2)

    metrics.update({
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "exit_reasons": reasons,
        "monthly_pnl": {k: v for k, v in sorted(monthly.items())},
        "by_symbol": by_sym,
    })
    return metrics


# ---------- main runner ----------

def prefetch_data(cfg: BacktestConfig, progress_cb=None) -> dict:
    """Fetch all kline data once. Returns a cache dict the optimizer can re-use
    across many Optuna trials without re-hitting OpenD.

    Layout:
        {
          "tf":        TF preset for the timeframe,
          "kltype":    moomoo KLType for the intraday frame,
          "spy_daily": DataFrame | None,
          "per_ticker": {sym: {"intraday": df, "daily": df_d_or_none}, ...},
        }
    """
    from .moomoo_client import MoomooClient
    from .timeframe import DAILY, HOUR_1, MIN_10, MIN_30
    from moomoo import KLType

    _TF_BY_NAME = {"DAILY": DAILY, "HOUR_1": HOUR_1, "MIN_10": MIN_10, "MIN_30": MIN_30}
    tf = _TF_BY_NAME.get(cfg.timeframe.upper(), DAILY)
    kltype = tf.kltype
    total_bars = _bars_needed(cfg)

    tickers = cfg.tickers or _load_watchlist()
    per_ticker: dict[str, dict] = {}
    spy_daily = None

    c = MoomooClient()
    try:
        if cfg.apply_regime_gate:
            try:
                spy_daily = c.get_kline("SPY", bars=max(cfg.days + 250, 350),
                                        ktype=KLType.K_DAY)
                log.info("[prefetch] SPY daily: %d bars", len(spy_daily))
            except Exception as e:
                log.warning("[prefetch] SPY fetch failed: %s", e)

        for idx, sym in enumerate(tickers):
            if progress_cb:
                progress_cb(idx, len(tickers), sym)
            try:
                df = c.get_kline(sym, bars=total_bars, ktype=kltype)
                if len(df) < _warm_up(cfg) + 20:
                    log.warning("[prefetch] %s: only %d bars — skipping", sym, len(df))
                    continue
                daily_df = None
                if cfg.apply_mtf_gate or cfg.apply_gap_gate:
                    try:
                        daily_df = c.get_kline(sym, bars=max(cfg.days + 60, 250),
                                               ktype=KLType.K_DAY)
                    except Exception as e:
                        log.warning("[prefetch] daily fetch failed for %s: %s", sym, e)
                per_ticker[sym] = {"intraday": df, "daily": daily_df}
            except Exception as e:
                log.warning("[prefetch] %s failed: %s", sym, e)
    finally:
        c.close()

    log.info("[prefetch] complete: %d tickers cached", len(per_ticker))
    return {"tf": tf, "kltype": kltype, "spy_daily": spy_daily, "per_ticker": per_ticker}


def simulate_with_cache(cfg: BacktestConfig, cache: dict) -> dict:
    """Run the simulation phase only, using pre-fetched data.

    Delegates to the TIME-STEPPED portfolio simulator (`simulate_time_stepped`)
    so DD breaker / max_positions / sector caps see the same chronological
    state the live `risk_manager` sees. The old per-ticker `backtest_ticker`
    is kept around for ad-hoc single-symbol debugging only.

    No OpenD calls — pure CPU. This is what the Optuna optimizer calls 20-30x
    while only the params change.
    """
    _orig = os.environ.get("TIMEFRAME", "")
    os.environ["TIMEFRAME"] = cfg.timeframe
    try:
        return simulate_time_stepped(cfg, cache)
    finally:
        if _orig:
            os.environ["TIMEFRAME"] = _orig
        else:
            os.environ.pop("TIMEFRAME", None)


def run_backtest(
    cfg: BacktestConfig,
    progress_cb=None,
) -> dict:
    """One-shot: fetch + simulate. Equivalent to prefetch_data + simulate_with_cache.

    Kept for backwards compatibility with the GUI, CLI, and any external callers.
    """
    cache = prefetch_data(cfg, progress_cb=progress_cb)
    result = simulate_with_cache(cfg, cache)
    # Log per-symbol trade counts for parity with the old behaviour.
    by_sym: dict[str, int] = {}
    for t in result["trades"]:
        by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0) + 1
    for sym in cache["per_ticker"]:
        log.info("%s: %d trades", sym, by_sym.get(sym, 0))
    _save_result(result)
    return result


def _save_result(result: dict) -> None:
    """Write the backtest result JSON to disk for the GUI to pick up."""
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(result, indent=2))
    log.info("Results saved to %s", RESULTS_FILE)


# ---------- pretty print ----------

def print_report(result: dict) -> None:
    m = result["metrics"]
    cfg = result["config"]
    print("\n" + "=" * 64)
    print(f"  BACKTEST REPORT  |  {cfg['timeframe']}  |  {cfg['days']} days")
    print("=" * 64)
    print(f"  Tickers tested   : {len(cfg['tickers'])}")
    print(f"  Total trades     : {m.get('total_trades', 0)}")
    if m.get("total_trades", 0) == 0:
        print("  No trades generated — try lowering --threshold")
        return

    # --- Sample stats ---
    print()
    print("  -- SAMPLE --")
    print(f"  Win rate           : {m['win_rate_pct']}%")
    print(f"  Profit factor      : {m['profit_factor']}")
    print(f"  Expectancy/trade   : ${m.get('expectancy_per_trade_usd', 0):+.2f}")
    print(f"  Kelly fraction     : {m.get('kelly_fraction_pct', 0):+.2f}%")
    print(f"  Avg win / loss     : {m['avg_win_pct']:+.2f}% / {m['avg_loss_pct']:+.2f}%")

    # --- Return ---
    print()
    print("  -- RETURN --")
    print(f"  Starting capital   : ${m.get('starting_capital_usd', 0):,.2f}")
    print(f"  Final equity       : ${m.get('final_equity_usd', 0):,.2f}")
    print(f"  Net PnL            : ${m.get('net_pnl_usd', 0):+,.2f}  "
          f"({m.get('total_return_pct', 0):+.2f}%)")
    print(f"  CAGR               : {m.get('cagr_pct', 0):+.2f}%")

    # --- Risk-adjusted ---
    print()
    print("  -- RISK-ADJUSTED --")
    print(f"  Sharpe (daily)     : {m['sharpe_ratio']}")
    print(f"  Sortino            : {m.get('sortino_ratio', 0)}")
    print(f"  Calmar             : {m.get('calmar_ratio', 0)}")
    print(f"  MAR                : {m.get('mar_ratio', 0)}")
    print(f"  Ulcer Index        : {m.get('ulcer_index', 0)}")

    # --- Drawdown ---
    print()
    print("  -- DRAWDOWN --")
    print(f"  Max DD             : ${m.get('max_drawdown_usd', 0):.2f}  "
          f"({m.get('max_drawdown_pct', 0):.2f}%)")
    print(f"  Underwater days    : {m.get('max_drawdown_days', 0)}")

    # --- Monte Carlo (the bullshit detector) ---
    mc = m.get("monte_carlo", {})
    if mc and "n_simulations" in mc:
        print()
        print(f"  -- MONTE CARLO ({mc['n_simulations']} sims) --")
        print(f"  Final equity P5/P50/P95 : ${mc['p5_final']:,.0f}  /  "
              f"${mc['p50_final']:,.0f}  /  ${mc['p95_final']:,.0f}")
        print(f"  Max DD%   P5/P50/P95    : {mc['p5_max_dd_pct']}%  /  "
              f"{mc['p50_max_dd_pct']}%  /  {mc['p95_max_dd_pct']}%")
        print(f"  P(profitable)           : {mc['prob_profitable_pct']}%")
        print(f"  P(ruin: -50% DD)        : {mc['prob_ruin_pct']}%")
    elif mc and "note" in mc:
        print(f"\n  Monte Carlo: {mc['note']}")

    # --- Exit / Monthly / Symbol breakdown (unchanged) ---
    print()
    print("  Exit breakdown:")
    for reason, count in sorted(m.get("exit_reasons", {}).items()):
        print(f"    {reason:<12}: {count}")

    print()
    print("  Monthly PnL:")
    for month, pnl in m.get("monthly_pnl", {}).items():
        bar = "█" * min(20, max(0, int(abs(pnl) / 5)))
        sign = "+" if pnl >= 0 else "-"
        print(f"    {month}  {sign}${abs(pnl):.2f}  {bar}")

    print()
    print("  Top 5 symbols:")
    by_sym = m.get("by_symbol", {})
    ranked = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for sym, s in ranked[:5]:
        wr = round(s["wins"] / s["trades"] * 100)
        print(f"    {sym:<8}  {s['trades']} trades  {wr}% WR  ${s['pnl']:+.2f}")
    print("=" * 64)
    print(f"  Full results saved to: data/backtest_results.json")
    print("=" * 64 + "\n")


# ---------- CLI entry point ----------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    ap = argparse.ArgumentParser(description="moomoo-trader backtester")
    ap.add_argument("--days", type=int, default=180, help="Lookback window in calendar days")
    ap.add_argument("--timeframe", default=None, help="HOUR_1 or DAILY (default: from .env)")
    ap.add_argument("--threshold", type=float, default=None, help="Entry score threshold (default: from .env)")
    ap.add_argument("--tickers", nargs="*", help="Specific tickers (default: watchlist)")
    args = ap.parse_args()

    from .config import settings

    cfg = BacktestConfig(
        days=args.days,
        timeframe=args.timeframe or settings.timeframe,
        threshold=args.threshold or settings.entry_threshold,
        tickers=args.tickers or [],
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        # NEW: pull tuned exit / gap knobs from settings (Optuna writes these).
        tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
    )

    print(f"\nRunning backtest: {cfg.timeframe}, {cfg.days} days, threshold={cfg.threshold}")
    print(f"Tickers: {cfg.tickers or 'watchlist'}\n")

    result = run_backtest(cfg)
    print_report(result)


if __name__ == "__main__":
    main()
