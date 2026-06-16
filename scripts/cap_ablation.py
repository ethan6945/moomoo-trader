"""Single-name cap ablation (2026-06-12): MAX_POSITION_PCT 30/40/50/70% under
the v2.0 honest lens (dynamic top-15, realism flags, breakeven on). The 70%
owner setting was validated on the OLD engine + pinned universe; this re-bases
the concentration decision. One prefetch, four sims. Does not save results."""
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

base = make_cfg(140, 15)
cache = prefetch_data(base)
got = len(cache.get("per_ticker") or {})
if got < 58:   # 80% of the 72-name pool — refuse a degraded comparison
    sys.exit(f"prefetch degraded ({got}/72 tickers) — rerun when the network is stable")

for cap in (0.30, 0.40, 0.50, 0.70):
    m = _run_live_engine(replace(base, max_position_pct=cap), cache,
                         rich_metrics=True)["metrics"]
    dd = max(float(m.get("max_dd_mtm_pct") or 0), 1.0)
    print("CAP_AB " + json.dumps({
        "cap_pct": int(cap * 100),
        "trades": m.get("total_trades"),
        "win_rate_pct": m.get("win_rate_pct"),
        "daily_pnl_usd": m.get("daily_pnl_usd"),
        "max_dd_mtm_pct": m.get("max_dd_mtm_pct"),
        "profit_factor": m.get("profit_factor"),
        "calmar_like": round(float(m.get("daily_pnl_usd") or 0) / dd, 3),
        "cash_limited": m.get("n_cash_limited_entries"),
    }))
