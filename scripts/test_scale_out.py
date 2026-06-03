"""Regression tests for the 3-tranche scale-out wired into _manage_one (2026-05-31).

Exercised through the REAL executor soft-management path with a fake broker:

  S0. USE_SCALE_OUT off (default) → legacy TP_HALF is byte-for-byte unchanged:
      at the single TP it sells HALF and sets half_closed.
  S1. below TP1 → nothing banked.
  S2. at TP1 (<TP2) → bank 1/3 of the ORIGINAL lot, tp1_done set, qty -= 1/3.
  S3. through TP2 in one tick → bank 1/3 (TP1) then 1/3 (TP2) the same tick.
  S4. runner: once price reaches the single TP the remaining lot closes WHOLE
      (reason TP) and the position is removed — matching backtest_v3.
  S5. NO trailing under scale-out: get_kline (used only by the legacy trail) is
      never called on the scale-out path.

R unit = entry − initial stop. With entry=100, stop=90 → R=10, so TP1_R=3 → $130,
TP2_R=6 → $160.

Standalone (no pytest): `.venv/bin/python3 scripts/test_scale_out.py`.
"""
from __future__ import annotations
import logging, sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

import pandas as pd
import src.executor as ex
from src.config import settings as real_settings
from moomoo import TrdSide


class FakeClient:
    """Records SELL orders. get_kline is tripwired: the scale-out path must
    never call it (only the legacy trail does)."""
    def __init__(self, price):
        self._price = price
        self.sells: list[tuple] = []
        self.kline_calls = 0

    def get_snapshot(self, symbol):
        return {"last_price": self._price}

    def place_limit_order(self, symbol, qty, price, side):
        if side == TrdSide.SELL:
            self.sells.append((symbol, qty, price))
        return "fake_oid"

    def get_kline(self, symbol, bars=30):
        self.kline_calls += 1
        # 30 gently-rising bars so the legacy trail can compute ema/atr cleanly.
        closes = [100 + i * 0.1 for i in range(bars)]
        return pd.DataFrame({
            "close": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
        })


def _stub_env():
    ex._close_and_log = lambda *a, **k: 0.0
    ex._is_stalled = lambda *a, **k: False
    ex._business_days_between = lambda a, b: 0   # age 0 → no max-hold/stall


def make_trade(qty=30, entry=100.0, stop=90.0, tp=200.0):
    return {
        "symbol": "AAPL", "qty": qty, "qty_initial": qty,
        "entry_price": entry, "stop_loss": stop, "take_profit": tp,
        "init_risk_per_share": entry - stop,
        "tp1_done": False, "tp2_done": False, "half_closed": False,
        "high_water": entry, "low_water": entry, "atr": 2.0,
        "opened_at": datetime.utcnow().isoformat(),
    }


def run_manage(price, trade, *, scale_out):
    """Drive one _manage_one tick; returns (client, trades, actions)."""
    _stub_env()
    ex._settings = (SimpleNamespace(use_scale_out=True, tp1_r=3.0, tp2_r=6.0,
                                    max_hold_days=7)
                    if scale_out else real_settings)
    try:
        client = FakeClient(price)
        trades = {"AAPL": trade}
        actions: list[dict] = []
        ex._manage_one(client, "AAPL", trade, trades, actions)
        return client, trades, actions
    finally:
        ex._settings = real_settings


def check(label, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def main() -> int:
    print("scale-out (_manage_one) — tranche banking, runner close, legacy parity\n")
    results = []

    # S0 — scale-out OFF (default settings): legacy TP_HALF unchanged.
    t = make_trade(qty=30, tp=150.0)
    c, trades, acts = run_manage(160.0, t, scale_out=False)
    results.append(check(
        "S0 off: at TP sells HALF, half_closed set, no scale_out action",
        c.sells == [("AAPL", 15, 160.0)] and t["half_closed"] is True
        and t["qty"] == 15 and not any(a["type"] == "scale_out" for a in acts)))

    # S1 — below TP1 (130): nothing banked.
    t = make_trade()
    c, trades, acts = run_manage(120.0, t, scale_out=True)
    results.append(check(
        "S1 on: price<TP1 → no sell, qty intact, tp1_done False",
        c.sells == [] and t["qty"] == 30 and t["tp1_done"] is False))

    # S2 — at TP1 (130) but below TP2 (160): bank exactly 1/3.
    t = make_trade()
    c, trades, acts = run_manage(135.0, t, scale_out=True)
    results.append(check(
        "S2 on: TP1 banks 1/3 (10), tp1_done set, tp2_done False, qty=20",
        c.sells == [("AAPL", 10, 135.0)] and t["tp1_done"] is True
        and t["tp2_done"] is False and t["qty"] == 20
        and any(a["type"] == "scale_out" and a["tranche"] == 1 for a in acts)))

    # S3 — through TP2 (160) in one tick: bank 1/3 then 1/3 (same tick).
    t = make_trade()
    c, trades, acts = run_manage(165.0, t, scale_out=True)
    results.append(check(
        "S3 on: TP1+TP2 same tick → two 10-lot sells, qty=10, both done",
        c.sells == [("AAPL", 10, 165.0), ("AAPL", 10, 165.0)]
        and t["tp1_done"] and t["tp2_done"] and t["qty"] == 10
        and "AAPL" in trades))

    # S4 — runner: single TP in range → remaining lot closes WHOLE, pos removed.
    t = make_trade(tp=150.0)   # TP1=130, TP2=160, single TP=150
    c, trades, acts = run_manage(155.0, t, scale_out=True)
    # 155 ≥ TP1(130) banks 10; 155 < TP2(160) so no TP2; 155 ≥ TP(150) → runner 20 closes.
    results.append(check(
        "S4 on: TP1 banks 10 then runner 20 closes at TP, position removed",
        c.sells == [("AAPL", 10, 155.0), ("AAPL", 20, 155.0)]
        and "AAPL" not in trades
        and any(a["type"] == "tp_full" for a in acts)))

    # S5 — no trail on scale-out path: get_kline never called.
    t = make_trade()
    c, trades, acts = run_manage(135.0, t, scale_out=True)
    results.append(check(
        "S5 on: scale-out path never calls get_kline (no runner trail)",
        c.kline_calls == 0))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
