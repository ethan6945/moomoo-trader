from src import cash_yield as cy
from src.config import settings

P = F = 0
def chk(name, cond):
    global P, F
    print(("  ok  " if cond else " FAIL ")+name);
    P, F = (P+1, F) if cond else (P, F+1)

print("cfg:", dict(buf=settings.cash_yield_buffer_usd, mn=settings.cash_yield_min_trade_usd,
      only_bear=settings.cash_yield_only_bear, sym=settings.cash_yield_symbol))

# BEAR: park idle cash
d = cy.decide("BEAR", current_cash=10000, held_qty=0, price=100.4)
chk("BEAR buys", d["action"]=="buy" and d["qty"]==int(9500/(100.4*1.001)))

# BULL holding → sell all (free cash for longs)
d = cy.decide("BULL", current_cash=200, held_qty=50, price=100.4)
chk("BULL sells all held", d["action"]=="sell" and d["qty"]==50)

# BULL flat → hold
chk("BULL flat holds", cy.decide("BULL", 200, 0, 100.4)["action"]=="hold")

# NEUTRAL counts as tradeable when only_bear → unwind
d = cy.decide("NEUTRAL", 200, 10, 100.0)
chk("NEUTRAL unwinds (only_bear)", d["action"]=="sell" and d["qty"]==10)

# BEAR but idle cash below min sweep → hold
chk("BEAR dust holds", cy.decide("BEAR", 800, 0, 100.4)["action"]=="hold")

# BEAR but no price → hold
chk("BEAR no-price holds", cy.decide("BEAR", 10000, 0, 0)["action"]=="hold")

print(f"\n{P} passed, {F} failed")
import sys; sys.exit(1 if F else 0)
