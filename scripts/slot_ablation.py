"""Slot-target A/B (2026-06-12): SLOT_TARGET_USD 1000 (→5 slots @$5k) vs 500
(→10 slots @$5k) under the v2.0 lens (dynamic top-15, cap 0.40, breakeven on).
Hypothesis: zero effect — the cash wall binds at ~3 positions long before the
slot cap. One prefetch, two sims."""
from __future__ import annotations
import json, logging, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
from src.backtest import prefetch_data, _run_live_engine  # noqa: E402
from src import config as _c  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
from phase1_universe_validation import make_cfg  # noqa: E402

cfg = make_cfg(140, 15)
cache = prefetch_data(cfg)
if len(cache.get("per_ticker") or {}) < 58:
    sys.exit("prefetch degraded — rerun later")

for slot in (1000, 500):
    object.__setattr__(_c.settings, "slot_target_usd", slot)
    from src.config import derive_max_positions
    m = _run_live_engine(cfg, cache, rich_metrics=True)["metrics"]
    print("SLOT_AB " + json.dumps({
        "slot_target_usd": slot,
        "slots_at_5k": derive_max_positions(cfg.account_usd),
        "trades": m.get("total_trades"),
        "daily_pnl_usd": m.get("daily_pnl_usd"),
        "max_dd_mtm_pct": m.get("max_dd_mtm_pct"),
        "profit_factor": m.get("profit_factor"),
        "cash_limited": m.get("n_cash_limited_entries"),
    }))
