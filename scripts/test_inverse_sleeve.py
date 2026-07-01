import pandas as pd, numpy as np
from datetime import datetime, timezone, timedelta
from src import inverse_sleeve as iv
from src.config import settings

P=F=0
def chk(n,c):
    global P,F; print(("  ok  " if c else " FAIL ")+n);
    P,F=(P+1,F) if c else (P,F+1)

# ── indicators on a synthetic uptrending inverse-ETF df ──
close = pd.Series(np.linspace(20, 30, 40))
df = pd.DataFrame({"close": close, "high": close+0.2, "low": close-0.2, "open": close})
ind = iv.indicators(df)
chk("indicators returns price/sma/atr", ind and ind["price"]>ind["sma"] and ind["atr"]>0)

# ── entry_signal ──
ind = {"price": 30.0, "sma": 28.0, "atr": 1.0}
e = iv.entry_signal("BEAR", ind)
chk("BEAR+trending enters", e is not None and e["stop"]==round(30-settings.inverse_sleeve_sl_atr*1.0,2)
    and e["tp"]==round(30+settings.inverse_sleeve_tp_atr*1.0,2))
chk("non-BEAR no entry", iv.entry_signal("BULL", ind) is None)
chk("below-SMA no entry", iv.entry_signal("BEAR", {"price":27.0,"sma":28.0,"atr":1.0}) is None)
chk("zero-ATR no entry", iv.entry_signal("BEAR", {"price":30.0,"sma":28.0,"atr":0.0}) is None)

# ── exit_signal ──
pos = {"entry":30.0,"stop":27.5,"tp":36.0,"opened_at":datetime.now(timezone.utc).isoformat()}
chk("regime flip exits", iv.exit_signal("BULL", {"price":31,"sma":28}, pos)=="regime no longer BEAR")
chk("sma break exits", iv.exit_signal("BEAR", {"price":27.9,"sma":28}, pos) is not None)
chk("stop hit exits", iv.exit_signal("BEAR", {"price":27.4,"sma":25}, pos)=="ATR stop hit (market rallied)")
chk("tp hit exits", iv.exit_signal("BEAR", {"price":36.5,"sma":25}, pos)=="ATR take-profit hit")
chk("in-trend no exit", iv.exit_signal("BEAR", {"price":31,"sma":28}, pos) is None)
old = {**pos, "opened_at": (datetime.now(timezone.utc)-timedelta(days=40)).isoformat()}
chk("max-hold exits", "max-hold" in (iv.exit_signal("BEAR", {"price":31,"sma":28}, old) or ""))

# ── size caps ──
q = iv.size(price=30.0, stop=27.5, budget=4500.0)
cap = int(4500*settings.inverse_sleeve_max_pct/30.0)
risk = int(4500*settings.risk_per_trade/2.5)
chk("size = min(risk,cap)", q==max(0,min(risk,cap)))
chk("size 0 on bad price", iv.size(0,0,4500)==0)

# ── harness regime series ──
from scripts.inverse_sleeve_backtest import _regime_series
spy = pd.DataFrame({"close": pd.Series(np.r_[np.linspace(500,400,210)])})  # downtrend
reg = _regime_series(spy)
chk("regime series marks BEAR in downtrend", (reg=="BEAR").iloc[-1])

print(f"\n{P} passed, {F} failed")
import sys; sys.exit(1 if F else 0)
