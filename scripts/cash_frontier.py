"""cash_frontier — what is the BEST $/day a real $5,000 *cash* account can do?

engine_compare.py proved the $100/day combo was implicit leverage (it needs
~5× margin; strip it with simulate_v3(enforce_cash=True) and $105/day → $43/day,
worse than baseline). So this sweep drops sizing-leverage entirely and asks the
honest question: among exit-management tweaks that DON'T borrow money
(scale-out, breakeven, trailing, threshold), which lifts the cash-on $/day on
BOTH windows while keeping mark-to-market DD under the 18% halt?

Everything is scored through the NEW engine (backtest_v3, enforce_cash=True) so
the numbers are what a cash account would actually live. A couple of mild
regime-leverage rows are included ONLY as a contrast — to show even 1.25-1.5×
doesn't help once you can't fund it.

Run:  .venv/bin/python3 scripts/cash_frontier.py
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data
from src.backtest_v3 import simulate_v3
from src.config import settings

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

TICKERS = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]
DD_CAP = settings.dd_halt_pct   # 18%


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


# Non-leverage exit/threshold tweaks (the honest levers) + mild-leverage contrasts.
VARIANTS = [
    ("baseline (.env)", {}),
    # — bank partials & recycle cash (should HELP a cash account) —
    ("scaleout 3/6", dict(use_scale_out=True, tp1_r=3.0, tp2_r=6.0)),
    ("scaleout 2/5", dict(use_scale_out=True, tp1_r=2.0, tp2_r=5.0)),
    ("scaleout 2/4", dict(use_scale_out=True, tp1_r=2.0, tp2_r=4.0)),
    # — breakeven: free capital from losers faster —
    ("breakeven 1.0", dict(use_breakeven_stop=True, breakeven_trigger_r=1.0)),
    ("breakeven 1.5", dict(use_breakeven_stop=True, breakeven_trigger_r=1.5)),
    ("so3/6 + be1.5", dict(use_scale_out=True, tp1_r=3.0, tp2_r=6.0,
                           use_breakeven_stop=True, breakeven_trigger_r=1.5)),
    # — let winners run (wide trail) —
    ("trail5 hold20", dict(use_trailing_stop=True, trail_atr_mult=5.0, max_hold_days=20)),
    ("so3/6 + trail5 h20", dict(use_scale_out=True, tp1_r=3.0, tp2_r=6.0,
                                use_trailing_stop=True, trail_atr_mult=5.0, max_hold_days=20)),
    # — trade-count levers —
    ("threshold 65", dict(threshold=65.0)),
    ("threshold 75", dict(threshold=75.0)),
    ("so3/6 + thr65", dict(use_scale_out=True, tp1_r=3.0, tp2_r=6.0, threshold=65.0)),
    # — mild leverage CONTRAST (expected: no real help on cash) —
    ("[lev] reg1.25", dict(use_regime_scaling=True, regime_bull_mult=1.25)),
    ("[lev] reg1.5+so3/6", dict(use_regime_scaling=True, regime_bull_mult=1.5,
                                use_scale_out=True, tp1_r=3.0, tp2_r=6.0)),
]


def run(cfg, name, cache):
    m = simulate_v3(cfg, cache, enforce_cash=True)["metrics"]
    return {"name": name, "trd": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0), "daily": m.get("daily_pnl_usd", 0),
            "ddm": m.get("max_dd_mtm_pct", 0), "feq": m.get("final_equity_usd", 0),
            "cashlim": m.get("n_cash_limited_entries", 0)}


def show(rows, base_daily):
    rows.sort(key=lambda r: -r["daily"])
    print(f"{'variant':<20}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'finalEq':>9}{'cashLim':>8}{'vs base':>9}")
    print("-" * 96)
    for r in rows:
        d = r["daily"] - base_daily
        safe = r["ddm"] < DD_CAP
        mark = "  <<<" if (r["daily"] > base_daily and safe) else ("  !DD" if not safe else "")
        print(f"{r['name']:<20}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}"
              f"{r['feq']:>9,.0f}{r['cashlim']:>8}{d:>+9.1f}{mark}")


for days in [180, 360]:
    print(f"\n{'='*96}\n  {days}-DAY · CASH ACCOUNT (enforce_cash=True, DD cap={DD_CAP:.0f}%, "
          f"seed ${settings.account_usd:,.0f})\n{'='*96}")
    cache = prefetch_data(base(days))
    rows = [run(replace(base(days), **ov), name, cache) for name, ov in VARIANTS]
    base_daily = next(r for r in rows if r["name"] == "baseline (.env)")["daily"]
    show(rows, base_daily)

print("\nDONE")
