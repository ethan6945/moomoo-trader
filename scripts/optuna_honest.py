"""optuna_honest — Phase 2c: walk-forward retune on the HONEST cash-walled engine.

The optimizer was rerouted (src/optimizer.py) from the leveraged oracle to
_run_live_engine, so every Optuna trial now scores on the real $5k cash account
(cash wall + VIX + earnings + MY commissions). This driver runs the study on the
PINNED 10-name universe (the universe screen proved expansion hurts) at the
Phase-2a base (mpp=0.40), full config (ML+MR ON) so the cash dynamics match
production exactly.

Hypothesis: the incumbent params (threshold=70, tp=6.0, sl=3.25) were tuned on an
engine that could hold unlimited positions. A cash account should prefer
SELECTIVITY — the widened threshold range (55→80) lets Optuna find it.

Searches {threshold, tp_atr_mult, sl_atr_mult, max_gap_pct, base_slip_bp} for the
combined OOS Sortino (0.7·mean + 0.3·worst-fold). Whatever it finds still gets
validated on honest 180d+360d before any .env recommendation.

Run:  .venv/bin/python3 scripts/optuna_honest.py
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import optuna
from src.config import settings
from src.optimizer import run_study, print_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
optuna.logging.set_verbosity(optuna.logging.WARNING)   # mute per-trial chatter; keep our INFO

OLD10 = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]

_saved_mpp = settings.max_position_pct
object.__setattr__(settings, "max_position_pct", 0.40)   # Phase-2a base (frozen dc → bypass)
try:
    summary = run_study(
        n_trials=40, days=180, n_folds=3, min_trades=30,
        tickers=OLD10,
        # Match the PRODUCTION engine EXACTLY: ML veto ON, mean-revert OFF.
        # (fast_mode=False was the bug — it forced MR ON, inflating the trade
        # count so the search picked threshold=77, which then starved the
        # MR-off production engine to 10 trades. These explicit flags override
        # the fast_mode coupling.)
        fast_mode=True, apply_ml_gate=True, apply_mr_strategy=False,
    )
    print_summary(summary)
    print(f"\n[study json] data/optimizer/{summary['study_name']}.json")
finally:
    object.__setattr__(settings, "max_position_pct", _saved_mpp)   # never leak the patch

print("\nDONE")
