"""Unit tests for the pyramiding / stacking mechanics in backtest_v3 (2026-05-31).

These pin the PURE helpers that model the live `open_position` add-on, so a
future edit can't silently change how a winner gets stacked:

  A. _stack_gate  — WHEN a held name qualifies for an add-on: room under the
     per-symbol stack cap AND unrealised profit ≥ min_r × initial risk
     (entry − stop). A non-positive R-unit (stop trailed up to/above entry)
     never qualifies; a losing position never qualifies.
  B. _merge_addon — HOW the add-on folds into the held Trade: share-weighted
     average entry (2 dp), stop/TP raised-never-lowered, qty + qty_initial
     re-anchored to the new total, ATR re-anchored, stacks++, highest_high
     lifted; and the returned cash debit == s_qty × s_entry (so the cash wall
     still bites on a real $5k account).
  C. live-config gate — the gate behaves correctly at the ACTUAL .env settings
     (max_stacks_per_symbol, stack_min_r_multiple) the bot runs with.

The incumbent `simulate_time_stepped` is intentionally NOT touched here — it is
the frozen oracle, and scripts/engine_compare.py is the standing diff-test that
fails if it ever drifts. Pyramiding lives only in simulate_v3's strategy lens
(use_pyramiding, off in parity_mode), so it can't perturb that parity check.

Standalone (no pytest):  .venv/bin/python3 scripts/test_pyramiding.py
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest import Trade
from src.backtest_v3 import _stack_gate, _merge_addon
from src.config import settings

_results: list[bool] = []


def check(label, cond, detail="") -> bool:
    ok = bool(cond)
    _results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label:<54}{detail}")
    return ok


def _pos(qty, entry, stop, tp, *, atr=2.0, hh=None, stacks=1, qty_initial=None) -> Trade:
    """A held position, only the fields the merge touches matter."""
    return Trade(
        symbol="TEST", entry_bar=1, entry_date="2026-01-01",
        entry_price=entry, stop_loss=stop, take_profit=tp, qty=qty,
        qty_initial=qty if qty_initial is None else qty_initial,
        atr_at_entry=atr, highest_high=entry if hh is None else hh, stacks=stacks,
    )


# ── A. _stack_gate — the WHEN ────────────────────────────────────────────────
def test_gate() -> None:
    print("\nA · _stack_gate (when a held name qualifies for an add-on)")
    # entry 100, stop 90 → r_unit = 10. min_r 0.5 → trigger at last_px ≥ 105.
    g = lambda last, stacks=1, mx=5, mr=0.5, e=100.0, s=90.0: _stack_gate(
        stacks, e, s, last, max_stacks=mx, min_r=mr)

    check("at exactly +0.5R qualifies", g(105.0) is True, "  last=105 → 0.50R")
    check("just below +0.5R blocked", g(104.99) is False, "  last=104.99 → 0.499R")
    check("well above threshold qualifies", g(120.0) is True, "  last=120 → 2.0R")
    check("losing position blocked", g(98.0) is False, "  last=98 → −0.2R")
    check("flat (==entry) blocked", g(100.0) is False, "  last=100 → 0.0R")
    # stack cap
    check("at cap blocked even deep in profit", g(200.0, stacks=5, mx=5) is False,
          "  stacks=5/5")
    check("one below cap allowed", g(120.0, stacks=4, mx=5) is True, "  stacks=4/5")
    # degenerate R-unit (stop trailed to/above entry) never qualifies
    check("r_unit == 0 (stop==entry) blocked", g(300.0, e=100.0, s=100.0) is False,
          "  stop==entry")
    check("r_unit < 0 (stop>entry) blocked", g(300.0, e=100.0, s=105.0) is False,
          "  stop>entry")


# ── B. _merge_addon — the HOW ────────────────────────────────────────────────
def test_merge() -> None:
    print("\nB · _merge_addon (how an add-on folds into the held position)")

    # B1 even-weight average entry
    p = _pos(10, 100.0, 95.0, 140.0)
    debit = _merge_addon(p, s_entry=120.0, s_stop=98.0, s_tp=150.0, s_qty=10, s_atr=3.0)
    check("even-weight avg entry → 110.00", p.entry_price == 110.00, f"  got {p.entry_price}")
    check("qty summed → 20", p.qty == 20, f"  got {p.qty}")
    check("qty_initial re-anchored → 20", p.qty_initial == 20, f"  got {p.qty_initial}")
    check("stacks incremented → 2", p.stacks == 2, f"  got {p.stacks}")
    check("stop raised 95→98", p.stop_loss == 98.0, f"  got {p.stop_loss}")
    check("TP raised 140→150", p.take_profit == 150.0, f"  got {p.take_profit}")
    check("ATR re-anchored 2.0→3.0", p.atr_at_entry == 3.0, f"  got {p.atr_at_entry}")
    check("highest_high lifted to add-on entry 120", p.highest_high == 120.0,
          f"  got {p.highest_high}")
    check("cash debit == s_qty×s_entry (1200)", debit == 1200.0, f"  got {debit}")

    # B2 uneven weights
    p = _pos(30, 100.0, 90.0, 130.0)
    _merge_addon(p, s_entry=140.0, s_stop=80.0, s_tp=120.0, s_qty=10, s_atr=4.0)
    check("uneven-weight avg (30@100 + 10@140) → 110.00", p.entry_price == 110.00,
          f"  got {p.entry_price}")

    # B3 rounding to 2 dp: (6×100 + 1×105)/7 = 100.7142857 → 100.71
    p = _pos(6, 100.0, 90.0, 130.0)
    _merge_addon(p, s_entry=105.0, s_stop=90.0, s_tp=130.0, s_qty=1, s_atr=2.0)
    check("avg entry rounded to 2 dp → 100.71", p.entry_price == 100.71,
          f"  got {p.entry_price}")

    # B4 stop/TP never LOOSEN when the add-on's are worse
    p = _pos(10, 100.0, 95.0, 140.0)
    _merge_addon(p, s_entry=120.0, s_stop=92.0, s_tp=130.0, s_qty=10, s_atr=2.0)
    check("stop NOT lowered (95 kept vs add-on 92)", p.stop_loss == 95.0,
          f"  got {p.stop_loss}")
    check("TP NOT lowered (140 kept vs add-on 130)", p.take_profit == 140.0,
          f"  got {p.take_profit}")

    # B5 highest_high not pulled DOWN by a lower add-on entry
    p = _pos(10, 100.0, 95.0, 140.0, hh=130.0)
    _merge_addon(p, s_entry=120.0, s_stop=95.0, s_tp=140.0, s_qty=10, s_atr=2.0)
    check("highest_high kept at 130 (add-on entry 120 lower)", p.highest_high == 130.0,
          f"  got {p.highest_high}")

    # B6 two successive stacks compound correctly
    p = _pos(10, 100.0, 95.0, 140.0)
    _merge_addon(p, s_entry=120.0, s_stop=98.0, s_tp=150.0, s_qty=10, s_atr=3.0)  # →20@110
    _merge_addon(p, s_entry=130.0, s_stop=99.0, s_tp=160.0, s_qty=20, s_atr=3.5)  # →40@120
    check("double-stack avg (20@110 + 20@130) → 120.00", p.entry_price == 120.00,
          f"  got {p.entry_price}")
    check("double-stack qty → 40", p.qty == 40, f"  got {p.qty}")
    check("double-stack stacks → 3", p.stacks == 3, f"  got {p.stacks}")


# ── C. gate at the LIVE .env config ──────────────────────────────────────────
def test_live_config() -> None:
    print(f"\nC · gate at live config (max_stacks={settings.max_stacks_per_symbol}, "
          f"min_r={settings.stack_min_r_multiple}R)")
    mx = settings.max_stacks_per_symbol
    mr = settings.stack_min_r_multiple
    # entry 100, stop 90 → r_unit 10; threshold price = 100 + mr×10
    trig = 100.0 + mr * 10.0
    check("fresh winner at threshold qualifies",
          _stack_gate(1, 100.0, 90.0, trig, max_stacks=mx, min_r=mr) is True,
          f"  last={trig:.2f}")
    check("a hair under threshold blocked",
          _stack_gate(1, 100.0, 90.0, trig - 0.01, max_stacks=mx, min_r=mr) is False,
          f"  last={trig-0.01:.2f}")
    check("name already at max stacks blocked",
          _stack_gate(mx, 100.0, 90.0, 1000.0, max_stacks=mx, min_r=mr) is False,
          f"  stacks={mx}/{mx}")


def main() -> int:
    print("pyramiding mechanics — _stack_gate (when) + _merge_addon (how)")
    test_gate()
    test_merge()
    test_live_config()
    passed = sum(_results)
    total = len(_results)
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
