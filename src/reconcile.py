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


def reconcile(broker_positions: pd.DataFrame, auto_fix: bool = True,
              client=None) -> dict:
    """Compare our internal records to broker positions.

    2026-05-30 update: auto-fix the safe drifts instead of just logging.
      • ORPHAN (broker has, we don't) → adopt with defensive stop/TP
      • GHOST  (we track, broker doesn't) → drop our stale record
      • MISMATCH (qty drift) → adopt broker's qty (broker is authoritative)
    After auto-fix, the state is internally consistent again — no need to halt.
    Halt is reserved for repeated-after-fix drift (broken state we can't recover).
    """
    try:
        our_trades = json.loads(OPEN_TRADES_FILE.read_text()) if OPEN_TRADES_FILE.exists() else {}
    except json.JSONDecodeError:
        our_trades = {}

    broker_holdings: dict[str, dict] = {}    # symbol → {qty, cost_price}
    if not broker_positions.empty:
        held = broker_positions[broker_positions["qty"].astype(float) > 0]
        for _, row in held.iterrows():
            sym = row["code"].split(".")[-1]
            broker_holdings[sym] = {
                "qty": int(float(row["qty"])),
                "cost_price": float(row.get("cost_price") or 0),
            }

    our_syms = {s for s, t in our_trades.items() if t.get("qty", 0) > 0}
    broker_syms = set(broker_holdings.keys())

    orphans = [
        {"symbol": s,
         "broker_qty": broker_holdings[s]["qty"],
         "broker_cost": broker_holdings[s]["cost_price"]}
        for s in sorted(broker_syms - our_syms)
    ]
    ghosts = [
        {"symbol": s,
         "our_qty": our_trades[s].get("qty"),
         "entry": our_trades[s].get("entry_price")}
        for s in sorted(our_syms - broker_syms)
    ]
    mismatches = []
    for s in sorted(our_syms & broker_syms):
        our_qty = int(our_trades[s].get("qty", 0))
        broker_qty = broker_holdings[s]["qty"]
        if our_qty != broker_qty:
            mismatches.append({
                "symbol": s, "our_qty": our_qty, "broker_qty": broker_qty
            })

    fixes_applied: list[dict] = []

    if auto_fix:
        now_iso = datetime.utcnow().isoformat()

        # FIX ORPHAN: add to our tracking with defensive stop/TP defaults.
        for o in orphans:
            sym = o["symbol"]
            cost = o["broker_cost"] or 0.0
            if cost <= 0:
                log.warning("Reconcile orphan %s has zero cost_price — skipping auto-add",
                            sym)
                continue
            # ATR-derived stop/TP using the bot's LIVE exit multipliers. Use the
            # REAL ATR(14) when a client is available — the old 2%-of-price
            # proxy put stops in the wrong place for every name whose true ATR
            # wasn't 2% (the first 8 adopted orphans went 1/8 at −$193 largely
            # on fabricated levels). Falls back to the proxy only if the kline
            # fetch fails.
            from . import runtime_config
            atr_proxy = cost * 0.02
            if client is not None:
                try:
                    import pandas_ta_classic as ta
                    kdf = client.get_kline(sym, bars=30)
                    real_atr = float(ta.atr(kdf["high"], kdf["low"],
                                            kdf["close"], length=14).iloc[-1])
                    if real_atr > 0 and not pd.isna(real_atr):
                        atr_proxy = real_atr
                except Exception as e:
                    log.warning("orphan %s: real ATR fetch failed (%s) — using 2%% proxy",
                                sym, e)
            our_trades[sym] = {
                "symbol": sym, "qty": o["broker_qty"],
                "entry_price": cost,
                "stop_loss": round(cost - runtime_config.sl_atr_mult() * atr_proxy, 2),
                "take_profit": round(cost + runtime_config.tp_atr_mult() * atr_proxy, 2),
                "atr": atr_proxy,
                "half_closed": False,
                "buy_order_id": None, "stop_order_id": None, "tp_order_id": None,
                "opened_at": now_iso,
                "high_water": cost, "low_water": cost,
                "ml_proba_entry": None,
                "strategy": "reconcile_orphan_recovery",
                "stacks": 1,
            }
            fixes_applied.append({"type": "ORPHAN_ADOPTED", "symbol": sym,
                                  "qty": o["broker_qty"], "cost": cost})
            log.warning("Reconcile auto-fix: adopted orphan %s qty=%d @ $%.2f",
                        sym, o["broker_qty"], cost)

        # FIX GHOST: drop stale records the broker doesn't actually hold.
        for g in ghosts:
            sym = g["symbol"]
            our_trades.pop(sym, None)
            fixes_applied.append({"type": "GHOST_DROPPED", "symbol": sym})
            log.warning("Reconcile auto-fix: dropped ghost %s", sym)

        # FIX MISMATCH: adopt broker's qty (broker side is authoritative).
        for m in mismatches:
            sym = m["symbol"]
            if sym in our_trades:
                our_trades[sym]["qty"] = m["broker_qty"]
                fixes_applied.append({"type": "QTY_ADOPTED", "symbol": sym,
                                      "old_qty": m["our_qty"], "new_qty": m["broker_qty"]})
                log.warning("Reconcile auto-fix: %s qty %d → %d",
                            sym, m["our_qty"], m["broker_qty"])

        if fixes_applied:
            try:
                OPEN_TRADES_FILE.write_text(json.dumps(our_trades, indent=2, default=str))
                # Also push through the SQLite mirror in executor.
                from . import executor
                executor._save_open_trades(our_trades)
            except Exception as e:
                log.error("reconcile auto-fix write failed: %s", e)

        # Reset severe streak — drift just got fixed.
        try:
            db.update_state({"reconcile_severe_streak": 0})
        except Exception:
            pass

    # Streak tracker (legacy 2-strike halt) — only counts UNFIXED severe state.
    unfixed_severe = (not auto_fix) and (
        bool(mismatches) or (bool(orphans) and bool(ghosts))
    )
    severe_streak = int(db.get_state().get("reconcile_severe_streak", 0) or 0)
    if unfixed_severe:
        severe_streak += 1
    else:
        severe_streak = 0
    db.update_state({"reconcile_severe_streak": severe_streak})
    halt_now = unfixed_severe and severe_streak >= 2

    result = {
        "ts": datetime.now(NY).isoformat(),
        "ok": not (orphans or ghosts or mismatches),
        "severe": unfixed_severe,
        "orphans": orphans,
        "ghosts": ghosts,
        "mismatches": mismatches,
        "fixes_applied": fixes_applied,
        "summary": (
            f"OK ({len(broker_syms)} positions)"
            if not (orphans or ghosts or mismatches)
            else f"{len(orphans)} orphan, {len(ghosts)} ghost, "
                 f"{len(mismatches)} mismatch — {len(fixes_applied)} auto-fixed"
        ),
    }

    if halt_now:
        try:
            db.atomic_state(lambda s: {"halted": True})
            log.warning("Severe reconcile drift (streak=%d, no auto-fix) → halted=True",
                        severe_streak)
        except Exception as e:
            log.error("auto-halt on reconcile failed: %s", e)

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
