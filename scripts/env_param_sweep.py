"""env_param_sweep — tune ONLY the genuinely-live .env params under the honest
cash-on engine. No leverage, no backtest-only flags.

cash_frontier.py covered exit-management flags (scale-out/breakeven/trail) — most
of which are NOT wired into the live executor, so they can't be set in .env today.
This sweep instead moves only the parameters the live bot actually reads from .env
and applies on every trade:

    max_hold_days · tp_atr_mult · sl_atr_mult · risk_per_trade · max_position_pct

(threshold was already swept in cash_frontier: 65 hurts & blows DD, 75 starves —
so it's excluded here.)

The live .env is ALREADY heavily tuned (tp=8.0, sl=3.5, hold=7, rpt=0.05,
mpp=0.50), so the honest expectation is small/no headroom — this run is diligence
to confirm we're near the cash-account optimum, not a promise of a big win.

Every row is scored through simulate_v2(enforce_cash=True): a real $5k cash
balance + mark-to-market DD, so $/day is what a live cash account would live.
A variant only WINS if it beats baseline $/day on BOTH windows with MTM-DD under
the 18% halt on BOTH.

Run:  .venv/bin/python3 scripts/env_param_sweep.py
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


# Only LIVE-honoured params. Base values come from .env via settings:
#   tp_atr_mult=8.0  sl_atr_mult=3.5  max_hold_days=7  risk_per_trade=0.05  max_position_pct=0.50
VARIANTS = [
    ("baseline (.env)", {}),
    # — holding period —
    ("hold 5",          dict(max_hold_days=5)),
    ("hold 10",         dict(max_hold_days=10)),
    ("hold 14",         dict(max_hold_days=14)),
    ("hold 20",         dict(max_hold_days=20)),
    # — take-profit width (base 8.0) —
    ("tp 6",            dict(tp_atr_mult=6.0)),
    ("tp 10",           dict(tp_atr_mult=10.0)),
    ("tp 12",           dict(tp_atr_mult=12.0)),
    # — stop width (base 3.5; tighter SL → bigger size → hungrier for cash) —
    ("sl 2.5",          dict(sl_atr_mult=2.5)),
    ("sl 3.0",          dict(sl_atr_mult=3.0)),
    ("sl 4.0",          dict(sl_atr_mult=4.0)),
    ("sl 4.5",          dict(sl_atr_mult=4.5)),
    # — risk per trade (base 0.05) —
    ("rpt 0.04",        dict(risk_per_trade=0.04)),
    ("rpt 0.06",        dict(risk_per_trade=0.06)),
    ("rpt 0.07",        dict(risk_per_trade=0.07)),
    # — per-name cap (base 0.50; lower → more concurrent names fit in cash) —
    ("mpp 0.40",        dict(max_position_pct=0.40)),
    ("mpp 0.60",        dict(max_position_pct=0.60)),
    # — sensible combos —
    ("sl3 + hold10",    dict(sl_atr_mult=3.0, max_hold_days=10)),
    ("tp10 + hold14",   dict(tp_atr_mult=10.0, max_hold_days=14)),
    ("sl3 + tp10",      dict(sl_atr_mult=3.0, tp_atr_mult=10.0)),
    ("mpp0.40 + sl3",   dict(max_position_pct=0.40, sl_atr_mult=3.0)),
]


def run(cfg, name, cache):
    m = simulate_v2(cfg, cache, enforce_cash=True)["metrics"]
    return {"name": name, "trd": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0), "daily": m.get("daily_pnl_usd", 0),
            "ddm": m.get("max_dd_mtm_pct", 0), "feq": m.get("final_equity_usd", 0),
            "cashlim": m.get("n_cash_limited_entries", 0)}


def show(rows, base_daily):
    rows.sort(key=lambda r: -r["daily"])
    print(f"{'variant':<18}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'finalEq':>9}{'cashLim':>8}{'vs base':>9}")
    print("-" * 94)
    for r in rows:
        d = r["daily"] - base_daily
        safe = r["ddm"] < DD_CAP
        mark = "  <<<" if (r["daily"] > base_daily and safe) else ("  !DD" if not safe else "")
        print(f"{r['name']:<18}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}"
              f"{r['feq']:>9,.0f}{r['cashlim']:>8}{d:>+9.1f}{mark}")


store: dict[str, dict] = {}   # name -> {180: row, 360: row}
base_daily_by_win: dict[int, float] = {}

for days in [180, 360]:
    print(f"\n{'='*94}\n  {days}-DAY · CASH ACCOUNT (enforce_cash=True, DD cap={DD_CAP:.0f}%, "
          f"seed ${settings.account_usd:,.0f})\n{'='*94}")
    cache = prefetch_data(base(days))
    rows = [run(replace(base(days), **ov), name, cache) for name, ov in VARIANTS]
    base_daily = next(r for r in rows if r["name"] == "baseline (.env)")["daily"]
    base_daily_by_win[days] = base_daily
    for r in rows:
        store.setdefault(r["name"], {})[days] = r
    show(rows, base_daily)

# ── cross-window verdict: a real win must beat base on BOTH windows, safe on BOTH ──
print(f"\n{'='*94}\n  BOTH-WINDOW VERDICT (beats baseline $/day on 180d AND 360d, "
      f"MTM-DD<{DD_CAP:.0f}% on both)\n{'='*94}")
print(f"{'variant':<18}{'180 $/day':>11}{'180 DD%':>9}{'360 $/day':>11}{'360 DD%':>9}{'verdict':>12}")
print("-" * 70)
winners = []
for name, w in store.items():
    if name == "baseline (.env)":
        continue
    d180 = w[180]["daily"] - base_daily_by_win[180]
    d360 = w[360]["daily"] - base_daily_by_win[360]
    safe = w[180]["ddm"] < DD_CAP and w[360]["ddm"] < DD_CAP
    win = d180 > 0 and d360 > 0 and safe
    verdict = "WIN <<<" if win else ("!DD" if not safe else "")
    if win:
        winners.append(name)
    print(f"{name:<18}{w[180]['daily']:>+11.1f}{w[180]['ddm']:>9.2f}"
          f"{w[360]['daily']:>+11.1f}{w[360]['ddm']:>9.2f}{verdict:>12}")
print("-" * 70)
print(f"baseline (.env)   {base_daily_by_win[180]:>+11.1f}"
      f"{store['baseline (.env)'][180]['ddm']:>9.2f}"
      f"{base_daily_by_win[360]:>+11.1f}"
      f"{store['baseline (.env)'][360]['ddm']:>9.2f}")
print()
if winners:
    print(f"BOTH-WINDOW WINNERS: {', '.join(winners)}")
else:
    print("BOTH-WINDOW WINNERS: none — the live .env is already at/near the "
          "cash-account optimum for these single-knob moves.")

print("\nDONE")
