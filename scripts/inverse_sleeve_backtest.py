"""Validation harness for the inverse-ETF sleeve (src/inverse_sleeve.py).

The sleeve is DEFAULT OFF and must not go live until it clears a gate. This
script is that gate. It fetches DAILY bars (so it is NOT limited by the broker's
HOUR_1 ~150-bar cap — daily gives a genuine multi-year window, which is what a
regime/trend play needs), replays the exact entry/exit rule, and answers:

  1. Over the test window, does the sleeve make money in the bear stretches?
  2. Does it BEAT just sitting in cash / SGOV over the SAME days it was deployed?
     (If not, the sleeve is pure added risk for no reward — keep it OFF.)
  3. What max drawdown does it add?

Usage (from repo root, OpenD running):
    .venv/bin/python -m scripts.inverse_sleeve_backtest
    .venv/bin/python -m scripts.inverse_sleeve_backtest --symbol SQQQ --days 500
    .venv/bin/python -m scripts.inverse_sleeve_backtest --sl-atr 3 --tp-atr 8

A PASS is: total return clearly > SGOV-on-deployed-days AND max DD not silly.
Decide with the SAME skepticism the portfolio sim applied to every other idea —
a marginal or negative result means leave INVERSE_SLEEVE_ENABLED=false.
"""
from __future__ import annotations

import argparse

import pandas as pd
import pandas_ta_classic as ta

from src.config import settings

SGOV_ANNUAL_YIELD = 0.045   # ~4.5%/yr risk-free baseline for the comparison


def _regime_series(spy: pd.DataFrame) -> pd.Series:
    """BEAR when SPY below BOTH its 200 & 50 SMA, mirroring src/regime.py."""
    close = spy["close"]
    sma200 = ta.sma(close, length=200)
    sma50 = ta.sma(close, length=50)
    label = pd.Series("NEUTRAL", index=spy.index)
    label[(close > sma200) & (close > sma50)] = "BULL"
    label[(close < sma200) & (close < sma50)] = "BEAR"
    return label


def run(symbol: str, days: int, sl_atr: float, tp_atr: float,
        sma_len: int, atr_len: int, max_hold: int) -> dict:
    from src.moo_client import client
    from moomoo import KLType

    bars = max(days + 220, 460)   # +warmup for SMA200
    with client() as c:
        spy = c.get_kline("SPY", bars=bars, ktype=KLType.K_DAY).reset_index(drop=True)
        inv = c.get_kline(symbol, bars=bars, ktype=KLType.K_DAY).reset_index(drop=True)

    n = min(len(spy), len(inv))
    spy, inv = spy.iloc[-n:].reset_index(drop=True), inv.iloc[-n:].reset_index(drop=True)
    regime = _regime_series(spy)
    inv_sma = ta.sma(inv["close"], length=sma_len)
    inv_atr = ta.atr(inv["high"], inv["low"], inv["close"], length=atr_len)

    start = max(200, sma_len, atr_len) + 1
    pos = None                       # {entry, stop, tp, day}
    trades: list[dict] = []
    deployed_days = 0

    for i in range(start, n):
        reg = regime.iloc[i]
        price = float(inv["close"].iloc[i])
        hi, lo = float(inv["high"].iloc[i]), float(inv["low"].iloc[i])
        sma = float(inv_sma.iloc[i]); atr = float(inv_atr.iloc[i])
        if pd.isna(sma) or pd.isna(atr):
            continue

        if pos is None:
            # entry rule: BEAR + inverse ETF above its own SMA + valid ATR
            if reg == "BEAR" and price > sma and atr > 0:
                pos = {"entry": price, "stop": price - sl_atr * atr,
                       "tp": price + tp_atr * atr, "day": i}
        else:
            deployed_days += 1
            exit_px = exit_reason = None
            # intraday stop/tp first (realistic fills), then close-based reasons
            if lo <= pos["stop"]:
                exit_px, exit_reason = pos["stop"], "stop"
            elif hi >= pos["tp"]:
                exit_px, exit_reason = pos["tp"], "tp"
            elif reg != "BEAR":
                exit_px, exit_reason = price, "regime"
            elif price <= sma:
                exit_px, exit_reason = price, "sma_break"
            elif i - pos["day"] >= max_hold:
                exit_px, exit_reason = price, "max_hold"
            if exit_px is not None:
                ret = (exit_px - pos["entry"]) / pos["entry"]
                trades.append({"ret": ret, "reason": exit_reason,
                               "hold": i - pos["day"]})
                pos = None

    # equity curve over trades (compounded), max drawdown
    equity = 1.0; peak = 1.0; max_dd = 0.0
    for t in trades:
        equity *= (1 + t["ret"]); peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    total_ret = equity - 1.0
    wins = [t for t in trades if t["ret"] > 0]
    sgov_on_deployed = SGOV_ANNUAL_YIELD * (deployed_days / 252.0)

    return {
        "symbol": symbol, "window_bars": n - start, "n_trades": len(trades),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "total_return_pct": total_ret * 100,
        "avg_trade_pct": (sum(t["ret"] for t in trades) / len(trades) * 100) if trades else 0.0,
        "max_dd_pct": max_dd * 100,
        "deployed_days": deployed_days,
        "sgov_baseline_pct": sgov_on_deployed * 100,
        "edge_vs_sgov_pct": (total_ret - sgov_on_deployed) * 100,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=settings.inverse_sleeve_symbol)
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--sl-atr", type=float, default=settings.inverse_sleeve_sl_atr)
    ap.add_argument("--tp-atr", type=float, default=settings.inverse_sleeve_tp_atr)
    ap.add_argument("--sma", type=int, default=settings.inverse_sleeve_sma_len)
    ap.add_argument("--atr", type=int, default=settings.inverse_sleeve_atr_len)
    ap.add_argument("--max-hold", type=int, default=settings.inverse_sleeve_max_hold_days)
    a = ap.parse_args()

    r = run(a.symbol, a.days, a.sl_atr, a.tp_atr, a.sma, a.atr, a.max_hold)
    print("\n=== Inverse-ETF sleeve backtest ===")
    print(f"  symbol={r['symbol']}  bars={r['window_bars']}  trades={r['n_trades']}  "
          f"win={r['win_rate']:.0f}%")
    print(f"  total return : {r['total_return_pct']:+.2f}%   avg/trade {r['avg_trade_pct']:+.2f}%")
    print(f"  max drawdown : {r['max_dd_pct']:.2f}%   deployed {r['deployed_days']} days")
    print(f"  SGOV baseline: {r['sgov_baseline_pct']:+.2f}% over the same deployed days")
    print(f"  EDGE vs SGOV : {r['edge_vs_sgov_pct']:+.2f}%")
    verdict = ("PASS — beats risk-free on deployed days"
               if r["edge_vs_sgov_pct"] > 0 and r["n_trades"] >= 3
               else "FAIL/INCONCLUSIVE — keep INVERSE_SLEEVE_ENABLED=false")
    print(f"  VERDICT      : {verdict}")
    print("  (Apply the same skepticism as the portfolio sim — marginal = OFF.)\n")


if __name__ == "__main__":
    main()
