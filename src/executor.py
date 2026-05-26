"""Order execution layer.

Places a limit buy at the signal price, then attaches a sell-stop at the ATR
stop-loss level. The take-profit half is tracked locally in `data/state.json`
and resolved on the next scan (MooMoo doesn't natively bracket OCOs across
contexts in the simple SDK path).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytz

_ET = pytz.timezone("America/New_York")

from moomoo import TrdSide

from .config import settings as _settings  # noqa: F401 (used in caller too)
from . import db, portfolio, risk_manager

from .config import settings
from .indicators import Signal
from .moomoo_client import MoomooClient

ORDER_TIMEOUT_MIN = 5    # cancel unfilled BUY orders after this many minutes


def place_bracket(
    client: MoomooClient,
    symbol: str,
    qty: int,
    stop_price: float,
    tp_price: float,
) -> tuple[str | None, str | None]:
    """Place SELL-stop + SELL-limit pair on REAL accounts. Returns (stop_id, tp_id).

    Either one filling means the position is (at least partly) gone, and the
    other leg must be cancelled — done in `manage_open_trades`.  Either side
    raising is logged and returned as None; the caller decides whether to
    fall back to soft tracking."""
    stop_id, tp_id = None, None
    try:
        stop_id = client.place_stop_loss(symbol, qty, stop_price)
        log.info("Bracket STOP attached %s qty=%d @ $%.2f (id=%s)",
                 symbol, qty, stop_price, stop_id)
    except Exception as e:
        log.error("Bracket STOP failed for %s: %s", symbol, e)
    try:
        tp_id = client.place_limit_order(symbol, qty, tp_price, TrdSide.SELL)
        log.info("Bracket TP attached %s qty=%d @ $%.2f (id=%s)",
                 symbol, qty, tp_price, tp_id)
    except Exception as e:
        log.error("Bracket TP failed for %s: %s", symbol, e)
    return stop_id, tp_id

log = logging.getLogger(__name__)

OPEN_TRADES_FILE = settings.root / "data" / "open_trades.json"


def _load_open_trades() -> dict:
    """Now reads from SQLite. Old JSON file still mirrored for legacy GUI compat."""
    return db.load_open_trades()


def _save_open_trades(trades: dict) -> None:
    """Atomic replace of the open_trades table. Also mirrors to legacy JSON
    for any reader that still touches the file directly."""
    with db.transaction() as c:
        c.execute("DELETE FROM open_trades")
        for sym, t in trades.items():
            c.execute("""
                INSERT INTO open_trades
                (symbol, qty, entry_price, stop_loss, take_profit, atr,
                 half_closed, buy_order_id, stop_order_id, tp_order_id,
                 opened_at, extra)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sym, int(t.get("qty", 0)),
                float(t.get("entry_price", 0)),
                float(t.get("stop_loss", 0)),
                float(t.get("take_profit", 0)),
                float(t.get("atr") or 0),
                1 if t.get("half_closed") else 0,
                t.get("buy_order_id"),
                t.get("stop_order_id"),
                t.get("tp_order_id"),
                t.get("opened_at") or datetime.utcnow().isoformat(),
                None,
            ))
    # Legacy JSON mirror (kept until all GUI readers migrate; cheap to write).
    try:
        OPEN_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        OPEN_TRADES_FILE.write_text(json.dumps(trades, indent=2, default=str))
    except Exception as e:
        log.warning("legacy JSON mirror write failed: %s", e)


def open_position(client: MoomooClient, signal: Signal, qty: int,
                  ml_proba: float | None = None) -> dict:
    """Buy at limit. REAL → attach OCO bracket (STOP + TP); SIMULATE → soft-track.

    Captures `ml_proba` and `strategy` at entry so they can be matched against
    actual outcome (R-multiple, MFE/MAE) at close-time → enables calibration."""
    buy_order_id = client.place_limit_order(
        signal.symbol, qty, signal.price, TrdSide.BUY
    )

    stop_order_id, tp_order_id = None, None
    if settings.moomoo_trade_env == "REAL":
        stop_order_id, tp_order_id = place_bracket(
            client, signal.symbol, qty, signal.stop_loss, signal.take_profit
        )
        if not (stop_order_id and tp_order_id):
            log.warning("Bracket incomplete for %s (stop=%s tp=%s) — soft tracking active as fallback",
                        signal.symbol, stop_order_id, tp_order_id)
    else:
        log.info("SIMULATE: soft stop @ $%.2f & TP @ $%.2f tracked locally",
                 signal.stop_loss, signal.take_profit)

    trade = {
        "symbol": signal.symbol,
        "qty": qty,
        "entry_price": signal.price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "atr": signal.atr,
        "half_closed": False,
        "buy_order_id": buy_order_id,
        "stop_order_id": stop_order_id,
        "tp_order_id": tp_order_id,
        "opened_at": datetime.utcnow().isoformat(),
        # Water-marks start at the entry price — updated each manage tick.
        "high_water": signal.price,
        "low_water": signal.price,
        "ml_proba_entry": ml_proba,
        "strategy": getattr(signal, "strategy", "trend"),
    }
    trades = _load_open_trades()
    trades[signal.symbol] = trade
    _save_open_trades(trades)
    return trade


def _close_and_log(symbol: str, trade: dict, qty: int, exit_price: float, reason: str) -> float:
    """Record a close to risk_manager (state) + portfolio (R-multiple, MFE/MAE).
    Returns the realised pnl."""
    pnl = (exit_price - trade["entry_price"]) * qty
    risk_manager.record_trade_close(pnl, account_usd=settings.account_usd)

    entry = float(trade["entry_price"]) or 1e-9
    hw = float(trade.get("high_water") or trade["entry_price"])
    lw = float(trade.get("low_water") or trade["entry_price"])
    mfe_pct = (hw - entry) / entry * 100
    mae_pct = (lw - entry) / entry * 100

    portfolio.record_close(
        symbol=symbol,
        qty=qty,
        entry=trade["entry_price"],
        stop=trade["stop_loss"],
        exit_price=exit_price,
        exit_reason=reason,
        opened_at=trade.get("opened_at", ""),
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        ml_proba_entry=trade.get("ml_proba_entry"),
        strategy=trade.get("strategy", "trend"),
    )
    return pnl


def cancel_stale_orders(client: MoomooClient) -> list[dict]:
    """Cancel BUY orders older than ORDER_TIMEOUT_MIN minutes.
    Stale unfilled limits eat budget headroom and let prices drift away."""
    canceled: list[dict] = []
    try:
        pending = client.list_pending_buys()
    except Exception as e:
        log.warning("list_pending_buys failed: %s", e)
        return canceled
    if pending.empty:
        return canceled

    # Use ET (America/New_York) for both sides — MooMoo create_time is naive ET.
    now_et = datetime.now(_ET).replace(tzinfo=None)
    for _, row in pending.iterrows():
        ct = str(row.get("create_time", ""))
        try:
            created = datetime.fromisoformat(ct.split(".")[0]) if ct else now_et
            age_min = (now_et - created).total_seconds() / 60
        except (ValueError, IndexError):
            age_min = 0.0
        if age_min > ORDER_TIMEOUT_MIN:
            order_id = str(row.get("order_id", ""))
            sym = str(row.get("code", "?")).split(".")[-1]
            if order_id and client.cancel_order(order_id):
                canceled.append({"type": "cancel_stale", "symbol": sym,
                                 "order_id": order_id, "age_min": round(age_min, 1)})
                log.info("Canceled stale buy %s (id=%s, age=%.1fm)", sym, order_id, age_min)
                # Clean up our `open_trades` record too — we wrote it speculatively
                # the moment we placed the order. If the order never filled, our
                # entry was never real. Without this cleanup we'd carry a "ghost"
                # forever and reconcile would keep complaining.
                tracked = _load_open_trades()
                tracked_trade = tracked.get(sym)
                if tracked_trade and str(tracked_trade.get("buy_order_id")) == order_id:
                    tracked.pop(sym)
                    _save_open_trades(tracked)
                    log.info("Removed ghost open_trades entry for %s (order %s never filled)",
                             sym, order_id)
    return canceled


def _check_bracket_fills(client: MoomooClient, symbol: str, trade: dict) -> dict | None:
    """OCO check: if either bracket leg filled, cancel the other and close the trade.

    Returns an action dict if a bracket leg fired (caller pops the trade), else None.
    Trades without bracket IDs (SIMULATE or REAL-fallback) return None — soft logic
    handles them downstream."""
    stop_id = trade.get("stop_order_id")
    tp_id = trade.get("tp_order_id")
    if not (stop_id and tp_id):
        return None

    stop_filled = client.is_order_filled(stop_id)
    tp_filled = client.is_order_filled(tp_id)

    if stop_filled:
        if client.cancel_order(tp_id):
            log.info("OCO: %s STOP filled, cancelled TP %s", symbol, tp_id)
        else:
            log.warning("OCO: %s STOP filled but TP cancel failed (id=%s)", symbol, tp_id)
        pnl = _close_and_log(symbol, trade, trade["qty"], trade["stop_loss"], "SL_BRACKET")
        return {"type": "stop_hit_bracket", "symbol": symbol,
                "price": trade["stop_loss"], "qty": trade["qty"], "pnl": pnl}

    if tp_filled:
        if client.cancel_order(stop_id):
            log.info("OCO: %s TP filled, cancelled STOP %s", symbol, stop_id)
        else:
            log.warning("OCO: %s TP filled but STOP cancel failed (id=%s)", symbol, stop_id)
        pnl = _close_and_log(symbol, trade, trade["qty"], trade["take_profit"], "TP_BRACKET")
        return {"type": "tp_hit_bracket", "symbol": symbol,
                "price": trade["take_profit"], "qty": trade["qty"], "pnl": pnl}

    return None


def manage_open_trades(client: MoomooClient) -> list[dict]:
    """Per-scan housekeeping: stale order cancel → OCO bracket check → soft fallback."""
    actions: list[dict] = []
    actions.extend(cancel_stale_orders(client))

    trades = _load_open_trades()
    for symbol, trade in list(trades.items()):
        has_bracket = bool(trade.get("stop_order_id") and trade.get("tp_order_id"))

        # --- REAL bracket path: OCO check + max-hold only (broker handles SL/TP) ---
        if has_bracket:
            bracket_action = _check_bracket_fills(client, symbol, trade)
            if bracket_action is not None:
                actions.append(bracket_action)
                trades.pop(symbol)
                continue
            # Bracket alive — only thing the bot still owns is max-hold timeout.
            try:
                opened = datetime.fromisoformat(trade["opened_at"])
                age_days = (datetime.utcnow() - opened).days
            except (KeyError, ValueError):
                age_days = 0
            if age_days >= _settings.max_hold_days:
                # Pull bracket legs first, then market-sell remaining qty.
                for oid in (trade["stop_order_id"], trade["tp_order_id"]):
                    if oid:
                        client.cancel_order(oid)
                try:
                    last = float(client.get_snapshot(symbol)["last_price"])
                except Exception as e:
                    log.warning("max-hold snapshot failed %s: %s", symbol, e)
                    continue
                client.place_limit_order(symbol, trade["qty"], last * 0.995, TrdSide.SELL)
                pnl = _close_and_log(symbol, trade, trade["qty"], last, "MAX_HOLD")
                actions.append({"type": "max_hold_bracket", "symbol": symbol,
                                "price": last, "qty": trade["qty"],
                                "age_days": age_days, "pnl": pnl})
                trades.pop(symbol)
            continue   # bracket path done — don't fall through to soft logic

        # --- SIMULATE / REAL-fallback (no bracket): soft-track via snapshot polling ---
        try:
            snap = client.get_snapshot(symbol)
            last = float(snap["last_price"])
        except Exception as e:
            log.warning("snapshot failed for %s: %s", symbol, e)
            continue

        # Update water-marks for MFE/MAE on eventual close.
        trade["high_water"] = max(float(trade.get("high_water") or trade["entry_price"]), last)
        trade["low_water"] = min(float(trade.get("low_water") or trade["entry_price"]), last)

        # Soft stop-loss check (SIMULATE, or REAL when bracket attach failed).
        if last <= trade["stop_loss"]:
            client.place_limit_order(symbol, trade["qty"], last * 0.995, TrdSide.SELL)
            pnl = _close_and_log(symbol, trade, trade["qty"], last, "SL")
            actions.append({"type": "stop_hit", "symbol": symbol, "price": last,
                            "qty": trade["qty"], "stop": trade["stop_loss"], "pnl": pnl})
            trades.pop(symbol)
            continue

        # Max-hold force close.
        opened = datetime.fromisoformat(trade["opened_at"])
        age_days = (datetime.utcnow() - opened).days
        if age_days >= _settings.max_hold_days:
            client.place_limit_order(symbol, trade["qty"], last * 0.995, TrdSide.SELL)
            pnl = _close_and_log(symbol, trade, trade["qty"], last, "MAX_HOLD")
            actions.append({"type": "max_hold", "symbol": symbol, "price": last,
                            "qty": trade["qty"], "age_days": age_days, "pnl": pnl})
            trades.pop(symbol)
            continue

        # Take-profit half close.
        if not trade["half_closed"] and last >= trade["take_profit"]:
            half = trade["qty"] // 2
            if half > 0:
                client.place_limit_order(symbol, half, last, TrdSide.SELL)
                pnl = _close_and_log(symbol, trade, half, last, "TP_HALF")
                trade["qty"] -= half
                trade["half_closed"] = True
                actions.append({"type": "tp_half", "symbol": symbol, "price": last,
                                "qty": half, "pnl": pnl})

        # Trailing stop after first TP.
        if trade["half_closed"]:
            df = client.get_kline(symbol, bars=30)
            import pandas_ta_classic as ta
            ema20 = float(ta.ema(df["close"], length=20).iloc[-1])
            new_stop = max(trade["stop_loss"], round(ema20, 2))
            if new_stop > trade["stop_loss"]:
                trade["stop_loss"] = new_stop
                actions.append({"type": "trail", "symbol": symbol, "new_stop": new_stop})

    _save_open_trades(trades)
    return actions


def manual_close(client: MoomooClient, symbol: str) -> dict:
    """GUI helper — cancel any bracket legs, then market-sell the position."""
    trades = _load_open_trades()
    if symbol not in trades:
        raise RuntimeError(f"no tracked position for {symbol}")
    trade = trades[symbol]

    # Cancel any live bracket legs so we don't oversell.
    for key in ("stop_order_id", "tp_order_id"):
        oid = trade.get(key)
        if oid:
            ok = client.cancel_order(oid)
            log.info("manual_close: cancel %s leg %s → %s", key, oid, ok)

    snap = client.get_snapshot(symbol)
    last = float(snap["last_price"])
    client.place_limit_order(symbol, trade["qty"], last * 0.995, TrdSide.SELL)
    pnl = _close_and_log(symbol, trade, trade["qty"], last, "MANUAL")
    trades.pop(symbol)
    _save_open_trades(trades)
    return {"type": "manual_close", "symbol": symbol, "qty": trade["qty"],
            "price": last, "pnl": pnl}


def edit_stop(client: MoomooClient | None, symbol: str, new_stop: float) -> dict:
    """GUI helper — adjust stop loss. If a broker stop is active, re-place it."""
    trades = _load_open_trades()
    if symbol not in trades:
        raise RuntimeError(f"no tracked position for {symbol}")
    trade = trades[symbol]
    old = trade["stop_loss"]
    new_stop = round(float(new_stop), 2)

    new_stop_id = trade.get("stop_order_id")
    if new_stop_id and client is not None:
        try:
            client.cancel_order(new_stop_id)
            new_stop_id = client.place_stop_loss(symbol, trade["qty"], new_stop)
            log.info("edit_stop: %s broker stop re-placed @ $%.2f (id=%s)",
                     symbol, new_stop, new_stop_id)
        except Exception as e:
            log.error("edit_stop: re-place failed for %s: %s — JSON updated, broker stop stale!", symbol, e)
            notifier_msg = f"⚠ {symbol} stop edit: broker re-place FAILED, manual fix needed"
            try:
                from . import notifier
                notifier.send(notifier_msg)
            except Exception:
                pass

    trade["stop_loss"] = new_stop
    trade["stop_order_id"] = new_stop_id
    _save_open_trades(trades)
    return {"type": "edit_stop", "symbol": symbol, "old": old, "new": new_stop}
