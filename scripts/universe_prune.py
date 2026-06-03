"""universe_prune — Phase 2c follow-up: prune the auto-expanded 25 back to the
names that actually earn their slot on the honest engine.

universe_screen proved the live 25-name watchlist is WORSE than the old 10 on
both windows (cash-wall quality dilution: PF 3.53→1.87, cashLim 42→148). But the
per-symbol table split the 15 added names cleanly:

  KEEPERS (net-positive in BOTH 180d & 360d, semis-adjacent → fit the momentum
  signal's wheelhouse): DELL, SMCI, HPE, HPQ, FTNT
  DEAD WEIGHT (out-of-wheelhouse, bleeding): MGM (gaming), UAL (airline),
  IBM (legacy), APP (adtech), AKAM, ORCL, WDAY, plus marginal NOW/HOOD/APH.

The prune has a STRUCTURAL rationale (sector fit), not just in-sample luck. This
script tests whether the curated 15 beats the clean 10 — i.e. is *selective*
expansion worth it, or is the binding constraint simply cash (in which case the
honest answer is: pin the watchlist to the 10 and fix the updater).

Honest production path (_run_live_engine) at the Phase-2a base (mpp=0.40).

Run:  .venv/bin/python3 scripts/universe_prune.py
"""
from __future__ import annotations
import json, logging, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, _run_live_engine
from src.config import settings

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

OLD10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
KEEPERS = ["DELL", "SMCI", "HPE", "HPQ", "FTNT"]   # net-positive both windows, sector-fit
PRUNED15 = OLD10 + KEEPERS
DD_CAP = settings.dd_halt_pct


def base(days, tickers, mpp=0.40):
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=tickers, account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade, max_position_pct=mpp,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
    )


def run(tickers, days):
    cfg = base(days, tickers)
    out = _run_live_engine(cfg, prefetch_data(cfg), rich_metrics=False)
    m = out["metrics"]
    return {"trd": m.get("total_trades", 0), "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0), "net": m.get("net_pnl_usd", 0),
            "daily": m.get("daily_pnl_usd", 0), "ddm": m.get("max_dd_mtm_pct", 0),
            "cashlim": m.get("n_cash_limited_entries", 0), "trades": out["trades"]}


def by_month(trades):
    b = defaultdict(float)
    for t in trades:
        b[str(t["exit_date"])[:7]] += t["pnl"]
    return b


for days in [180, 360]:
    print(f"\n{'='*78}\n  {days}-DAY · PRUNE TEST  10 vs 15-curated  (mpp=0.40, honest, DD cap={DD_CAP:.0f}%)\n{'='*78}")
    r10 = run(OLD10, days)
    r15 = run(PRUNED15, days)
    print(f"  {'universe':<12}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}{'mtmDD%':>8}{'cashLim':>8}")
    print("  " + "-"*56)
    for lab, r in (("old 10", r10), ("curated 15", r15)):
        print(f"  {lab:<12}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}{r['net']:>+9.0f}"
              f"{r['daily']:>+7.1f}{r['ddm']:>8.2f}{r['cashlim']:>8}")
    d = r15["daily"] - r10["daily"]
    safe = r15["ddm"] < DD_CAP
    verdict = ("15 WINS" if (d > 0.05 and safe) else
               "15 !DD" if not safe else
               "15 ~= 10 (flat)" if abs(d) <= 0.05 else "15 worse — pin to 10")
    print(f"  → curated-15 vs old-10:  ${d:+.1f}/day   DD {r15['ddm']:.1f}%   [{verdict}]")

    # red-month check: does selective expansion shrink the red months?
    m10, m15 = by_month(r10["trades"]), by_month(r15["trades"])
    reds = sorted(m for m in set(m10) | set(m15) if min(m10.get(m, 0), m15.get(m, 0)) < 0)
    if reds:
        print(f"  red-month compare:  {'month':<9}{'old10':>9}{'cur15':>9}{'Δ':>8}")
        for mo in reds:
            a, b = m10.get(mo, 0.0), m15.get(mo, 0.0)
            print(f"                      {mo:<9}{a:>+9.0f}{b:>+9.0f}{b-a:>+8.0f}")

print("\nDONE")
