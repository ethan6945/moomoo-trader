"""A/B validation (2026-06-11): breakeven +1R on the full-history window,
dynamic top-15 universe, honest lens — now INCLUDING the per-scan entry cap
parity fix. One prefetch, two sims. Never writes backtest_results.json."""
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

cfg = make_cfg(140, 15)
cache = prefetch_data(cfg)
if not cache.get("per_ticker"):
    sys.exit("prefetch failed")

for be, tag in ((False, "breakeven_OFF"), (True, "breakeven_ON")):
    # settings.use_breakeven_stop defaults true now; _run_live_engine reads it.
    # Override per-run via the cfg replace + a monkeypatched settings read is
    # brittle — instead pass through the cfg field and neutralize the settings
    # overlay by patching _run_live_engine's source settings object directly.
    from src import config as _c
    object.__setattr__(_c.settings, "use_breakeven_stop", be)
    m = _run_live_engine(cfg, cache, rich_metrics=True)["metrics"]
    print("BE_AB " + json.dumps({
        "tag": tag,
        "trades": m.get("total_trades"),
        "win_rate_pct": m.get("win_rate_pct"),
        "net_pnl_usd": m.get("net_pnl_usd"),
        "daily_pnl_usd": m.get("daily_pnl_usd"),
        "max_dd_mtm_pct": m.get("max_dd_mtm_pct"),
        "profit_factor": m.get("profit_factor"),
        "exit_reasons": m.get("exit_reasons"),
    }))
