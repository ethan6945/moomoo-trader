"""fidelity_compare — what does closing the 3 reachable backtest↔live gaps cost?

The V3 engine now models three things it previously didn't, each behind a
default-OFF, parity-suppressed flag (engine_compare stays byte-exact):

  apply_vix_sizing          live risk_manager halves size when VIX>25, quarters >35
  apply_earnings_gate       block a NEW entry within N days before an earnings report
  use_realistic_commission  broker per-ORDER fees instead of the flat $1/trade

This sweeps them one at a time (and all-on) on the SAME prefetched data,
enforce_cash=True (the honest deleveraged $5k account), both windows, WITHOUT
scale-out so every trade is exactly one buy + one sell — the realistic commission
model charges per order, so partials would re-bill the buy fee and muddy the read.

Two commission rows bracket the broker reality:
  +comm MY   broker (MY) US-stocks: 0.03% × notional + $0.99 / order / SIDE
  +comm US   broker US-resident: $0 commission + $0 platform, only sell-side SEC/TAF

earnBlk = how many entries the earnings gate actually blocked. "vs base" = $/day
delta from the baseline (flat-$1, no new gates). The ALL row (with MY commission)
is the closest-to-live number for a Malaysian $5k cash account.

Run:  .venv/bin/python3 scripts/fidelity_compare.py
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

# (label, config overrides). base() pins use_scale_out=False for all rows so the
# per-order commission stays an exact 1-buy-1-sell charge.
CONFIGS = [
    ("baseline (flat $1)",   dict()),
    ("+VIX risk-off size",   dict(apply_vix_sizing=True)),
    ("+earnings gate",       dict(apply_earnings_gate=True)),
    ("+comm MY .03%+$.99",   dict(use_realistic_commission=True)),
    ("+comm US $0",          dict(use_realistic_commission=True,
                                  commission_pct_per_order=0.0,
                                  platform_fee_per_order=0.0)),
    ("ALL (MY comm)",        dict(apply_vix_sizing=True, apply_earnings_gate=True,
                                  use_realistic_commission=True)),
]


def base(days, tickers):
    return BacktestConfig(
        days=days, timeframe="HOUR_1", threshold=70.0, tickers=tickers,
        account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
        tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
        max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
        apply_mr_strategy=False, use_scale_out=False,
    )


def row(label, overrides, days, tickers, cache):
    cfg = replace(base(days, tickers), **overrides)
    res = simulate_v3(cfg, cache, enforce_cash=True)
    m = res["metrics"]
    return {"label": label, "trd": m.get("total_trades", 0),
            "wr": m.get("win_rate_pct", 0), "pf": m.get("profit_factor", 0),
            "net": m.get("net_pnl_usd", 0), "daily": m.get("daily_pnl_usd", 0),
            "ddm": m.get("max_dd_mtm_pct", 0),
            "gross": m.get("peak_gross_exposure_pct", 0),
            "cashlim": m.get("n_cash_limited_entries", 0),
            "earnblk": m.get("n_earnings_blocked", 0)}


def show(rows, base_daily):
    print(f"{'config':<22}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'pkGr%':>7}{'cashLim':>8}{'earnBlk':>8}{'vs base':>9}")
    print("-" * 95)
    for r in rows:
        safe = r["ddm"] < DD_CAP
        d = r["daily"] - base_daily
        mark = "  !DD" if not safe else ""
        print(f"{r['label']:<22}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}{r['gross']:>7.0f}"
              f"{r['cashlim']:>8}{r['earnblk']:>8}{d:>+9.1f}{mark}")


for days in [180, 360]:
    print(f"\n{'='*95}\n  {days}-DAY · CASH ACCOUNT (enforce_cash=True, DD cap {DD_CAP:.0f}%, "
          f"seed ${settings.account_usd:,.0f})\n"
          f"  closing the 3 reachable live gaps — VIX risk-off / earnings gate / real broker fees\n"
          f"  (no scale-out: every trade = 1 buy + 1 sell, so the per-order commission is exact)\n{'='*95}")
    cache = prefetch_data(base(days, LIVE10))
    rows = [row(lbl, ov, days, LIVE10, cache) for lbl, ov in CONFIGS]
    show(rows, rows[0]["daily"])

print("\nDONE")
