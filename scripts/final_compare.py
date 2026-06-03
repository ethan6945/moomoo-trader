"""final_compare — the capstone before/after for the no-leverage upgrade.

Shows, side by side on BOTH windows, what a real $5k CASH account would live
(simulate_v3 enforce_cash=True) against the incumbent's optimistic number
(simulate_time_stepped = what the GUI reports), for the recommended changes:

  baseline          — live .env exactly as it is today (10 semis, single TP_HALF)
  + scale-out        — USE_SCALE_OUT 3/6 (the new live feature; cash-frontier best
                       no-leverage exit lever)
  + diversified      — best cross-sector watchlist from watchlist_diversify.py
  + both             — diversified watchlist AND scale-out

The "honest read" line is the cash-on $/day at MTM-DD — the number to trust.

Run:  .venv/bin/python3 scripts/final_compare.py
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache
from src.backtest_v3 import simulate_v3
from src.config import settings

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

DD_CAP = settings.dd_halt_pct
LIVE10 = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]

# ── best cross-sector mix from watchlist_diversify.py (2026-05-31 result) ──────
# The diversify sweep's verdict: diversification does NOT help a $5k cash account.
# Of the candidates, live10+cross5 was the best diversified option (180d +42.9 vs
# semis-only +55.4; 360d +9.2 vs +11.2) — but it still LOSES $/day on both windows
# AND did not lower MTM-DD (semis is the breadwinner; finance/consumer names lose).
# We keep it here only to SHOW that cost side-by-side; the recommendation is to
# keep the live semis watchlist.
DIVERSIFIED = LIVE10 + ["LLY", "JPM", "XOM", "COST", "CAT"]

SCALE_OUT = dict(use_scale_out=True, tp1_r=3.0, tp2_r=6.0)


def base(days, tickers):
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0, tickers=tickers,
        account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False,
    )


def row(label, days, tickers, ov, cache, *, active=False):
    cfg = replace(base(days, tickers), **ov)
    if active:
        m = simulate_with_cache(cfg, cache)["metrics"]
        net = m.get("net_pnl_usd", 0)
        return {"label": label, "trd": m.get("total_trades", 0),
                "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
                "net": net, "daily": net / days, "ddm": None,
                "feq": settings.account_usd + net}
    m = simulate_v3(cfg, cache, enforce_cash=True)["metrics"]
    return {"label": label, "trd": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0), "daily": m.get("daily_pnl_usd", 0),
            "ddm": m.get("max_dd_mtm_pct", 0), "feq": m.get("final_equity_usd", 0)}


def show(rows, base_daily):
    print(f"{'config':<26}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'finalEq':>9}{'vs base':>9}")
    print("-" * 96)
    for r in rows:
        ddm = f"{r['ddm']:.2f}" if r["ddm"] is not None else "  —"
        d = "" if r["ddm"] is None else f"{r['daily']-base_daily:>+9.1f}"
        safe = (r["ddm"] is None) or (r["ddm"] < DD_CAP)
        mark = "" if r["ddm"] is None else ("  <<<" if (r["daily"] > base_daily and safe)
                                            else ("  !DD" if not safe else ""))
        print(f"{r['label']:<26}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{ddm:>8}{r['feq']:>9,.0f}{d:>9}{mark}")


for days in [180, 360]:
    print(f"\n{'='*96}\n  {days}-DAY · before/after (cash-on $/day is the honest number; "
          f"DD cap {DD_CAP:.0f}%)\n{'='*96}")
    cache10 = prefetch_data(base(days, LIVE10))
    cacheD = prefetch_data(base(days, DIVERSIFIED))
    rows = [
        row("Active baseline (GUI)", days, LIVE10, {}, cache10, active=True),
        row("cash baseline (.env)", days, LIVE10, {}, cache10),
        row("cash + scale-out 3/6", days, LIVE10, SCALE_OUT, cache10),
        row("cash + diversified", days, DIVERSIFIED, {}, cacheD),
        row("cash + diversified + SO", days, DIVERSIFIED, SCALE_OUT, cacheD),
    ]
    base_daily = next(r for r in rows if r["label"] == "cash baseline (.env)")["daily"]
    show(rows, base_daily)

print("\nDONE")
