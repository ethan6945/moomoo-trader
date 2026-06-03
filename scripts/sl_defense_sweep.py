"""sl_defense_sweep — Phase 2b, the real lever: defend the stop-loss bucket.

Two diagnostics (regime_diagnose, leak_diagnose) refuted the regime and dead-money
hypotheses and proved the actual leak: EVERY losing month is 100% stop-loss exits.
TP is 100%-win, MAX_HOLD is ~78%-win and net-POSITIVE; the ONLY red bucket is SL
(360d: 37 trades, -$2,910). So the only honest Phase-2b defense is to attack the
SL bucket itself — convert would-be stop-outs into ~breakeven scratches.

The engine already supports this with no code change: a raised stop fires as
BREAKEVEN (parked at entry) or TRAIL (chandelier) instead of SL. Both are OFF in
production (zero such exits in the trial). This sweep flips those config knobs and
measures the honest net effect — the cost is that some TP winners get cut to
breakeven if they arm then pull back. Pure config, no leverage, no parity risk.

Decision rule: keep a row only if it cuts the SL bleed AND holds net $/day at
equal-or-lower MTM-DD on BOTH windows. Anything that bleeds the green months to
save the red ones is rejected.

Faithful to live: every cell runs _run_live_engine at the Phase-2a base
(mpp=0.40), same honest cash wall + VIX + earnings + MY commissions.

Run:  .venv/bin/python3 scripts/sl_defense_sweep.py
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

TICKERS = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]
DD_CAP = settings.dd_halt_pct


def base(days, mpp=0.40):
    return BacktestConfig(
        days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
        tickers=TICKERS, account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade, max_position_pct=mpp,
        max_hold_days=settings.max_hold_days, tp_atr_mult=settings.tp_atr_mult,
        sl_atr_mult=settings.sl_atr_mult, max_gap_pct=settings.max_gap_pct,
        dd_size_cut_pct=settings.dd_size_cut_pct, dd_halt_pct=settings.dd_halt_pct,
    )


# label → cfg overrides. First row = baseline (all anti-SL exits OFF).
EXITS = [
    ("baseline (off)",  {}),
    ("BE @ +1R",        {"use_breakeven_stop": True, "breakeven_trigger_r": 1.0}),
    ("BE @ +1.5R",      {"use_breakeven_stop": True, "breakeven_trigger_r": 1.5}),
    ("trail 3xATR@1R",  {"use_trailing_stop": True, "trail_atr_mult": 3.0, "trail_activate_r": 1.0}),
    ("trail 2xATR@1R",  {"use_trailing_stop": True, "trail_atr_mult": 2.0, "trail_activate_r": 1.0}),
    ("BE1R + trail3x",  {"use_breakeven_stop": True, "breakeven_trigger_r": 1.0,
                         "use_trailing_stop": True, "trail_atr_mult": 3.0, "trail_activate_r": 1.0}),
]


def run(cfg, cache):
    out = _run_live_engine(cfg, cache, rich_metrics=False)
    m = out["metrics"]
    # bucket the closed trades by exit reason to track where the SL trades went
    buck = defaultdict(lambda: [0, 0.0])  # reason -> [count, pnl]
    for t in out["trades"]:
        b = buck[t["exit_reason"]]; b[0] += 1; b[1] += t["pnl"]
    sl_n, sl_pnl = buck["SL"]
    save_n = buck["BREAKEVEN"][0] + buck["TRAIL"][0]
    save_pnl = buck["BREAKEVEN"][1] + buck["TRAIL"][1]
    return {"trd": m.get("total_trades", 0), "wr": m.get("win_rate_pct", 0),
            "pf": m.get("profit_factor", 0), "net": m.get("net_pnl_usd", 0),
            "daily": m.get("daily_pnl_usd", 0), "ddm": m.get("max_dd_mtm_pct", 0),
            "sl_n": sl_n, "sl_pnl": sl_pnl, "save_n": save_n, "save_pnl": save_pnl}


def show(rows, base_daily):
    print(f"{'exit config':<16}{'trd':>5}{'WR%':>6}{'PF':>6}{'NetPnL':>9}{'$/day':>7}"
          f"{'mtmDD%':>8}{'SLn':>5}{'SL$':>8}{'savedN':>7}{'saved$':>8}{'vs base':>9}")
    print("-" * 102)
    for r in rows:
        d = r["daily"] - base_daily
        safe = r["ddm"] < DD_CAP
        mark = "  <<<" if (r["daily"] > base_daily + 0.05 and safe) else ("  !DD" if not safe else "")
        print(f"{r['label']:<16}{r['trd']:>5}{r['wr']:>6.1f}{r['pf']:>6.2f}"
              f"{r['net']:>+9.0f}{r['daily']:>+7.1f}{r['ddm']:>8.2f}"
              f"{r['sl_n']:>5}{r['sl_pnl']:>+8.0f}{r['save_n']:>7}{r['save_pnl']:>+8.0f}{d:>+9.1f}{mark}")


for days in [180, 360]:
    print(f"\n{'='*102}\n  {days}-DAY · STOP-LOSS DEFENSE SWEEP  (mpp=0.40, honest production, "
          f"DD cap={DD_CAP:.0f}%)\n  every red month is 100% SL exits — does an anti-SL exit cut it "
          f"without bleeding winners?\n{'='*102}")
    cache = prefetch_data(base(days))
    rows = []
    for label, ov in EXITS:
        r = run(replace(base(days), **ov), cache)
        r["label"] = label
        rows.append(r)
    show(rows, rows[0]["daily"])

print("\nDONE")
