"""push100 — find the best $/day on the live watchlist with DRAWDOWN BOUNDED.

Context (2026-05-30): baseline (.env) does ~$54.6/day on 180d, ~$15.7/day on
360d. The user wants ~$100/day. The only real $/day levers in this engine are
sizing (regime_bull_mult, risk_per_trade — both applied as leverage on top of
the mpp cap) and trade count (threshold). Leverage buys $/day at the cost of
max-DD, so every variant is reported WITH its DD and a Net/DD efficiency number.

Goal of the table: locate the highest $/day that still keeps max-DD under the
user's own circuit-breaker (dd_halt_pct = 18%), on BOTH windows — not just the
single biggest backtest number.
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data, simulate_with_cache
from src.config import settings

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

TICKERS = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]
DD_CAP = settings.dd_halt_pct   # 18.0 — the user's live no-new-entries threshold


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


# (name, overrides) — every override is a kwarg to dataclasses.replace(base).
VARIANTS = [
    ("BASELINE (.env)", {}),
    # — regime leverage intensity —
    ("reg1.5", dict(use_regime_scaling=True, regime_bull_mult=1.5)),
    ("reg2.0", dict(use_regime_scaling=True, regime_bull_mult=2.0)),
    ("reg2.5", dict(use_regime_scaling=True, regime_bull_mult=2.5)),
    # — regime + scale-out (bank partials → tame DD) —
    ("reg2.0+so3/6", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                          use_scale_out=True, tp1_r=3.0, tp2_r=6.0)),
    ("reg2.5+so3/6", dict(use_regime_scaling=True, regime_bull_mult=2.5,
                          use_scale_out=True, tp1_r=3.0, tp2_r=6.0)),
    ("reg2.0+so2/5", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                          use_scale_out=True, tp1_r=2.0, tp2_r=5.0)),
    # — regime + breakeven (cut losers to 0R after +R, keep upside) —
    ("reg2.0+be1.5", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                          use_breakeven_stop=True, breakeven_trigger_r=1.5)),
    ("reg2.0+so3/6+be2", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                              use_scale_out=True, tp1_r=3.0, tp2_r=6.0,
                              use_breakeven_stop=True, breakeven_trigger_r=2.0)),
    # — regime + higher base risk —
    ("reg2.0 rpt.06", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                           risk_per_trade=0.06)),
    ("reg2.0 rpt.07", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                           risk_per_trade=0.07)),
    ("reg2.0 rpt.06+so3/6", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                                 risk_per_trade=0.06, use_scale_out=True,
                                 tp1_r=3.0, tp2_r=6.0)),
    # — regime + more trades —
    ("reg2.0 thr65", dict(use_regime_scaling=True, regime_bull_mult=2.0, threshold=65.0)),
    # — regime + let winners run —
    ("reg2.0 tp10 hold10", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                                tp_atr_mult=10.0, max_hold_days=10)),
    # — aggressive but DD-managed —
    ("reg2.5 rpt.06+so3/6", dict(use_regime_scaling=True, regime_bull_mult=2.5,
                                 risk_per_trade=0.06, use_scale_out=True,
                                 tp1_r=3.0, tp2_r=6.0)),
    ("reg2.0 mpp.65", dict(use_regime_scaling=True, regime_bull_mult=2.0,
                           max_position_pct=0.65)),
]


def run(cfg, name, cache):
    m = simulate_with_cache(cfg, cache)["metrics"]
    net = m.get("net_pnl_usd", 0)
    dd = m.get("max_drawdown_pct", 0)
    return {"name": name, "trades": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": net, "daily": net / cfg.days, "sortino": m.get("sortino_ratio", 0),
            "dd": dd, "netdd": net / max(dd, 1e-9)}


def show(rows, base_net, base_daily):
    rows.sort(key=lambda r: -r["daily"])
    print(f"{'variant':<22} {'trd':>4} {'WR%':>5} {'PF':>5} {'NetPnL':>9} "
          f"{'$/day':>7} {'Sort':>6} {'DD%':>6} {'Net/DD':>7} {'vs base/day':>11}")
    print("-" * 100)
    for r in rows:
        d = r["daily"] - base_daily
        # flag: beats baseline $/day AND keeps DD under the user's halt threshold.
        mark = "  <<<" if (r["daily"] > base_daily and r["dd"] < DD_CAP) else ""
        hit = " 🎯" if r["daily"] >= 100 and r["dd"] < DD_CAP else ""
        print(f"{r['name']:<22} {r['trades']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
              f"{r['net']:>+9.0f} {r['daily']:>+7.1f} {r['sortino']:>+6.1f} {r['dd']:>6.2f} "
              f"{r['netdd']:>7.0f} {d:>+11.1f}{mark}{hit}")


for days in [180, 360]:
    print(f"\n{'='*100}\n  {days}-DAY WINDOW   (DD cap = {DD_CAP:.0f}%, tickers={len(TICKERS)})\n{'='*100}")
    cache = prefetch_data(base(days))
    rows = []
    for name, ov in VARIANTS:
        rows.append(run(replace(base(days), **ov), name, cache))
    base_row = next(r for r in rows if r["name"] == "BASELINE (.env)")
    show(rows, base_row["net"], base_row["daily"])

print("\nDONE")
