"""Conviction-gated margin experiment (2026-06-12): allow HIGH-SCORE entries
only to borrow past the cash wall. Variants: score gate 75/80, leverage 1.5x/2x,
vs the no-leverage baseline. Margin interest charged post-hoc at 8%/yr on the
measured borrowed dollar-days. v2.0 lens (dynamic top-15, cap 0.40, breakeven).
NOTE: live execution would require a MARGIN account — this is feasibility only."""
from __future__ import annotations
import json, logging, sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
from src.backtest import prefetch_data, _run_live_engine  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
from phase1_universe_validation import make_cfg  # noqa: E402

MARGIN_RATE = 0.08

base = make_cfg(140, 15)
cache = prefetch_data(base)
if len(cache.get("per_ticker") or {}) < 58:
    sys.exit("prefetch degraded — rerun later")

variants = [
    ("baseline_no_lev", 0.0, 1.0),
    ("score75_lev1.5", 75.0, 1.5),
    ("score80_lev1.5", 80.0, 1.5),
    ("score80_lev2.0", 80.0, 2.0),
]
for tag, score, mult in variants:
    cfg = replace(base, conviction_lev_score=score, conviction_lev_mult=mult)
    m = _run_live_engine(cfg, cache, rich_metrics=True)["metrics"]
    interest = round(m.get("borrowed_dollar_days", 0.0) * MARGIN_RATE / 252, 2)
    print("LEV_AB " + json.dumps({
        "tag": tag,
        "trades": m.get("total_trades"),
        "daily_pnl_usd": m.get("daily_pnl_usd"),
        "net_pnl_usd": m.get("net_pnl_usd"),
        "max_dd_mtm_pct": m.get("max_dd_mtm_pct"),
        "profit_factor": m.get("profit_factor"),
        "n_leveraged_entries": m.get("n_leveraged_entries", 0),
        "max_borrowed_usd": m.get("max_borrowed_usd", 0),
        "est_margin_interest_usd": interest,
        "daily_pnl_after_interest": round(
            (float(m.get("net_pnl_usd") or 0) - interest) / 140, 2),
    }))
