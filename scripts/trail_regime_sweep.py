"""Sweep the two NEW techniques (2026-05-30) against the live .env baseline:

  1. Chandelier ATR trailing stop  (use_trailing_stop / trail_atr_mult /
     trail_activate_r) — lets fat-tail winners run past the MAX_HOLD guillotine.
  2. Regime-scaled sizing (use_regime_scaling / regime_bull_mult) — levers up in
     a confirmed strong bull + calm VIX.

Runs on BOTH 180-day and 360-day windows. Prefetches each window once, then
replays every config against the cache so the data fetch isn't repeated.
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


def base(days):
    """Mirror the live .env exactly so 'baseline' == the CLI backtest."""
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0, tickers=TICKERS,
        account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False,
    )


def run(cfg, name, cache):
    r = simulate_with_cache(cfg, cache)
    m = r["metrics"]
    reasons = m.get("exit_reasons", {})
    return {
        "name": name, "trades": m.get("total_trades", 0),
        "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
        "net": m.get("net_pnl_usd", 0), "daily": m.get("net_pnl_usd", 0) / cfg.days,
        "sortino": m.get("sortino_ratio", 0), "dd": m.get("max_drawdown_pct", 0),
        "trail": reasons.get("TRAIL", 0), "maxhold": reasons.get("MAX_HOLD", 0),
        "tp": reasons.get("TP", 0), "sl": reasons.get("SL", 0),
    }


def show(rows, base_net):
    rows.sort(key=lambda r: -r["net"])
    print(f"{'variant':<26} {'trd':>4} {'WR%':>5} {'PF':>5} {'NetPnL':>9} "
          f"{'$/day':>7} {'Sort':>6} {'DD%':>6} {'TR':>3} {'MH':>3} {'TP':>3} {'vs base':>8}")
    print("-" * 104)
    for r in rows:
        delta = r["net"] - base_net
        mark = "  <<<" if delta > 0 and r["dd"] < 20 else ""
        print(f"{r['name']:<26} {r['trades']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
              f"{r['net']:>+9.0f} {r['daily']:>+7.1f} {r['sortino']:>+6.1f} {r['dd']:>6.2f} "
              f"{r['trail']:>3} {r['maxhold']:>3} {r['tp']:>3} {delta:>+8.0f}{mark}")


for days in [180, 360]:
    print(f"\n{'='*104}\n  {days}-DAY WINDOW   (tickers={len(TICKERS)}, mpp={settings.max_position_pct}, "
          f"tp={settings.tp_atr_mult}, sl={settings.sl_atr_mult}, hold={settings.max_hold_days})\n{'='*104}")
    cache = prefetch_data(base(days))
    rows = []

    b0 = run(base(days), "BASELINE (.env)", cache)
    base_net = b0["net"]
    rows.append(b0)

    # Phase A — trailing within the current 7-day hold cap.
    for tm in [2.5, 3.0, 3.5]:
        rows.append(run(replace(base(days), use_trailing_stop=True, trail_atr_mult=tm),
                        f"trail{tm} hold7", cache))

    # Phase B — trailing + extended hold so the trail (not the calendar) exits.
    for mh in [12, 20, 30]:
        for tm in [2.5, 3.0, 3.5]:
            rows.append(run(replace(base(days), use_trailing_stop=True,
                                    trail_atr_mult=tm, max_hold_days=mh),
                            f"trail{tm} hold{mh}", cache))

    # Phase C — regime-scaled sizing alone.
    for bm in [1.25, 1.5, 1.75, 2.0]:
        rows.append(run(replace(base(days), use_regime_scaling=True, regime_bull_mult=bm),
                        f"regime x{bm}", cache))

    show(rows, base_net)
