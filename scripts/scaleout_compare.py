"""scaleout_compare — is the *partial-profit-taking itself* worth it, or is the
only thing scale-out buys us "don't trail the runner"?

The trap: at live params the runner take-profit sits at
    TP_ATR_MULT × ATR = 8.0 × ATR  →  in R-units = 8.0 / SL_ATR_MULT(3.5) = 2.29R.
The live tranches TP1_R=3.0 / TP2_R=6.0 are ABOVE that (10.5 / 21 ATR), so on a
normal gradual move the whole position exits at the +8 ATR runner FIRST and the
partials almost never fire. So "use_scale_out" today is mostly a no-op except on
single-bar spikes / gaps.

This pins it down by sweeping the tranche heights on the SAME prefetched data,
enforce_cash=True (the honest deleveraged $5k account), both windows:

  no scale-out          whole position rides to +8 ATR runner / SL / max-hold
  live 3R/6R (ABOVE)    today's setting — tranches above runner, rarely fire
  low  1R/2R (BELOW)    tranches BELOW runner → 1/3 out at +3.5 ATR, 1/3 at +7
  xlow .75R/1.5R        partials even earlier

tp1Fire / tp2Fire = how many times each partial ACTUALLY triggered (exit_reason
TP1 / TP2). If "low/xlow" (where 分批 truly bites) can't beat "no scale-out" on
$/day at MTM-DD, then partial profit-taking itself isn't adding edge here.

Run:  .venv/bin/python3 scripts/scaleout_compare.py
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

DD_CAP = settings.dd_halt_pct
LIVE10 = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]
RUNNER_R = settings.tp_atr_mult / settings.sl_atr_mult   # +8 ATR expressed in R

# (label, scale-out overrides)
CONFIGS = [
    ("no scale-out",       dict(use_scale_out=False)),
    ("live 3R/6R (above)", dict(use_scale_out=True, tp1_r=3.0,  tp2_r=6.0)),
    ("low 1R/2R (below)",  dict(use_scale_out=True, tp1_r=1.0,  tp2_r=2.0)),
    ("xlow .75R/1.5R",     dict(use_scale_out=True, tp1_r=0.75, tp2_r=1.5)),
]


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


def row(label, overrides, days, tickers, cache):
    cfg = replace(base(days, tickers), **overrides)
    res = simulate_v3(cfg, cache, enforce_cash=True)
    m = res["metrics"]
    reasons: dict[str, int] = {}
    for t in res["trades"]:
        r = t.get("exit_reason")
        reasons[r] = reasons.get(r, 0) + 1
    return {"label": label, "trd": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0), "daily": m.get("daily_pnl_usd", 0),
            "ddm": m.get("max_dd_mtm_pct", 0),
            "gross": m.get("peak_gross_exposure_pct", 0),
            "cashlim": m.get("n_cash_limited_entries", 0),
            "tp1": reasons.get("TP1", 0), "tp2": reasons.get("TP2", 0)}


def show(rows, base_daily):
    print(f"{'config':<22}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'pkGr%':>7}{'cashLim':>8}{'tp1Fire':>8}{'tp2Fire':>8}"
          f"{'vs base':>9}")
    print("-" * 105)
    for r in rows:
        safe = r["ddm"] < DD_CAP
        d = r["daily"] - base_daily
        mark = ("  <<<" if (r["label"] != "no scale-out" and d > 0 and safe)
                else ("  !DD" if not safe else ""))
        print(f"{r['label']:<22}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}{r['gross']:>7.0f}"
              f"{r['cashlim']:>8}{r['tp1']:>8}{r['tp2']:>8}{d:>+9.1f}{mark}")


for days in [180, 360]:
    print(f"\n{'='*105}\n  {days}-DAY · CASH ACCOUNT (enforce_cash=True, DD cap {DD_CAP:.0f}%, "
          f"seed ${settings.account_usd:,.0f}) — is 分批 itself worth it?\n"
          f"  runner TP = {settings.tp_atr_mult:.0f} ATR = {RUNNER_R:.2f}R  →  "
          f"tranches BELOW {RUNNER_R:.2f}R actually fire on normal moves "
          f"(live 3R/6R sit ABOVE it)\n{'='*105}")
    cache = prefetch_data(base(days, LIVE10))
    rows = [row(lbl, ov, days, LIVE10, cache) for lbl, ov in CONFIGS]
    show(rows, rows[0]["daily"])

print("\nDONE")
