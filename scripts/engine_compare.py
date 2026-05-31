"""engine_compare — the STANDING DIFFERENTIAL REGRESSION TEST for the unified
engine. Runs the incumbent (frozen oracle) SIDE-BY-SIDE with backtest_v2 on
identical data + config, prints where they agree / diverge, and HARD-FAILS
(exit 1) if the mechanism-validation lens stops reproducing the oracle.

The "two engines in one" idea: simulate_v2 has two lenses of the SAME code —
  • parity_mode=True  → mechanism validation. Sizes like the incumbent, no cash
                        wall, no pyramiding. MUST match the oracle to the dollar.
  • enforce_cash=True → strategy validation. Real $5k cash balance + MTM-DD (+
                        optional pyramiding) — the honest, deleveraged result.

Three rows per (window × config):
  A · Active        = simulate_time_stepped  (the incumbent oracle; the GUI engine)
  B · V2 parity     = simulate_v2(parity_mode=True) — independent mechanics that
                      MUST reproduce A to the dollar AND to the trade. If it does,
                      neither engine hides a fill/exit/accounting bug. This is the
                      assertion that gates the whole file (PARITY PASS/FAIL).
  C · V2 cash-on    = simulate_v2(enforce_cash=True) — adds a REAL cash balance:
                      a position can only be opened with cash on hand. The gap
                      C-vs-B is the implicit-leverage premium the incumbent bakes
                      in (it can hold ~2.5× notional on the $5k seed, cost-free).

Also shows mark-to-market DD (V2 only) — the honest peak-to-trough including
open losers, which the realized-PnL DD of the incumbent understates.

Run:  .venv/bin/python3 scripts/engine_compare.py   (exit 0 = parity holds)
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache
from src.backtest_v2 import simulate_v2
from src.config import settings

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

TICKERS = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]


def base(days):
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0, tickers=TICKERS,
        account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False,
    )


# The two configs worth validating: the live baseline, and the combo-sweep
# winner that claimed > $100/day on 180d.
CONFIGS = [
    ("BASELINE (.env)", {}),
    ("reg2.0+scaleout", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                             use_scale_out=True, tp1_r=3.0, tp2_r=6.0)),
]


def _row(label, m, days, *, is_active):
    """Normalize either engine's metrics dict into the comparison columns."""
    net = m.get("net_pnl_usd", 0)
    return {
        "label": label,
        "trd": m.get("total_trades", 0),
        "wr": m.get("win_rate_pct", 0),
        "pf": m.get("profit_factor", 0),
        "net": net,
        "daily": net / days,
        "ddr": m.get("max_drawdown_pct", 0),                  # realized DD
        "ddm": None if is_active else m.get("max_dd_mtm_pct", 0),
        "feq": settings.account_usd + net,
        "gross": None if is_active else m.get("peak_gross_exposure_pct", 0),
        "cashlim": None if is_active else m.get("n_cash_limited_entries", 0),
        "reasons": m.get("exit_reasons", {}),
    }


def show(rows, days, cfg_name):
    print(f"\n{'─'*104}")
    print(f"  {days}-DAY · {cfg_name}   (seed ${settings.account_usd:,.0f}, "
          f"mpp={settings.max_position_pct:.2f}, rpt={settings.risk_per_trade:.2f}, cap=5 slots)")
    print(f"{'─'*104}")
    print(f"{'engine':<16}{'trd':>4}{'WR%':>7}{'PF':>6}{'NetPnL':>10}{'$/day':>8}"
          f"{'realDD%':>9}{'mtmDD%':>8}{'finalEq':>10}{'peakGr%':>9}{'cashLim':>8}")
    print("-" * 104)
    for r in rows:
        ddm = f"{r['ddm']:.2f}" if r['ddm'] is not None else "—"
        gr = f"{r['gross']:.0f}" if r['gross'] is not None else "—"
        cl = f"{r['cashlim']}" if r['cashlim'] is not None else "—"
        print(f"{r['label']:<16}{r['trd']:>4}{r['wr']:>7.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+10.0f}{r['daily']:>+8.1f}{r['ddr']:>9.2f}{ddm:>8}"
              f"{r['feq']:>10,.0f}{gr:>9}{cl:>8}")

    # divergence read-outs
    a = rows[0]; b = rows[1]; c = rows[2]
    def pct(x, base):
        return (x - base) / base * 100 if base else 0.0
    print(f"\n  B vs A (mechanics check) : net {pct(b['net'], a['net']):+.1f}%  "
          f"| trades {b['trd']-a['trd']:+d}  → "
          f"{'AGREE ✓ engine mechanics reproduce the incumbent' if abs(pct(b['net'], a['net'])) < 8 else 'DIVERGE — investigate fills/exits'}")
    print(f"  C vs B (cash constraint) : net {pct(c['net'], b['net']):+.1f}%  "
          f"| {c['cashlim']} cash-limited entries  → "
          f"this is the implicit-leverage premium removed")
    print(f"  honest read (C)          : ${c['daily']:+.1f}/day at {c['ddm']:.1f}% MTM-DD "
          f"on a real ${settings.account_usd:,.0f} cash account")


_parity_failures: list[tuple] = []

for days in [180, 360]:
    print(f"\n{'='*104}\n  {days}-DAY WINDOW   (tickers={len(TICKERS)})\n{'='*104}")
    cache = prefetch_data(base(days))
    for cfg_name, ov in CONFIGS:
        cfg = replace(base(days), **ov)
        a = simulate_with_cache(cfg, cache)["metrics"]
        b = simulate_v2(cfg, cache, parity_mode=True)["metrics"]   # mechanism lens
        c = simulate_v2(cfg, cache, enforce_cash=True)["metrics"]  # strategy lens
        rows = [
            _row("A · Active",     a, days, is_active=True),
            _row("B · V2 parity",  b, days, is_active=False),
            _row("C · V2 cash-on", c, days, is_active=False),
        ]
        show(rows, days, cfg_name)

        # ── STANDING PARITY ASSERTION (the "mechanism validation") ──
        # parity_mode must reproduce the incumbent to the dollar AND to the
        # trade. Any drift means the two independent engines disagree → a real
        # fill/exit/accounting bug crept into one of them. Fail loudly.
        net_gap = abs(b.get("net_pnl_usd", 0) - a.get("net_pnl_usd", 0))
        trd_gap = abs(b.get("total_trades", 0) - a.get("total_trades", 0))
        ok = net_gap <= 0.01 and trd_gap == 0
        print(f"  PARITY {'PASS ✓' if ok else 'FAIL ✗'}: v2(parity_mode) vs incumbent"
              f"  → net Δ ${net_gap:.2f}, trades Δ {trd_gap}")
        if not ok:
            _parity_failures.append((days, cfg_name, net_gap, trd_gap))

print(f"\n{'='*104}")
if _parity_failures:
    print(f"❌ PARITY REGRESSION — {len(_parity_failures)} config(s) drifted from the incumbent:")
    for d, n, ng, tg in _parity_failures:
        print(f"     {d}d · {n}: net Δ ${ng:.2f}, trades Δ {tg}")
    print("   The unified engine no longer reproduces the oracle. Investigate "
          "fills/exits/accounting before trusting any v2 strategy number.")
    sys.exit(1)
print("✅ PARITY OK — v2(parity_mode) reproduces the incumbent to the dollar AND "
      "to the trade on every config × window.")
print("   Mechanism validated: the cash-on / pyramiding numbers sit on a trustworthy engine.")
print("\nDONE")
