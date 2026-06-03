"""universe_screen — Phase 2c: is the live 25-name universe better than the old 10?

DISCOVERY: watchlist.json auto-refreshed 10 → 25 names (watchlist_updater) AFTER
the Phase 2a/2b runs, which all used the first 10. So the cross-sector expansion
2c was going to propose ALREADY happened live. This screen validates the real
deployed universe instead of an invented candidate set:

  1. PORTFOLIO — does 25 beat 10 on honest $/day at equal-or-lower MTM-DD?
  2. PER-SYMBOL — rank all 25 by net contribution; flag the 15 added names so we
     can prune any dead weight (names that don't earn their slot under the cash
     wall + 5-slot cap).
  3. MONTHLY — do the cross-sector names decorrelate the all-semi red months
     (every Phase-2b red month was 100% stop-loss exits, clustered in semis)?

The 25 are SNAPSHOTTED as a constant below so a mid-run auto-refresh of the file
can't perturb the comparison. Honest production path (_run_live_engine) at the
Phase-2a base (mpp=0.40). Same cash wall + VIX + earnings + MY commissions.

CAVEAT (stated, not hidden): the 25 are today's momentum leaders backtested over
the past year — a survivorship tilt. Live uses a dynamically-refreshed watchlist,
so this static snapshot is mildly optimistic. The tilt is identical for the 10
subset, so the 10-vs-25 DELTA stays a fair read.

Run:  .venv/bin/python3 scripts/universe_screen.py
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

# Snapshot of config/watchlist.json @ 2026-06-01T12:59:24Z (frozen for a stable run)
NEW25 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP",
         "DELL", "SMCI", "HPE", "HPQ", "APP", "NOW", "FTNT", "ORCL", "AKAM", "WDAY",
         "HOOD", "IBM", "MGM", "UAL", "APH"]
OLD10 = NEW25[:10]
ADDED = set(NEW25[10:])          # the 15 names the updater appended
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
    cache = prefetch_data(cfg)
    out = _run_live_engine(cfg, cache, rich_metrics=False)
    m = out["metrics"]
    return {
        "trd": m.get("total_trades", 0), "wr": m.get("win_rate_pct", 0),
        "pf": m.get("profit_factor", 0), "net": m.get("net_pnl_usd", 0),
        "daily": m.get("daily_pnl_usd", 0), "ddm": m.get("max_dd_mtm_pct", 0),
        "cashlim": m.get("n_cash_limited_entries", 0), "trades": out["trades"],
    }


def by_symbol(trades):
    b = defaultdict(lambda: [0, 0.0])
    for t in trades:
        e = b[t["symbol"]]; e[0] += 1; e[1] += t["pnl"]
    return b


def by_month(trades):
    b = defaultdict(float)
    for t in trades:
        b[str(t["exit_date"])[:7]] += t["pnl"]
    return b


for days in [180, 360]:
    print(f"\n{'='*88}\n  {days}-DAY · UNIVERSE SCREEN  (mpp=0.40, honest production, DD cap={DD_CAP:.0f}%)\n{'='*88}")
    r10 = run(OLD10, days)
    r25 = run(NEW25, days)

    print(f"\n  (1) PORTFOLIO  10 vs 25")
    print(f"  {'universe':<10}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}{'mtmDD%':>8}{'cashLim':>8}")
    print("  " + "-"*54)
    for lab, r in (("old 10", r10), ("live 25", r25)):
        print(f"  {lab:<10}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}{r['net']:>+9.0f}"
              f"{r['daily']:>+7.1f}{r['ddm']:>8.2f}{r['cashlim']:>8}")
    d = r25["daily"] - r10["daily"]
    safe = r25["ddm"] < DD_CAP
    verdict = "25 WINS" if (d > 0.05 and safe) else ("25 !DD-BREACH" if not safe else "25 no better")
    print(f"  → live-25 vs old-10:  ${d:+.1f}/day   DD {r25['ddm']:.1f}% (cap {DD_CAP:.0f}%)   [{verdict}]")

    print(f"\n  (2) PER-SYMBOL CONTRIBUTION (live 25, sorted by net; * = added by updater)")
    print(f"  {'sym':<7}{'':<3}{'trd':>5}{'WR%':>7}{'net':>9}{'avg':>9}")
    print("  " + "-"*40)
    bs = by_symbol(r25["trades"])
    for sym in sorted(NEW25, key=lambda s: -bs[s][1]):
        n, net = bs[sym]
        tag = " *" if sym in ADDED else "  "
        if n == 0:
            print(f"  {sym:<7}{tag:<3}{0:>5}{'—':>7}{0:>+9.0f}{'—':>9}   (no trades — crowded out / no signal)")
        else:
            wr = sum(1 for t in r25['trades'] if t['symbol'] == sym and t['pnl'] > 0) / n * 100
            print(f"  {sym:<7}{tag:<3}{n:>5}{wr:>7.1f}{net:>+9.0f}{net/n:>+9.1f}")

    print(f"\n  (3) MONTHLY  10 vs 25  (does cross-sector decorrelate the red months?)")
    m10, m25 = by_month(r10["trades"]), by_month(r25["trades"])
    months = sorted(set(m10) | set(m25))
    print(f"  {'month':<9}{'old10':>9}{'live25':>9}{'delta':>9}")
    print("  " + "-"*36)
    for mo in months:
        a, b = m10.get(mo, 0.0), m25.get(mo, 0.0)
        flag = ""
        if a < 0 and b >= 0:
            flag = "  red→green"
        elif a < 0 and b < 0:
            flag = "  still red"
        print(f"  {mo:<9}{a:>+9.0f}{b:>+9.0f}{b-a:>+9.0f}{flag}")

print("\nDONE")
