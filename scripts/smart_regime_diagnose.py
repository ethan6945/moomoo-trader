"""Validation harness for SMART_REGIME hysteresis (src/regime.py).

The smart layer's one behavior change is gating on the HYSTERESIS-smoothed
`confirmed` label instead of the raw label. The win is robustness: fewer 1-day
whipsaws around the 200-MA, which means the cash-yield / inverse-sleeve sweeps
(and the entry block) stop thrashing in and out of BEAR. This script quantifies
that over real SPY history so you can decide with data, not vibes.

It replays SPY daily bars day-by-day, computing the RAW label and the CONFIRMED
label (threading the previous confirmed label through src.regime's exact
hysteresis), and reports: regime FLIPS (raw vs confirmed), BEAR days, and how
many short BEAR head-fakes the deadband absorbs.

Run (repo root, OpenD up):  .venv/bin/python -m scripts.smart_regime_diagnose
"""
from __future__ import annotations

import argparse

import pandas as pd
import pandas_ta_classic as ta

from src import regime as regime_mod


def _raw_label(price: float, sma200: float, sma50: float) -> str:
    above200, above50 = price > sma200, price > sma50
    if above200 and above50:
        return "BULL"
    if not above200 and not above50:
        return "BEAR"
    return "NEUTRAL"


def _flips(seq: list[str]) -> int:
    return sum(1 for a, b in zip(seq, seq[1:]) if a != b)


def _short_runs(seq: list[str], label: str, max_len: int = 2) -> int:
    """Count runs of `label` no longer than max_len (the whipsaw head-fakes)."""
    n = runs = i = 0
    while i < len(seq):
        if seq[i] == label:
            j = i
            while j < len(seq) and seq[j] == label:
                j += 1
            if (j - i) <= max_len:
                runs += 1
            i = j
        else:
            i += 1
    return runs


def run(days: int) -> dict:
    from src.moo_client import client
    from moomoo import KLType
    bars = max(days + 220, 460)
    with client() as c:
        spy = c.get_kline("SPY", bars=bars, ktype=KLType.K_DAY).reset_index(drop=True)
    close = spy["close"]
    sma200 = ta.sma(close, length=200)
    sma50 = ta.sma(close, length=50)

    raw_seq: list[str] = []
    conf_seq: list[str] = []
    prev_conf: str | None = None
    for i in range(200, len(spy)):
        p, s200, s50 = float(close.iloc[i]), float(sma200.iloc[i]), float(sma50.iloc[i])
        if pd.isna(s200) or pd.isna(s50):
            continue
        raw = _raw_label(p, s200, s50)
        conf = regime_mod._confirmed(raw, p, s200, s50, prev_conf)
        raw_seq.append(raw)
        conf_seq.append(conf)
        prev_conf = conf

    return {
        "days": len(raw_seq),
        "raw_flips": _flips(raw_seq),
        "confirmed_flips": _flips(conf_seq),
        "raw_bear_days": raw_seq.count("BEAR"),
        "confirmed_bear_days": conf_seq.count("BEAR"),
        "raw_short_bear_runs": _short_runs(raw_seq, "BEAR"),
        "confirmed_short_bear_runs": _short_runs(conf_seq, "BEAR"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=500)
    a = ap.parse_args()
    r = run(a.days)
    flip_cut = r["raw_flips"] - r["confirmed_flips"]
    pct = (flip_cut / r["raw_flips"] * 100) if r["raw_flips"] else 0.0
    print("\n=== SMART_REGIME hysteresis diagnostic ===")
    print(f"  window: {r['days']} trading days")
    print(f"  regime flips  : raw {r['raw_flips']}  →  confirmed {r['confirmed_flips']}  "
          f"({flip_cut} fewer, −{pct:.0f}%)")
    print(f"  BEAR days     : raw {r['raw_bear_days']}  →  confirmed {r['confirmed_bear_days']}")
    print(f"  short BEAR head-fakes (≤2d): raw {r['raw_short_bear_runs']}  →  "
          f"confirmed {r['confirmed_short_bear_runs']}")
    verdict = ("HELPS — fewer whipsaws, BEAR-day count barely changed"
               if flip_cut > 0 and abs(r["confirmed_bear_days"] - r["raw_bear_days"]) <= r["days"] * 0.05
               else "MARGINAL — review before enabling")
    print(f"  VERDICT       : {verdict}")
    print("  (Enable SMART_REGIME_ENABLED=true only if the whipsaw cut looks real.)\n")


if __name__ == "__main__":
    main()
