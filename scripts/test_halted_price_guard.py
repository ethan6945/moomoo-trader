"""Robustness regression tests for manage_open_trades (2026-05-30).

Three guarantees are pinned here, all exercised through the real
manage_open_trades → _manage_one code path with a fake broker client:

  A. Halted/anomalous price guard — a snapshot last_price of 0 / NaN / negative
     must NOT place a ~$0 phantom sell; the position is retained and retried.
  B. Bracket-path stall-out — a REAL (bracketed) position that has gone nowhere
     for >= STALL_MIN_DAYS gets culled (bracket cancelled + force-closed) so its
     slot/capital is freed, instead of idling until the 7-day max-hold.
  C. Per-symbol isolation — if managing one symbol throws, the OTHER symbols
     are still managed this scan (one bad name can't freeze the loop).

Standalone (no pytest): `python3 scripts/test_halted_price_guard.py`.
"""
from __future__ import annotations
import logging, sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.CRITICAL)   # silence guard/loop warnings

import src.executor as ex
import src.blacklist as bl
from moomoo import TrdSide


class FakeClient:
    """Minimal broker stand-in. `prices` is a scalar (all symbols) or a
    {symbol: price} map. `raise_on_filled` makes is_order_filled blow up, used
    to simulate a broker error mid-loop for the isolation test."""
    def __init__(self, prices, *, filled=False, raise_on_filled=False):
        self._prices = prices
        self._filled = filled
        self._raise_on_filled = raise_on_filled
        self.sells: list[tuple] = []
        self.cancels: list[str] = []

    def _price_for(self, symbol):
        return self._prices.get(symbol) if isinstance(self._prices, dict) else self._prices

    def get_snapshot(self, symbol):
        return {"last_price": self._price_for(symbol)}

    def is_order_filled(self, order_id):
        if self._raise_on_filled:
            raise RuntimeError(f"broker error checking order {order_id}")
        return self._filled

    def place_limit_order(self, symbol, qty, price, side):
        if side == TrdSide.SELL:
            self.sells.append((symbol, qty, price))
        return "fake_oid"

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return True


def _install_fakes(store: dict, age_business_days: int = 0):
    """Route load/save through an in-memory dict and stub everything that would
    otherwise touch SQLite / the blacklist file. Force a deterministic position
    age so stall-out / max-hold logic doesn't depend on the wall clock."""
    ex._load_open_trades = lambda: {k: dict(v) for k, v in store.items()}

    def _save(trades):
        store.clear()
        store.update({k: dict(v) for k, v in trades.items()})
    ex._save_open_trades = _save
    ex.cancel_stale_orders = lambda client: []
    ex._close_and_log = lambda *a, **k: 0.0
    ex._business_days_between = lambda a, b: age_business_days   # also seen by _is_stalled
    bl.get_blacklist = lambda: {}


def _soft_position(stop=95.0, tp=140.0) -> dict:
    """One open, no-bracket, soft-tracked position (AAPL)."""
    return {
        "AAPL": {
            "symbol": "AAPL", "entry_price": 100.0, "stop_loss": stop,
            "take_profit": tp, "qty": 10, "half_closed": False,
            "high_water": 100.0, "low_water": 100.0, "atr": 2.0,
            "opened_at": datetime.utcnow().isoformat(),
        }
    }


# ── Group A: halted/anomalous price guard (soft SL path) ──────────────────────
def case_price_guard(label, last_price, expect_sell) -> bool:
    store = _soft_position()
    _install_fakes(store, age_business_days=0)
    client = FakeClient(last_price)
    ex.manage_open_trades(client)

    sold = len(client.sells) > 0
    retained = "AAPL" in store
    ok = (sold == expect_sell) and (retained != expect_sell)
    print(f"[{'PASS' if ok else 'FAIL'}] A {label:<40} last={last_price!r:>7}  "
          f"sold={sold} retained={retained}")
    return ok


# ── Group B: bracket-path stall-out ───────────────────────────────────────────
def _bracket_position(high_water) -> dict:
    return {
        "NVDA": {
            "symbol": "NVDA", "entry_price": 100.0, "stop_loss": 95.0,
            "take_profit": 140.0, "qty": 10, "half_closed": False,
            "high_water": high_water, "low_water": 99.0, "atr": 2.0,
            "opened_at": (datetime.utcnow() - timedelta(days=12)).isoformat(),
            "stop_order_id": "S1", "tp_order_id": "T1",   # → has_bracket
        }
    }


def case_bracket_stall(label, high_water, age, expect_close) -> bool:
    store = _bracket_position(high_water)
    _install_fakes(store, age_business_days=age)
    # price 100 → between stop(95) and TP(140); bracket legs not filled.
    client = FakeClient({"NVDA": 100.0}, filled=False)
    actions = ex.manage_open_trades(client)

    closed = "NVDA" not in store
    reasons = {a.get("type") for a in actions}
    cancelled_bracket = {"S1", "T1"}.issubset(set(client.cancels))
    ok = (closed == expect_close)
    if expect_close:
        ok = ok and cancelled_bracket and ("stall_out" in reasons)
    print(f"[{'PASS' if ok else 'FAIL'}] B {label:<40} "
          f"closed={closed} reasons={sorted(reasons)} cancels={client.cancels}")
    return ok


# ── Group C: per-symbol isolation ─────────────────────────────────────────────
def case_isolation() -> bool:
    # BAD has a bracket → _check_bracket_fills → is_order_filled raises.
    # GOOD is soft with price 90 <= stop 95 → must still get sold despite BAD.
    store = {
        "BAD": {
            "symbol": "BAD", "entry_price": 100.0, "stop_loss": 95.0,
            "take_profit": 140.0, "qty": 10, "half_closed": False,
            "high_water": 100.0, "low_water": 99.0, "atr": 2.0,
            "opened_at": datetime.utcnow().isoformat(),
            "stop_order_id": "S9", "tp_order_id": "T9",
        },
        "GOOD": {
            "symbol": "GOOD", "entry_price": 100.0, "stop_loss": 95.0,
            "take_profit": 140.0, "qty": 5, "half_closed": False,
            "high_water": 100.0, "low_water": 90.0, "atr": 2.0,
            "opened_at": datetime.utcnow().isoformat(),
        },
    }
    _install_fakes(store, age_business_days=0)
    client = FakeClient({"BAD": 100.0, "GOOD": 90.0}, raise_on_filled=True)

    try:
        ex.manage_open_trades(client)   # must NOT raise — loop isolates BAD
        raised = False
    except Exception:
        raised = True

    good_sold = any(s[0] == "GOOD" for s in client.sells)
    good_gone = "GOOD" not in store
    bad_kept = "BAD" in store
    ok = (not raised) and good_sold and good_gone and bad_kept
    print(f"[{'PASS' if ok else 'FAIL'}] C isolation: BAD throws, GOOD still managed   "
          f"raised={raised} good_sold={good_sold} bad_kept={bad_kept}")
    return ok


def main() -> int:
    print("manage_open_trades robustness — halted guard / bracket stall / isolation\n")
    results = [
        # A — bad prices never sell; valid prices behave normally.
        case_price_guard("halted 0.0", 0.0, expect_sell=False),
        case_price_guard("halted 0 (int)", 0, expect_sell=False),
        case_price_guard("anomalous -5.0", -5.0, expect_sell=False),
        case_price_guard("anomalous NaN", float("nan"), expect_sell=False),
        case_price_guard("anomalous None", None, expect_sell=False),
        case_price_guard("control 90 (below stop → sell)", 90.0, expect_sell=True),
        case_price_guard("control 110 (above stop → hold)", 110.0, expect_sell=False),
        # B — bracket stall-out.
        case_bracket_stall("stalled (HW=100.5, age=4) → cull", 100.5, 4, expect_close=True),
        case_bracket_stall("progressing (HW=115, age=4) → keep", 115.0, 4, expect_close=False),
        case_bracket_stall("fresh (HW=100.5, age=1) → keep", 100.5, 1, expect_close=False),
        # C — isolation.
        case_isolation(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
