"""cash_starvation_sweep — the sizing/slot frontier cash_frontier.py never swept.

The honest v3 production path showed 90 cash-limited entries on 360d. Root cause:
MAX_POSITION_PCT=0.50 with a 5-slot cap means just TWO positions consume 100% of
the $5,000 cash, so slots 3-5 starve and good signals get dropped/clipped. This
sweep traces concentrated → diversified sizing to see how much forgone PnL we
recover WITHOUT adding leverage (enforce_cash stays True the whole time).

Faithful to live: every cell runs through `_run_live_engine` — the SAME honest
config production uses (real cash wall + VIX risk-off + earnings gate + MY
commissions). rich_metrics=False only skips the Monte-Carlo cost; headline
numbers are identical. The baseline row (0.50 × 5 slots) must reproduce the
production trial ($46.6/day 180d, $21.0/day 360d) — a built-in harness check.

The slot cap is read from the GLOBAL `settings.max_positions` (both engines read
_settings, not cfg), so it's monkeypatched per cell and restored after.

Run:  .venv/bin/python3 scripts/cash_starvation_sweep.py
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
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

TICKERS = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]
DD_CAP = settings.dd_halt_pct   # 18%


def base(days):
    """Match the production CLI cfg exactly (src/backtest.py main())."""
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=TICKERS, account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
    )


# (max_position_pct, max_positions). Traces concentrated → diversified.
# Note: cash binds at mpp × slots ≈ 100%, so raising slots only frees capacity
# once mpp is small enough that >5 positions can actually be funded.
GRID = [
    (0.50, 5),   # ← baseline = current .env
    (0.40, 5),
    (0.33, 5),
    (0.25, 5),
    (0.20, 5),
    (0.20, 8),
    (0.15, 8),
    (0.125, 8),
]


def run(cfg, cache):
    m = _run_live_engine(cfg, cache, rich_metrics=False)["metrics"]
    return {"trd": m.get("total_trades", 0), "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0), "net": m.get("net_pnl_usd", 0),
            "daily": m.get("daily_pnl_usd", 0), "ddm": m.get("max_dd_mtm_pct", 0),
            "feq": m.get("final_equity_usd", 0),
            "cashlim": m.get("n_cash_limited_entries", 0),
            "earnblk": m.get("n_earnings_blocked", 0)}


def show(rows, base_daily):
    print(f"{'mpp × slots':<14}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'finalEq':>9}{'cashLim':>8}{'earnBlk':>8}{'vs base':>9}")
    print("-" * 98)
    for r in rows:
        d = r["daily"] - base_daily
        safe = r["ddm"] < DD_CAP
        mark = "  <<<" if (r["daily"] > base_daily + 0.05 and safe) else ("  !DD" if not safe else "")
        print(f"{r['label']:<14}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}"
              f"{r['feq']:>9,.0f}{r['cashlim']:>8}{r['earnblk']:>8}{d:>+9.1f}{mark}")


_saved_slots = settings.max_positions
try:
    for days in [180, 360]:
        print(f"\n{'='*98}\n  {days}-DAY · CASH-STARVATION SWEEP (enforce_cash=True, "
              f"DD cap={DD_CAP:.0f}%, seed ${settings.account_usd:,.0f}, {len(TICKERS)} tickers)\n"
              f"  sizing dimension only — all exit/fidelity knobs = live production\n{'='*98}")
        cache = prefetch_data(base(days))
        rows = []
        for mpp, slots in GRID:
            object.__setattr__(settings, "max_positions", slots)  # global slot cap (frozen dc → bypass)
            cfg = replace(base(days), max_position_pct=mpp)
            r = run(cfg, cache)
            r["label"] = f"{mpp:.3g} × {slots}"
            rows.append(r)
        base_daily = rows[0]["daily"]               # first row = 0.50 × 5 = baseline
        show(rows, base_daily)
finally:
    object.__setattr__(settings, "max_positions", _saved_slots)  # restore — never leak the patch

print("\nDONE")
