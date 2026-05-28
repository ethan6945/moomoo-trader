"""Audit internal trade state vs broker positions.

Detects three failure modes:
  • ORPHAN    — broker has the position, our records don't (manual buy or
                executor crashed before persisting open_trades.json).
  • GHOST     — our records claim a position the broker doesn't have
                (closed manually or unrecorded close).
  • MISMATCH  — both sides know the symbol but qty differs (partial fill).

We log + persist a snapshot to data/reconcile.json so the GUI can surface it.
Auto-fix is intentionally NOT done — wrong-direction repairs cost real money.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz

from . import db
from .config import settings

log = logging.getLogger(__name__)

OPEN_TRADES_FILE = settings.root / "data" / "open_trades.json"
RECONCILE_FILE = settings.root / "data" / "reconcile.json"
NY = pytz.timezone("America/New_York")


def reconcile(broker_positions: pd.DataFrame) -> dict:
    """Compare our internal records to broker positions."""
    try:
        our_trades = json.loads(OPEN_TRADES_FILE.read_text()) if OPEN_TRADES_FILE.exists() else {}
    except json.JSONDecodeError:
        our_trades = {}

    broker_holdings: dict[str, int] = {}
    if not broker_positions.empty:
        held = broker_positions[broker_positions["qty"].astype(float) > 0]
        for _, row in held.iterrows():
            sym = row["code"].split(".")[-1]
            broker_holdings[sym] = int(float(row["qty"]))

    our_syms = {s for s, t in our_trades.items() if t.get("qty", 0) > 0}
    broker_syms = set(broker_holdings.keys())

    orphans = [
        {"symbol": s, "broker_qty": broker_holdings[s]}
        for s in sorted(broker_syms - our_syms)
    ]
    ghosts = [
        {
            "symbol": s,
            "our_qty": our_trades[s].get("qty"),
            "entry": our_trades[s].get("entry_price"),
        }
        for s in sorted(our_syms - broker_syms)
    ]
    mismatches = []
    for s in sorted(our_syms & broker_syms):
        our_qty = int(our_trades[s].get("qty", 0))
        broker_qty = broker_holdings[s]
        if our_qty != broker_qty:
            mismatches.append({
                "symbol": s, "our_qty": our_qty, "broker_qty": broker_qty
            })

    # Severity rules — when do we auto-halt?
    #   • mismatch by ≥ 1 share is always severe (qty drift = unknown exposure)
    #   • both orphan AND ghost simultaneously = our records are out of sync in both directions
    #   • a single orphan or single ghost = warn but allow trading (low risk: just record drift)
    severe = bool(mismatches) or (bool(orphans) and bool(ghosts))
    # 2026-05-28: 2-strike halt rule — paper trading routinely shows transient
    # mismatches when MooMoo's broker side hasn't caught up to a just-placed
    # order. Halting on a single observation produced false stops in paper.
    # Now we increment a streak counter; halt only fires after 2 consecutive
    # severe reconciles. A clean reconcile resets the counter.
    severe_streak = int(db.get_state().get("reconcile_severe_streak", 0) or 0)
    if severe:
        severe_streak += 1
    else:
        severe_streak = 0
    db.update_state({"reconcile_severe_streak": severe_streak})
    halt_now = severe and severe_streak >= 2

    result = {
        "ts": datetime.now(NY).isoformat(),
        "ok": not (orphans or ghosts or mismatches),
        "severe": severe,
        "orphans": orphans,
        "ghosts": ghosts,
        "mismatches": mismatches,
        "summary": (
            f"OK ({len(broker_syms)} positions)"
            if not (orphans or ghosts or mismatches)
            else f"{len(orphans)} orphan, {len(ghosts)} ghost, {len(mismatches)} mismatch"
        ),
    }

    # Auto-halt on severe drift — but only after 2 consecutive observations,
    # so a one-off paper-trading hiccup doesn't shut down trading.
    if halt_now:
        try:
            db.atomic_state(lambda s: {"halted": True})
            log.warning("Severe reconcile drift (streak=%d) → halted=True "
                        "(resolve via GUI then click 'Halted' to reset)",
                        severe_streak)
        except Exception as e:
            log.error("auto-halt on reconcile failed: %s", e)
    elif severe:
        log.warning("Reconcile severe (streak=%d) — watching but not halted yet",
                    severe_streak)

    RECONCILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECONCILE_FILE.write_text(json.dumps(result, indent=2))
    return result


def log_reconcile(result: dict) -> str:
    """Pretty-print issues to log. Returns short summary for notifier."""
    if result["ok"]:
        log.info("Reconcile OK — %s", result["summary"])
        return ""

    log.warning("⚠ RECONCILE ISSUES — %s", result["summary"])
    msg_lines = ["*Reconcile alert*"]
    for o in result["orphans"]:
        line = f"ORPHAN: {o['symbol']} qty={o['broker_qty']} in broker, not tracked"
        log.warning("  %s", line)
        msg_lines.append(line)
    for g in result["ghosts"]:
        entry = g.get("entry") or 0
        line = f"GHOST: {g['symbol']} qty={g['our_qty']} @ ${entry:.2f} tracked, broker empty"
        log.warning("  %s", line)
        msg_lines.append(line)
    for m in result["mismatches"]:
        line = f"MISMATCH: {m['symbol']} ours={m['our_qty']} broker={m['broker_qty']}"
        log.warning("  %s", line)
        msg_lines.append(line)
    return "\n".join(msg_lines)
