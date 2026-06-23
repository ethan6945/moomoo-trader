"""Phase 2A — AI/algo intraday smart exit for HELD long positions.

Goes beyond the gap sentinel (which only guards overnight-gap catalysts): during
regular hours it exits a held name when the picture turns bearish, with two
intents —
  • LOCK PROFIT: a clear technical break-down while already in profit ⇒ bank the
    gain instead of giving it back waiting for the price TP (deterministic, no AI).
  • DEFENSIVE: concrete bearish news / analyst downgrade / sector roll ⇒ the AI
    (ai_validator.assess_exit) exits at any P&L.

Reuses the existing exit plumbing entirely: executor.manage_open_trades calls
assess() on the 5-min tick and force-closes on a hit (no new execution code).

FAIL-SAFE: the AI layer holds on any doubt; the algo layer only LOCKS PROFIT and
never cuts a not-yet-profitable trade (the protective stop owns the downside).
"""
from __future__ import annotations

import logging

import pandas as pd
import pandas_ta_classic as ta

from .config import settings
from .timeframe import current as _tf

log = logging.getLogger(__name__)


def _technical_state(df: pd.DataFrame) -> dict:
    """Deterministic bearish-reversal read from a fresh kline. Returns
    {bearish_break: bool, notes: str}; `notes` also feeds the AI as context."""
    tf = _tf()
    close = df["close"]
    ema9 = ta.ema(close, length=tf.ema_fast)
    ema21 = ta.ema(close, length=tf.ema_slow)
    macd = ta.macd(close, fast=tf.macd_fast, slow=tf.macd_slow, signal=tf.macd_signal)
    rsi = ta.rsi(close, length=tf.rsi_period)
    last = float(close.iloc[-1])
    e9 = float(ema9.iloc[-1])
    e21 = float(ema21.iloc[-1])

    macd_bear = cross_down = False
    notes = []
    if macd is not None and not macd.empty:
        ml = float(macd.iloc[-1, 0]); sg = float(macd.iloc[-1, 2])
        pml = float(macd.iloc[-2, 0]); psg = float(macd.iloc[-2, 2])
        cross_down = pml >= psg and ml < sg
        macd_bear = ml < sg
        notes.append("MACD " + ("death-cross" if cross_down
                                else ("below signal" if macd_bear else "above signal")))
    r = float(rsi.iloc[-1]) if rsi is not None and not rsi.empty else 50.0
    notes.append(f"RSI={r:.0f}")
    notes.append(f"price {'<' if last < e21 else '>='} EMA{tf.ema_slow}({e21:.2f}), "
                 f"EMA{tf.ema_fast} {'<' if e9 < e21 else '>='} EMA{tf.ema_slow}")

    # Bearish break = short-term structure flipped down AND momentum is down.
    bearish = (last < e21) and macd_bear and (e9 <= e21)
    if bearish:
        notes.insert(0, "technical break-DOWN")
    return {"bearish_break": bearish, "notes": ", ".join(notes)}


def assess(symbol: str, trade: dict, last: float, client) -> tuple[bool, str, int]:
    """Return (should_exit, reason, confidence) for a HELD long. Disabled →
    (False, '', 0). Called by executor.manage_open_trades on the 5-min tick."""
    if not settings.smart_exit_enabled:
        return False, "", 0

    # Unrealized R multiple (R = entry − initial stop).
    try:
        entry = float(trade["entry_price"])
        stop = float(trade["stop_loss"])
        r_unit = entry - stop
        r_now = (float(last) - entry) / r_unit if r_unit > 0 else 0.0
    except Exception:
        entry = stop = 0.0
        r_now = 0.0

    # Fresh technical state (degrade gracefully — never block on a fetch error).
    tech = {"bearish_break": False, "notes": "tech n/a"}
    try:
        df = client.get_kline(symbol, bars=120)
        if df is not None and len(df) >= 30:
            tech = _technical_state(df)
    except Exception as e:
        log.debug("smart-exit tech state %s failed: %s", symbol, e)

    # Options flow (optional; bearish positioning — puts ≫ OI, P/C skew — is a
    # strong exit tell for a held long). Self-protecting (timeout→neutral).
    opt_signal, opt_detail = "neutral", ""
    if settings.options_flow_enabled:
        try:
            from . import options_flow
            o = options_flow.assess(symbol, client)
            opt_signal, opt_detail = o["signal"], o["detail"]
        except Exception as e:
            log.debug("smart-exit options flow %s failed: %s", symbol, e)
    context = tech["notes"] + (f" | 期权流: {opt_signal} ({opt_detail})" if opt_detail else "")

    # 1) AI news/sentiment exit (any P&L) — concrete bearish catalyst. The AI now
    # sees both the technical state AND the options-flow read as context.
    if settings.smart_exit_ai:
        try:
            from . import ai_validator
            sell, conf, reason = ai_validator.assess_exit(
                symbol, context, entry=entry, last=float(last), r_now=r_now)
        except Exception as e:
            log.warning("smart-exit AI %s failed: %s — holding", symbol, e)
            sell, conf, reason = False, 0, ""
        if sell and conf >= settings.smart_exit_min_conf:
            return True, f"AI智能退出(信心{conf}): {reason}", conf

    # 2) Deterministic LOCK-PROFIT while in profit, on either a technical
    #    break-down OR clearly bearish options positioning.
    if r_now >= settings.smart_exit_min_profit_r:
        if tech["bearish_break"]:
            return True, f"技术破位锁盈(R={r_now:.1f}): {tech['notes']}", 100
        if opt_signal == "bearish":
            return True, f"期权流看空锁盈(R={r_now:.1f}): {opt_detail}", 100

    return False, "", 0
