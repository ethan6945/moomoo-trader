"""Smoke test for OpenD connection.

Two phases:
1. Quote context (no password needed) — proves OpenD is reachable + market data works.
2. Trade context (needs MOO_TRADE_PWD) — proves SIMULATE account is unlocked
   and account/positions queries work.

Run from project root:
    .venv/bin/python -m scripts.test_connection
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.moo_client import MooClient


def phase1_quote(c: MooClient) -> None:
    print(f"\n[1/2] Quote context @ {settings.moo_host}:{settings.moo_port}")
    df = c.get_kline("AAPL", bars=5)
    print(f"  ✓ AAPL last 5 daily candles fetched, latest close: ${df['close'].iloc[-1]:.2f}")
    snap = c.get_snapshot("AAPL")
    print(f"  ✓ Snapshot last_price: ${snap['last_price']:.2f}")


def phase2_trade(c: MooClient) -> None:
    print(f"\n[2/2] Trade context env={settings.moo_trade_env}")
    if not settings.moo_trade_pwd or settings.moo_trade_pwd == "your_6_digit_trade_password":
        print("  ✗ MOO_TRADE_PWD not set in .env — skipping trade test")
        return
    cash = c.get_account_cash()
    print(f"  ✓ Account cash: ${cash:,.2f}")
    positions = c.get_positions()
    print(f"  ✓ Open positions: {len(positions)}")
    if not positions.empty:
        print(positions[["code", "qty", "cost_price", "market_val"]].to_string(index=False))


def main() -> None:
    c = MooClient()
    try:
        phase1_quote(c)
        phase2_trade(c)
        print("\n✅ All checks passed.\n")
    finally:
        c.close()


if __name__ == "__main__":
    main()
