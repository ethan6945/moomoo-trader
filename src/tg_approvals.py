"""Telegram approval interface — approve/reject from your phone.

Posts each pending suggestion as a card with [✅ 批准][✖ 拒绝] inline buttons,
polls Telegram for your taps, and resolves the approval in the SHARED db queue
(src.approvals). The GUI reads/writes that same queue, so an action taken in
Telegram shows up in the GUI automatically (the GUI panel auto-refreshes), and
vice-versa. Approved items are applied by the scan loop's apply_approved().

Run `sync()` from a scheduler job every ~60s. No webhook — uses getUpdates
long-poll with a persisted offset, so it coexists with notifier.send().
"""
from __future__ import annotations

import logging

import requests

from . import approvals, db
from .config import settings

log = logging.getLogger(__name__)

OFFSET_KEY = "tg_update_offset"


def _enabled() -> bool:
    return bool(settings.telegram_token and settings.telegram_chat_id)


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_token}/{method}"


def _post(method: str, **payload):
    try:
        return requests.post(_api(method), json=payload, timeout=15)
    except Exception as e:
        log.warning("telegram %s failed: %s", method, e)
        return None


def _send_card(item: dict) -> int | None:
    text = (f"🔔 *待批准建议*\n\n{item.get('detail', '')}\n\n→ {item.get('action', '')}\n\n"
            f"`{item.get('id')}`")
    kb = {"inline_keyboard": [[
        {"text": "✅ 批准", "callback_data": f"ap:{item['id']}"},
        {"text": "✖ 拒绝", "callback_data": f"rj:{item['id']}"},
    ]]}
    r = _post("sendMessage", chat_id=settings.telegram_chat_id, text=text,
              parse_mode="Markdown", reply_markup=kb)
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def _set_msg_id(item_id: str, message_id: int) -> None:
    def _apply(state: dict) -> dict:
        q = list(state.get(approvals.QUEUE_KEY, []))
        for a in q:
            if a.get("id") == item_id:
                a["tg_message_id"] = message_id
        return {approvals.QUEUE_KEY: q}
    db.atomic_state(_apply)


def post_pending() -> int:
    """Send a Telegram card for each pending item not yet posted. Returns count."""
    n = 0
    for a in approvals.list_pending():
        if not a.get("tg_message_id"):
            mid = _send_card(a)
            if mid:
                _set_msg_id(a["id"], mid)
                n += 1
    return n


def poll() -> int:
    """Process button taps: resolve + edit the card to show the outcome.
    Returns the number of actions processed."""
    offset = db.get_state().get(OFFSET_KEY)
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(_api("getUpdates"), params=params, timeout=20).json()
    except Exception as e:
        log.warning("telegram getUpdates failed: %s", e)
        return 0
    if not resp.get("ok"):
        return 0

    processed = 0
    last = offset
    for upd in resp.get("result", []):
        last = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        if not (data.startswith("ap:") or data.startswith("rj:")):
            continue
        approve = data.startswith("ap:")
        item_id = data.split(":", 1)[1]
        ok = approvals.resolve(item_id, approve)
        toast = (("✅ 已批准 — 下次扫描执行" if approve else "✖ 已拒绝")
                 if ok else "已处理过")
        _post("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
        msg = cq.get("message", {})
        mid = msg.get("message_id")
        if ok and mid:
            tag = "✅ 已批准" if approve else "✖ 已拒绝"
            _post("editMessageText", chat_id=settings.telegram_chat_id,
                  message_id=mid, parse_mode="Markdown",
                  text=f"{msg.get('text', '')}\n\n*[{tag}]*")
        processed += 1
    if last is not None and last != offset:
        db.update_state({OFFSET_KEY: last})
    return processed


def sync() -> dict:
    """One cycle: post any new pending cards, then process taps. Scheduler job."""
    if not _enabled():
        return {"posted": 0, "processed": 0, "note": "telegram disabled"}
    posted = post_pending()
    processed = poll()
    if posted or processed:
        log.info("tg_approvals: posted %d, processed %d", posted, processed)
    return {"posted": posted, "processed": processed}
