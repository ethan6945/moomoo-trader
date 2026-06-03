"""Telegram push — silent fallback to console if token unset.

All message bodies route through src.i18n so the language matches the GUI
preference (data/prefs.json).  Switching the GUI to Chinese also switches
the Telegram notifications, without restarting the scheduler.
"""
from __future__ import annotations

import logging
import time

import requests

from .config import settings
from .i18n import t

log = logging.getLogger(__name__)


def send(text: str) -> None:
    if not settings.telegram_token or not settings.telegram_chat_id:
        log.info("[telegram disabled] %s", text)
        return
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            requests.post(url, data=payload, timeout=15)
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                log.warning("telegram send failed: %s", e)


def signal_msg(signal, ai_reason: str, qty: int) -> str:
    return t(
        "tg_buy",
        symbol=signal.symbol,
        price=signal.price,
        score=signal.score,
        qty=qty,
        cost=qty * signal.price,
        stop=signal.stop_loss,
        tp=signal.take_profit,
        atr=signal.atr,
        reasons="; ".join(signal.reasons),
        ai_reason=ai_reason,
    )


def skip_msg(symbol: str, reason: str) -> str:
    return t("tg_skip", symbol=symbol, reason=reason)


def trade_action_msg(action: dict) -> str:
    kind = action.get("type")
    if kind == "tp_half":
        return t("tg_tp_half", symbol=action["symbol"], qty=action["qty"],
                 price=action["price"], pnl=action.get("pnl", 0))
    if kind == "trail":
        return t("tg_trail", symbol=action["symbol"], new_stop=action["new_stop"])
    if kind in ("stop_hit", "stop_hit_bracket"):
        return t("tg_stop_hit", symbol=action["symbol"], qty=action["qty"],
                 pnl=action.get("pnl", 0), price=action["price"],
                 stop=action.get("stop", action["price"]))
    if kind in ("max_hold", "max_hold_bracket"):
        return t("tg_max_hold", symbol=action["symbol"], qty=action["qty"],
                 age_days=action.get("age_days", 0), pnl=action.get("pnl", 0))
    if kind == "tp_hit_bracket":
        # Bracket TP filled at broker — surface as a positive close.
        return t("tg_tp_half", symbol=action["symbol"], qty=action["qty"],
                 price=action["price"], pnl=action.get("pnl", 0))
    if kind == "manual_close":
        return t("tg_stop_hit", symbol=action["symbol"], qty=action["qty"],
                 pnl=action.get("pnl", 0), price=action["price"],
                 stop=action["price"])
    return str(action)
