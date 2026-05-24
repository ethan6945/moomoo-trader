"""ML model calibration tracking — does proba match real-world winrate?

A model with AUC 0.72 on holdout tells you it can RANK setups well, but it
doesn't tell you whether `proba=0.6` means "60% chance of winning" or "55%".
That second number is calibration, and it matters a lot for sizing:
when we say "high conviction at proba≥0.55 → full size", we're assuming
proba is roughly calibrated to actual outcome.

This module bins live trades by predicted proba and computes actual winrate
per bucket.  After 30-50 closed trades you can plot:

    Predicted proba     Actual winrate     # trades
    0.35-0.45           0.40               12
    0.45-0.55           0.52               18
    0.55-0.65           0.61               9
    0.65-0.75           0.68               6

Diagonal = perfect calibration.  Below = model is over-confident.  Above =
model is under-confident.  Saved to data/ml/calibration.json for the GUI.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from ..config import settings
from .. import db

log = logging.getLogger(__name__)

CALIB_FILE = settings.root / "data" / "ml" / "calibration.json"

# Probability buckets — 5 ranges over [0.30, 0.80].
BUCKETS = [(0.30, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.0)]


def compute() -> dict:
    """Read all closed_trades with ml_proba_entry and bucket by predicted proba.

    Returns the per-bucket {n, winrate, mean_proba, mean_r, mean_pnl} plus
    overall stats.  Persisted to data/ml/calibration.json for GUI consumption.
    """
    rows = db.closed_trades(limit=10_000)
    valid = [r for r in rows if r.get("ml_proba_entry") is not None]

    overall = {
        "total_trades_with_proba": len(valid),
        "total_closed_trades": len(rows),
    }

    buckets_out = []
    for lo, hi in BUCKETS:
        in_bucket = [r for r in valid if lo <= float(r["ml_proba_entry"]) < hi]
        if not in_bucket:
            buckets_out.append({
                "range": f"{lo:.2f}-{hi:.2f}", "n": 0,
                "winrate": None, "mean_proba": None,
                "mean_r": None, "mean_pnl": None,
            })
            continue
        wins = sum(1 for r in in_bucket if (r.get("pnl") or 0) > 0)
        mean_proba = sum(float(r["ml_proba_entry"]) for r in in_bucket) / len(in_bucket)
        mean_r = sum((r.get("r_multiple") or 0) for r in in_bucket) / len(in_bucket)
        mean_pnl = sum((r.get("pnl") or 0) for r in in_bucket) / len(in_bucket)
        buckets_out.append({
            "range": f"{lo:.2f}-{hi:.2f}",
            "n": len(in_bucket),
            "winrate": round(wins / len(in_bucket), 3),
            "mean_proba": round(mean_proba, 3),
            "mean_r": round(mean_r, 2),
            "mean_pnl": round(mean_pnl, 2),
        })

    # Brier score (calibration distance from perfect): mean((proba - outcome)^2)
    if valid:
        squared = [
            (float(r["ml_proba_entry"]) - (1 if (r.get("pnl") or 0) > 0 else 0)) ** 2
            for r in valid
        ]
        overall["brier_score"] = round(sum(squared) / len(squared), 4)
        # Lower is better. 0.25 = random. < 0.20 = useful. < 0.15 = excellent.
    else:
        overall["brier_score"] = None

    result = {"overall": overall, "buckets": buckets_out}
    try:
        CALIB_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALIB_FILE.write_text(json.dumps(result, indent=2))
    except Exception as e:
        log.warning("calibration write failed: %s", e)
    return result


def load() -> dict:
    if not CALIB_FILE.exists():
        return {"overall": {"total_trades_with_proba": 0}, "buckets": []}
    try:
        return json.loads(CALIB_FILE.read_text())
    except json.JSONDecodeError:
        return {"overall": {"total_trades_with_proba": 0}, "buckets": []}
