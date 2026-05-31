"""pyramiding_compare — does LIVE-style stacking help a real $5k CASH account,
or does it just concentrate the book and starve new names of cash?

This is the strategy question behind the engine upgrade. simulate_v2 now models
the live open_position add-on: a held name that RE-FIRES a qualifying buy while
already ≥ STACK_MIN_R_MULTIPLE in unrealised profit (and with room under
MAX_STACKS_PER_SYMBOL) gets an add-on merged in (weighted-avg entry; stop/TP
raised, never lowered). MAX_POSITIONS does NOT gate a stack — but the cash wall
does, which is the crux on $5k: every dollar added to a winner is a dollar a
brand-new name can't use.

We run BOTH lenses on the SAME prefetched data, enforce_cash=True (the honest
deleveraged account), on both windows:

  cash baseline       — use_pyramiding=False (today's behaviour)
  cash + pyramiding    — use_pyramiding=True  (model the live stacking feature)

and report the cash-on $/day, MTM-DD, peak gross exposure (concentration), the
number of cash-limited entries (the starvation signal), and how many positions
actually stacked. The honest read is $/day at MTM-DD on the $5k cash account.

Run:  .venv/bin/python3 scripts/pyramiding_compare.py
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data
from src.backtest_v2 import simulate_v2
from src.config import settings

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

DD_CAP = settings.dd_halt_pct
LIVE10 = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]


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


def stack_stats(trades):
    """How many closed positions carried >1 entry, and the deepest stack."""
    stacked = [t for t in trades if int(t.get("stacks", 1)) > 1]
    max_stk = max((int(t.get("stacks", 1)) for t in trades), default=1)
    return len(stacked), max_stk


def row(label, days, tickers, cache, *, pyramiding):
    cfg = replace(base(days, tickers), use_pyramiding=pyramiding)
    res = simulate_v2(cfg, cache, enforce_cash=True)
    m = res["metrics"]
    n_stacked, max_stk = stack_stats(res["trades"])
    return {"label": label, "trd": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0), "daily": m.get("daily_pnl_usd", 0),
            "ddm": m.get("max_dd_mtm_pct", 0), "feq": m.get("final_equity_usd", 0),
            "gross": m.get("peak_gross_exposure_pct", 0),
            "cashlim": m.get("n_cash_limited_entries", 0),
            "n_stacked": n_stacked, "max_stk": max_stk}


def show(rows, base_daily):
    print(f"{'config':<22}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'pkGr%':>7}{'cashLim':>8}{'stacked':>8}{'maxStk':>7}"
          f"{'vs base':>9}")
    print("-" * 102)
    for r in rows:
        safe = r["ddm"] < DD_CAP
        d = r["daily"] - base_daily
        mark = ("  <<<" if (r["label"].endswith("pyramiding") and d > 0 and safe)
                else ("  !DD" if not safe else ""))
        print(f"{r['label']:<22}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}{r['gross']:>7.0f}"
              f"{r['cashlim']:>8}{r['n_stacked']:>8}{r['max_stk']:>7}{d:>+9.1f}{mark}")


for days in [180, 360]:
    print(f"\n{'='*102}\n  {days}-DAY · CASH ACCOUNT (enforce_cash=True, DD cap {DD_CAP:.0f}%, "
          f"seed ${settings.account_usd:,.0f}) — does stacking help?\n"
          f"  stack gates: ≥{settings.stack_min_r_multiple}R unrealised, "
          f"<{settings.max_stacks_per_symbol} stacks/name\n{'='*102}")
    cache = prefetch_data(base(days, LIVE10))
    rows = [
        row("cash baseline", days, LIVE10, cache, pyramiding=False),
        row("cash + pyramiding", days, LIVE10, cache, pyramiding=True),
    ]
    base_daily = rows[0]["daily"]
    show(rows, base_daily)

print("\nDONE")
