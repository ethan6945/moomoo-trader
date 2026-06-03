"""optuna_validate — confirm the honest-Optuna params actually beat the incumbent.

The study searched on 180d/3-fold OOS Sortino — NOT $/day — so its winner must be
validated on the real metric before any .env recommendation. This runs the
incumbent vs the proposed params head-to-head on honest 180d+360d (mpp=0.40),
with monthly + exit-bucket deltas so we can SEE whether higher selectivity +
tighter stop shrinks the stop-loss bleed without clipping the winners.

INCUMBENT : threshold=70, tp=8.0, sl=3.5,  gap=2.5   (current .env)
OPTUNA    : threshold=72, tp=8.0, sl=3.25, gap=2.5   (corrected ML-on/MR-off study best)

slip is HELD CONSTANT (study's 3.0 is a cost assumption, not a strategy knob;
both sides inherit the same default so the test isolates threshold/tp/sl/gap).

Run:  .venv/bin/python3 scripts/optuna_validate.py
"""
from __future__ import annotations
import json, logging, sys
from collections import defaultdict
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

TICKERS = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"][:10]
OLD10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
DD_CAP = settings.dd_halt_pct

OPTUNA = dict(threshold=72, tp_atr_mult=8.0, sl_atr_mult=3.25, max_gap_pct=2.5)


def base(days, mpp=0.40):
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=OLD10, account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade, max_position_pct=mpp,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
    )


def run(cfg, cache):
    out = _run_live_engine(cfg, cache, rich_metrics=False)
    m = out["metrics"]
    ex = defaultdict(lambda: [0, 0.0]); mo = defaultdict(float)
    for t in out["trades"]:
        b = ex[t["exit_reason"]]; b[0] += 1; b[1] += t["pnl"]
        mo[str(t["exit_date"])[:7]] += t["pnl"]
    return {"trd": m.get("total_trades", 0), "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0), "net": m.get("net_pnl_usd", 0),
            "daily": m.get("daily_pnl_usd", 0), "ddm": m.get("max_dd_mtm_pct", 0),
            "sl": ex["SL"], "ex": ex, "mo": mo}


for days in [180, 360]:
    print(f"\n{'='*76}\n  {days}-DAY · OPTUNA VALIDATION  10 names, mpp=0.40, honest (DD cap={DD_CAP:.0f}%)\n{'='*76}")
    cache = prefetch_data(base(days))
    inc = run(base(days), cache)
    opt = run(replace(base(days), **OPTUNA), cache)
    print(f"  {'config':<12}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}{'mtmDD%':>8}{'SLn':>5}{'SL$':>8}")
    print("  " + "-"*62)
    for lab, r in (("incumbent", inc), ("optuna", opt)):
        print(f"  {lab:<12}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}{r['net']:>+9.0f}"
              f"{r['daily']:>+7.1f}{r['ddm']:>8.2f}{r['sl'][0]:>5}{r['sl'][1]:>+8.0f}")
    d = opt["daily"] - inc["daily"]
    safe = opt["ddm"] < DD_CAP
    verdict = ("OPTUNA WINS" if (d > 0.05 and safe) else "OPTUNA !DD" if not safe
               else "~flat" if abs(d) <= 0.05 else "incumbent better")
    print(f"  → optuna vs incumbent:  ${d:+.1f}/day   DD {opt['ddm']:.1f}%   [{verdict}]")

    reds = sorted(m for m in set(inc["mo"]) | set(opt["mo"])
                  if min(inc["mo"].get(m, 0), opt["mo"].get(m, 0)) < 0)
    if reds:
        print(f"  red months:  {'month':<9}{'incum':>9}{'optuna':>9}{'Δ':>8}")
        for mo in reds:
            a, b = inc["mo"].get(mo, 0.0), opt["mo"].get(mo, 0.0)
            print(f"               {mo:<9}{a:>+9.0f}{b:>+9.0f}{b-a:>+8.0f}")

print("\nDONE")
