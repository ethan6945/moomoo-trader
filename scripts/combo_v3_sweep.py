"""Round-2 combo sweep (2026-05-30): the round-1 sweep showed

  • regime-scaled sizing is a big PnL lever in bull (x2.0 → +$8k on 180d)
    but it inflates max DD (12.5% → 16.3%);
  • a tight Chandelier trail (2.5-3.5×) HURTS PnL in a clean uptrend because
    it exits winners on normal pullbacks before the 8×ATR TP.

Hypotheses tested here:
  1. A WIDE trail (4-6×ATR) protects against violent reversals without shaking
     out of normal pullbacks — so it may add DD safety at little PnL cost.
  2. regime-up + wide-trail together = bull-market upside WITH a DD cushion.
  3. scale-out banks partials, which should pair well with regime-up.

Runs BOTH windows; prefetches each once.
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
    m = simulate_with_cache(cfg, cache)["metrics"]
    reasons = m.get("exit_reasons", {})
    return {"name": name, "trades": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0), "daily": m.get("net_pnl_usd", 0) / cfg.days,
            "sortino": m.get("sortino_ratio", 0), "dd": m.get("max_drawdown_pct", 0),
            "calmar": m.get("net_pnl_usd", 0) / max(m.get("max_drawdown_pct", 1e-9), 1e-9),
            "trail": reasons.get("TRAIL", 0), "mh": reasons.get("MAX_HOLD", 0),
            "tp": reasons.get("TP", 0)}


def show(rows, base_net):
    rows.sort(key=lambda r: -r["net"])
    print(f"{'variant':<26} {'trd':>4} {'WR%':>5} {'PF':>5} {'NetPnL':>9} "
          f"{'$/day':>7} {'Sort':>6} {'DD%':>6} {'Net/DD':>7} {'vs base':>8}")
    print("-" * 100)
    for r in rows:
        delta = r["net"] - base_net
        mark = "  <<<" if delta > 200 else ""
        print(f"{r['name']:<26} {r['trades']:>4} {r['wr']:>5.1f} {r['pf']:>5.2f} "
              f"{r['net']:>+9.0f} {r['daily']:>+7.1f} {r['sortino']:>+6.1f} {r['dd']:>6.2f} "
              f"{r['calmar']:>7.0f} {delta:>+8.0f}{mark}")


for days in [180, 360]:
    print(f"\n{'='*100}\n  {days}-DAY WINDOW\n{'='*100}")
    cache = prefetch_data(base(days))
    rows = []
    b0 = run(base(days), "BASELINE (.env)", cache); base_net = b0["net"]; rows.append(b0)

    # regime alone (controls)
    for bm in [1.5, 1.75, 2.0]:
        rows.append(run(replace(base(days), use_regime_scaling=True, regime_bull_mult=bm),
                        f"regime x{bm}", cache))
    # wide trail alone
    for tm in [4.0, 5.0, 6.0]:
        rows.append(run(replace(base(days), use_trailing_stop=True, trail_atr_mult=tm,
                                max_hold_days=20), f"trail{tm} hold20", cache))
    # regime + wide trail
    for bm in [1.5, 2.0]:
        for tm in [4.0, 5.0]:
            rows.append(run(replace(base(days), use_regime_scaling=True, regime_bull_mult=bm,
                                    use_trailing_stop=True, trail_atr_mult=tm, max_hold_days=20),
                            f"reg{bm}+trail{tm}", cache))
    # regime + scale-out (bank partials)
    for bm in [1.5, 2.0]:
        rows.append(run(replace(base(days), use_regime_scaling=True, regime_bull_mult=bm,
                                use_scale_out=True, tp1_r=3.0, tp2_r=6.0),
                        f"reg{bm}+scaleout", cache))

    show(rows, base_net)
