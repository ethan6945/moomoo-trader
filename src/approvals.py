"""Approval queue — analyze → notify → APPROVE → execute. Feedback 铁律.

Automated analysis (weekly self-review, reconcile drift, blacklist, and a future
Anthropic-API background optimizer) NEVER changes live behavior silently. It
enqueues a suggestion here; the owner approves/rejects (GUI panel, or the
`approve`/`reject` CLI), and the scan loop applies APPROVED items via
apply_approved(). This is the single choke point that enforces "no silent
execution" — and the reserved mount point for the future autonomous optimizer
(it writes proposals here, never to the live config).
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import db

log = logging.getLogger(__name__)

QUEUE_KEY = "pending_approvals"
# Kinds that perform a real mutation when approved (everything else is
# informational — approving just acknowledges/dismisses it).
EXECUTABLE_KINDS = {"blacklist_review", "param_change"}


def _now() -> str:
    return datetime.now().isoformat()


def list_all() -> list[dict]:
    return list(db.get_state().get(QUEUE_KEY, []))


def list_pending() -> list[dict]:
    return [a for a in list_all() if a.get("status") == "pending"]


def _dedupe_key(kind: str, payload: dict | None) -> str:
    p = payload or {}
    tag = p.get("symbol") or p.get("strategy") or p.get("key") or ""
    return f"{kind}:{tag}"


def enqueue(kind: str, detail: str, action: str, payload: dict | None = None) -> str:
    """Add a suggestion for owner approval. De-dupes against existing PENDING
    items with the same (kind, target) so repeated weekly reviews don't pile up
    duplicates. Returns the dedupe key."""
    key = _dedupe_key(kind, payload)

    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        for a in q:
            if a.get("status") == "pending" and a.get("dedupe") == key:
                return {}  # already queued — no change
        item = {
            "id": f"{int(datetime.now().timestamp() * 1000)}-{len(q)}",
            "kind": kind, "detail": detail, "action": action,
            "payload": payload or {}, "dedupe": key,
            "status": "pending", "created_at": _now(),
        }
        q.append(item)
        return {QUEUE_KEY: q}

    db.atomic_state(_apply)
    return key


def resolve(item_id: str, approved: bool) -> bool:
    """Owner marks an item approved or rejected. Returns True if found+changed."""
    found = {"v": False}

    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        for a in q:
            if a.get("id") == item_id and a.get("status") == "pending":
                a["status"] = "approved" if approved else "rejected"
                a["resolved_at"] = _now()
                found["v"] = True
        return {QUEUE_KEY: q}

    db.atomic_state(_apply)
    return found["v"]


def apply_approved() -> list[dict]:
    """Execute approved-but-unexecuted items. Called once per scan by main.py.
    Returns the items that were applied (for a Telegram confirmation)."""
    applied: list[dict] = []
    todo = [a for a in list_all()
            if a.get("status") == "approved" and not a.get("executed")]
    for a in todo:
        try:
            _execute(a)
        except Exception as e:
            log.warning("apply approved %s failed: %s", a.get("id"), e)
            continue
        a_id = a["id"]

        def _mark(state: dict, _id=a_id) -> dict:
            q = list(state.get(QUEUE_KEY, []))
            for x in q:
                if x.get("id") == _id:
                    x["executed"] = True
                    x["executed_at"] = _now()
            return {QUEUE_KEY: q}

        db.atomic_state(_mark)
        applied.append(a)
    return applied


def _execute(item: dict) -> None:
    """Perform the real mutation for an approved executable item. Informational
    kinds are no-ops (approval = acknowledgement)."""
    kind = item.get("kind")
    payload = item.get("payload", {})
    if kind == "blacklist_review":
        sym = payload.get("symbol")
        if sym:
            from . import blacklist
            blacklist.add(sym, reason="weekly self-review (owner-approved)")
            log.info("approval applied: blacklisted %s", sym)
    elif kind == "param_change":
        # Reserved for the future Anthropic optimizer: {"key": ..., "value": ...}
        # written to runtime db-state (NOT .env) so it takes effect next scan.
        key = payload.get("key")
        value = payload.get("value")
        if key is not None:
            db.update_state({f"param_{key}": value})
            log.info("approval applied: param %s = %s", key, value)
    # strategy_flag / exit_balance: informational — nothing to execute.


def purge_resolved(keep_recent: int = 50) -> None:
    """Trim executed/rejected items so the queue doesn't grow unbounded."""
    def _apply(state: dict) -> dict:
        q = list(state.get(QUEUE_KEY, []))
        pending = [a for a in q if a.get("status") == "pending"]
        done = [a for a in q if a.get("status") != "pending"][-keep_recent:]
        return {QUEUE_KEY: pending + done}

    db.atomic_state(_apply)
