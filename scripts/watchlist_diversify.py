"""watchlist_diversify — does spreading the book beyond semis lower the
correlated-drawdown risk a real $5k cash account carries?

The live watchlist is 8 semis + 2 tech (DDOG, PANW). With MAX_POSITIONS=5 and
MAX_PER_SECTOR=5 the book can be FIVE fully-correlated semiconductor positions —
if the semi cycle rolls over, all five draw down together. cash_frontier proved
the .env is already near the cash-account optimum on $/day; this asks the OTHER
question: can we keep most of the $/day while cutting single-sector tail risk?

Each candidate is scored through simulate_v2(enforce_cash=True) on BOTH windows
(real $5k cash, mark-to-market DD). We also break PnL down by sector and report
the SEMIS SHARE of gross activity — the concentration we're trying to reduce.

NOTE: simulate_v2 trades every ticker in the prefetched cache, so each candidate
gets its OWN prefetch (network-heavy → candidates kept to a handful).

Run:  .venv/bin/python3 scripts/watchlist_diversify.py
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
from src.sector import get_sector

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b
_orig = _b.print
def print(*a, **k):
    k.setdefault("flush", True); _orig(*a, **k)

DD_CAP = settings.dd_halt_pct

# ── candidate watchlists ────────────────────────────────────────────────────
LIVE10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
TOP5_SEMI = ["SNDK", "WDC", "INTC", "MU", "LRCX"]      # best per-symbol PnL in notes
CROSS5 = ["LLY", "JPM", "XOM", "COST", "CAT"]          # health/finance/energy/cons/indust
CROSS7 = CROSS5 + ["NFLX", "UNH"]

CANDIDATES = {
    # name              tickers
    "live10 (semis)":   LIVE10,
    "spread10":         TOP5_SEMI + CROSS5,            # same slot budget, 6 sectors
    "live10 + cross5":  LIVE10 + CROSS5,               # keep semis, add optionality (15)
    "live10 + cross7":  LIVE10 + CROSS7,               # 17 names
}


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


def sector_breakdown(trades):
    """net PnL per sector + semis share of gross |PnL|."""
    by_sec: dict[str, float] = {}
    gross = 0.0
    semis_gross = 0.0
    for t in trades:
        sec = get_sector(t["symbol"])
        by_sec[sec] = by_sec.get(sec, 0.0) + t["pnl"]
        gross += abs(t["pnl"])
        if sec == "semis":
            semis_gross += abs(t["pnl"])
    semis_share = (semis_gross / gross * 100) if gross > 0 else 0.0
    return by_sec, semis_share


def run(days, name, tickers):
    cfg = base(days, tickers)
    cache = prefetch_data(cfg)
    got = sorted(cache["per_ticker"].keys())
    res = simulate_v2(cfg, cache, enforce_cash=True)
    m = res["metrics"]
    by_sec, semis_share = sector_breakdown(res["trades"])
    return {"name": name, "tickers": got, "n_tickers": len(got),
            "trd": m.get("total_trades", 0), "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0), "net": m.get("net_pnl_usd", 0),
            "daily": m.get("daily_pnl_usd", 0), "ddm": m.get("max_dd_mtm_pct", 0),
            "feq": m.get("final_equity_usd", 0), "by_sec": by_sec,
            "semis_share": semis_share}


def show(rows, base_daily, base_semis):
    print(f"{'candidate':<18}{'#tk':>4}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}"
          f"{'$/day':>7}{'mtmDD%':>8}{'semis%':>8}{'vs base':>9}")
    print("-" * 90)
    for r in rows:
        d = r["daily"] - base_daily
        safe = r["ddm"] < DD_CAP
        # interesting = keeps $/day within ~$2 of baseline, cuts semis concentration
        # by >=15pp, and stays under the DD halt.
        better_mix = (r["name"] != "live10 (semis)" and r["daily"] >= base_daily - 2
                      and (base_semis - r["semis_share"]) >= 15 and safe)
        mark = "  <<<" if better_mix else ("  !DD" if not safe else "")
        print(f"{r['name']:<18}{r['n_tickers']:>4}{r['trd']:>5}{r['wr']:>6.1f}"
              f"{r['pf']:>6.2f}{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}"
              f"{r['semis_share']:>7.0f}%{d:>+9.1f}{mark}")
    # sector PnL detail
    print("\n  per-sector NET PnL ($):")
    secs = sorted({s for r in rows for s in r["by_sec"]})
    print("  " + f"{'candidate':<18}" + "".join(f"{s[:7]:>9}" for s in secs))
    for r in rows:
        print("  " + f"{r['name']:<18}" + "".join(f"{r['by_sec'].get(s,0):>+9.0f}" for s in secs))


for days in [180, 360]:
    print(f"\n{'='*90}\n  {days}-DAY · CASH ACCOUNT (enforce_cash=True, DD cap={DD_CAP:.0f}%, "
          f"seed ${settings.account_usd:,.0f})\n{'='*90}")
    rows = []
    for name, tickers in CANDIDATES.items():
        rows.append(run(days, name, tickers))
    base_row = next(r for r in rows if r["name"] == "live10 (semis)")
    base_daily = base_row["daily"]; base_semis = base_row["semis_share"]
    rows.sort(key=lambda r: -r["daily"])
    show(rows, base_daily, base_semis)

print("\nDONE")
