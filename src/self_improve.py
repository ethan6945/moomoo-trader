"""Self-improvement proposals from REAL fills → approval queue.

Two evidence-based proposers, run from the weekly self-review:
  • half_kelly_proposal  (#5) — estimate the account's real edge (win-rate +
    payoff ratio) from closed trades, size risk_per_trade at HALF-Kelly, and
    propose it for approval. Replaces the fragile rolling-Sortino multiplier as
    the "how much to risk" brain.
  • universe_review      (#10) — rank the watchlist by real per-symbol
    expectancy over a long window and propose dropping chronic losers.

Both only PROPOSE (approvals.enqueue) — nothing changes until the owner approves
(feedback 铁律). The DeepSeek optimizer (optimizer_ai) and these share the queue.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from . import approvals, db, runtime_config
from .config import settings

log = logging.getLogger(__name__)

KELLY_MIN_TRADES = 20          # need this many closed trades to trust the edge
KELLY_RISK_FLOOR = 0.01        # never propose < 1% risk
KELLY_RISK_CEIL = 0.08         # never propose > 8% risk (cash account, no leverage)
UNIVERSE_MIN_TRADES = 6        # per-symbol trades before a drop suggestion
UNIVERSE_LOOKBACK_DAYS = 90


def _recent(days: int | None = None) -> list[dict]:
    rows = db.closed_trades(limit=10_000)
    if days is None or not rows:
        return rows
    def _ts(r):
        try:
            return datetime.fromisoformat(r.get("ts", ""))
        except (ValueError, TypeError):
            return None
    last = _ts(rows[-1]) or datetime.now()
    cutoff = last - timedelta(days=days)
    return [r for r in rows if (_ts(r) or last) >= cutoff]


# ── #5 half-Kelly risk sizing ────────────────────────────────────────────────
def half_kelly_risk(rows: list[dict]) -> tuple[float | None, str]:
    """Return (suggested_risk_per_trade, reason) or (None, why-not).

    Kelly f* = p − (1−p)/b, where p = win rate, b = avg_win / avg_loss (payoff).
    We use HALF-Kelly (f*/2) — full Kelly is famously too volatile for real money.
    """
    if len(rows) < KELLY_MIN_TRADES:
        return None, f"warmup: {len(rows)}/{KELLY_MIN_TRADES} trades"
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    losses = [abs(r["pnl"]) for r in rows if r["pnl"] < 0]
    if not wins or not losses:
        return None, "need both wins and losses to estimate edge"
    p = len(wins) / len(rows)
    avg_w = sum(wins) / len(wins)
    avg_l = sum(losses) / len(losses)
    b = avg_w / avg_l if avg_l > 0 else 0.0
    if b <= 0:
        return None, "degenerate payoff ratio"
    kelly = p - (1 - p) / b
    if kelly <= 0:
        return None, f"no edge (Kelly {kelly:.2f} ≤ 0, p={p:.0%}, b={b:.2f}) — don't size up"
    half = kelly / 2
    risk = max(KELLY_RISK_FLOOR, min(half, KELLY_RISK_CEIL))
    return round(risk, 3), (f"half-Kelly {half:.3f} (p={p:.0%}, payoff b={b:.2f}, "
                            f"{len(rows)} trades) → risk_per_trade {risk:.1%}")


def half_kelly_proposal(rows: list[dict] | None = None) -> bool:
    """Enqueue a risk_per_trade proposal if half-Kelly differs materially from
    the live setting. Returns True if a proposal was enqueued."""
    rows = rows if rows is not None else _recent(None)
    risk, reason = half_kelly_risk(rows)
    if risk is None:
        log.info("half-Kelly: %s", reason)
        return False
    # Compare against the value actually in force (runtime override beats the
    # frozen .env), so a once-approved change converges instead of re-proposing
    # the same card every week.
    current = runtime_config.risk_per_trade()
    # Only propose on a material change (>= 0.5 percentage point).
    if abs(risk - current) < 0.005:
        log.info("half-Kelly: %.1f%% ≈ current %.1f%% — no change", risk * 100, current * 100)
        return False
    # Backtest-validate on the honest engine — RISK-adjusted: a protective
    # down-size whose $/day dips is allowed if it cuts drawdown more (Calmar↑).
    # A change that worsens risk-adjusted return on either window is dropped. If
    # the backtest can't run (OpenD hiccup) we still enqueue — Kelly is itself an
    # evidence-based estimate from real fills — but mark it un-backtested.
    from . import optimizer_ai
    bt = optimizer_ai.backtest_risk_change(risk)
    if bt is not None and not bt["risk_adjusted_ok"]:
        log.info("half-Kelly: risk_per_trade %.1f%%→%.1f%% worsens risk-adjusted "
                 "return on backtest — not enqueued", current * 100, risk * 100)
        return False
    if bt is not None:
        evidence = (f" — 回测 180d ${bt['per_day'][180]:.1f}/day DD{bt['dd'][180]:.1f}% "
                    f"vs 当前 ${bt['base_per_day'][180]:.1f}/day DD{bt['base_dd'][180]:.1f}%")
        tag = "half-Kelly(回测验证)"
    else:
        evidence = " — 回测未跑(OpenD?)，依据真实成交 Kelly 估计"
        tag = "half-Kelly"
    approvals.enqueue(
        kind="param_change",
        detail=f"{tag}: risk_per_trade {current:.1%} → {risk:.1%} — {reason}{evidence}",
        action=f"Set risk_per_trade = {risk} (live, no restart)",
        payload={"key": "risk_per_trade", "value": risk},
    )
    return True


# ── #10 PnL-driven universe review ───────────────────────────────────────────
def universe_review(lookback_days: int = UNIVERSE_LOOKBACK_DAYS) -> dict:
    """Rank the watchlist by real per-symbol expectancy; propose dropping
    chronic losers. Returns the ranking (also for the GUI/report)."""
    rows = _recent(lookback_days)
    by_sym: dict = defaultdict(list)
    for r in rows:
        by_sym[r.get("symbol", "?")].append(r)
    ranking = []
    for sym, rs in by_sym.items():
        pnl = sum(r["pnl"] for r in rs)
        exp = pnl / len(rs)
        wr = sum(1 for r in rs if r["pnl"] > 0) / len(rs) * 100
        ranking.append({"symbol": sym, "n": len(rs), "pnl": round(pnl, 2),
                        "expectancy": round(exp, 2), "win_rate": round(wr, 1)})
    ranking.sort(key=lambda x: x["expectancy"])

    n_proposed = 0
    for r in ranking:
        if r["n"] >= UNIVERSE_MIN_TRADES and r["expectancy"] < 0 and r["pnl"] < 0:
            approvals.enqueue(
                kind="blacklist_review",
                detail=f"universe ({lookback_days}d): {r['symbol']} expectancy "
                       f"${r['expectancy']}/trade over {r['n']} trades, "
                       f"${r['pnl']} total, {r['win_rate']:.0f}% win — chronic drag",
                action=f"Drop {r['symbol']} from the universe (blacklist).",
                payload={"symbol": r["symbol"]},
            )
            n_proposed += 1
    return {"ranking": ranking, "n_proposed": n_proposed, "lookback_days": lookback_days}


def run_all(review_rows: list[dict] | None = None) -> dict:
    """Run both proposers (called by the weekly self-review). Returns a summary."""
    kelly = half_kelly_proposal()
    uni = universe_review()
    return {"kelly_proposed": kelly, "universe_dropped_proposed": uni["n_proposed"],
            "universe_ranking": uni["ranking"]}
