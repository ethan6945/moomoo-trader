"""Unit tests for the smart-regime layer (src/regime.py). Run:
    .venv/bin/python -m scripts.test_smart_regime
"""
import numpy as np, pandas as pd
from src import regime as R

P=F=0
def chk(n,c):
    global P,F; print(("  ok  " if c else " FAIL ")+n); P,F=(P+1,F) if c else (P,F+1)

# ── backtest parity: single-arg assess unchanged (label/block logic) ──
up = pd.Series(np.linspace(400, 500, 250))
df = pd.DataFrame({"close": up})
r = R.assess(df)                       # exactly how the backtest engines call it
chk("BULL on uptrend", r.label == "BULL" and r.bullish)
chk("no prev → confirmed == raw", r.confirmed == r.label)
chk("block_new_entries false in BULL", r.block_new_entries is False)
chk("sub_label set", r.sub_label in ("BULL", "STRONG_BULL", "HIGH_VOL_BULL"))
chk("vix None when not passed", r.vix is None)

down = pd.DataFrame({"close": pd.Series(np.linspace(500, 400, 250))})
rb = R.assess(down)
chk("BEAR on downtrend", rb.label == "BEAR" and rb.block_new_entries)

# ── positional construction still works (main.py builds Regime(...) this way) ──
manual = R.Regime("NEUTRAL", 0, 0, 0, False, False, "x")
chk("7-arg positional ctor ok", manual.label == "NEUTRAL" and manual.confirmed == "NEUTRAL")

# ── strength ──
chk("strength positive above MA", R._strength(110, 100) > 0)
chk("strength negative below MA", R._strength(90, 100) < 0)
chk("strength clamps to +1", R._strength(200, 100) == 1.0)

# ── sub_label ──
chk("STRONG_BULL", R._sub_label("BULL", 0.8, 15) == "STRONG_BULL")
chk("HIGH_VOL_BULL on hot vix", R._sub_label("BULL", 0.8, 30) == "HIGH_VOL_BULL")
chk("DEEP_BEAR", R._sub_label("BEAR", -0.8, 20) == "DEEP_BEAR")
chk("HIGH_VOL_BEAR", R._sub_label("BEAR", -0.2, 30) == "HIGH_VOL_BEAR")

# ── hysteresis (_confirmed) ──
chk("no prev → raw", R._confirmed("BEAR", 99, 100, 100, None) == "BEAR")
# prev BULL, price grazes just below 200-MA (within 0.4% band) → hold BULL
chk("graze below holds BULL", R._confirmed("NEUTRAL", 99.8, 100, 100, "BULL") == "BULL")
# prev BULL, price well below BOTH MAs (beyond band) → flip BEAR
chk("clear break flips BEAR", R._confirmed("BEAR", 98, 100, 100, "BULL") == "BEAR")
# prev BEAR, price grazes just above 200-MA → hold BEAR (no premature un-bear)
chk("graze above holds BEAR", R._confirmed("NEUTRAL", 100.2, 100, 100, "BEAR") == "BEAR")
# prev BEAR, price clearly above both → flip BULL
chk("clear rally flips BULL", R._confirmed("BULL", 102, 100, 100, "BEAR") == "BULL")

# ── risk_mult advisory ──
chk("risk_mult ≤1 and de-risks bear", R._risk_mult("HIGH_VOL_BEAR") == 0.5
    and R._risk_mult("STRONG_BULL") == 1.0)

print(f"\n{P} passed, {F} failed")
import sys; sys.exit(1 if F else 0)
