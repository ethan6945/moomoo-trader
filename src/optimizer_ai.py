"""Autonomous optimizer (DeepSeek v4-flash) — PROPOSES, never decides.

Given the weekly real-fill self-review, it asks DeepSeek for parameter-change
proposals (entry threshold / TP / SL) and writes them to the APPROVAL QUEUE.
The owner approves in the GUI/CLI; only then does a change take effect (runtime
override, no restart). This is the feedback-铁律 implementation of requirement
#8 — the system can optimize itself, but every change passes through the owner.

DeepSeek's API is OpenAI-compatible; we call it over plain HTTPS (requests) so
no extra SDK dependency is needed. With DEEPSEEK_API_KEY blank, this is a no-op
(the rules-based suggestions in self_review still run).
"""
from __future__ import annotations

import json
import logging

from . import approvals, runtime_config
from .config import settings

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a quantitative trading risk optimizer for a $5k CASH day-trading "
    "bot (no leverage, max 5 positions, US semis). You tune ONLY these params: "
    "entry_threshold (55-85), tp_atr_mult (2-12), sl_atr_mult (2-6). The owner "
    "wants higher $/day toward a $50/day target WITHOUT reckless risk, and "
    "dislikes over-conservative gatekeeping. Propose 0-3 SMALL, justified "
    "changes based on the real-fill review. Reply ONLY with a JSON array of "
    '{"key","value","rationale"} objects. Empty array if no change is warranted.'
)


def _current_params() -> dict:
    return {
        "entry_threshold": runtime_config.entry_threshold(),
        "tp_atr_mult": runtime_config.tp_atr_mult(),
        "sl_atr_mult": runtime_config.sl_atr_mult(),
    }


def _call_deepseek(review: dict) -> list[dict]:
    """Call DeepSeek chat completions. Returns parsed proposals or []."""
    if not settings.deepseek_key:
        return []
    import requests
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content":
                "Current params: " + json.dumps(_current_params())
                + "\nReal-fill review (last week): " + json.dumps({
                    k: review.get(k) for k in
                    ("n_trades", "per_day", "win_rate", "avg_r_multiple",
                     "by_strategy", "by_exit", "target_note")
                })},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    try:
        r = requests.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=30,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        # Tolerate a ```json fence.
        if content.startswith("```"):
            content = content.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        proposals = json.loads(content)
        return proposals if isinstance(proposals, list) else []
    except Exception as e:
        log.warning("DeepSeek optimizer call failed: %s", e)
        return []


def propose_from_review(review: dict) -> int:
    """Turn the weekly review into APPROVAL-queued param proposals.

    Returns the number of valid proposals enqueued. No-op (0) without a key."""
    proposals = _call_deepseek(review)
    n = 0
    for p in proposals:
        key, value = p.get("key"), p.get("value")
        if not runtime_config.is_valid(key, value):
            log.info("optimizer: rejected invalid proposal %s=%s", key, value)
            continue
        cur = _current_params().get(key)
        approvals.enqueue(
            kind="param_change",
            detail=f"DeepSeek: {key} {cur} → {value} — {p.get('rationale','')}",
            action=f"Set {key} = {value} (live, no restart)",
            payload={"key": key, "value": float(value)},
        )
        n += 1
    if n:
        log.info("optimizer: enqueued %d param proposal(s) for approval", n)
    return n
