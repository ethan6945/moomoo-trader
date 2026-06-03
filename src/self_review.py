"""Weekly self-review — learn from REAL fills, not backtests.

The old `_weekly_backtest_validation_job` re-ran a backtest; it never looked at
what the bot ACTUALLY did. This reviews the real closed trades from the past
week (broker fills recorded by `portfolio.record_close` → SQLite), breaks them
down by strategy / symbol / hour / exit-reason, computes the real $/day, and
emits concrete SUGGESTIONS toward the $50/day goal.

Feedback 铁律: this only ANALYZES and SUGGESTS. It NOTIFIES the owner (Telegram)
and enqueues any actionable suggestion for APPROVAL (see `approvals`). It never
silently changes live behavior.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from . import db
from .config import settings

log = logging.getLogger(__name__)

# A symbol needs at least this many trades before we'll suggest acting on its
# expectancy — fewer is just noise.
MIN_TRADES_FOR_SYMBOL_SUGGESTION = 4


def _parse_ts(s: str):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _recent_closed(days: int) -> list[dict]:
    """Closed trades whose exit timestamp is within the last `days` days."""
    rows = db.closed_trades(limit=10_000)
    if not rows:
        return []
    last_ts = _parse_ts(rows[-1].get("ts", "")) or datetime.now()
    cutoff = last_ts - timedelta(days=days)
    out = []
    for r in rows:
        t = _parse_ts(r.get("ts", ""))
        if t is not None and t >= cutoff:
            out.append(r)
    return out


def _expectancy(rows: list[dict]) -> float:
    """Average $ PnL per trade."""
    return sum(r["pnl"] for r in rows) / len(rows) if rows else 0.0


def weekly_review(days: int = 7) -> dict:
    """Analyse the last `days` of REAL fills. Returns a structured report."""
    rows = _recent_closed(days)
    n = len(rows)
    if n == 0:
        return {"days": days, "n_trades": 0, "note": "no closed trades this period"}

    total_pnl = sum(r["pnl"] for r in rows)
    wins = [r for r in rows if r["pnl"] > 0]
    win_rate = len(wins) / n * 100
    avg_r = sum(r.get("r_multiple", 0.0) for r in rows) / n
    per_day = total_pnl / days

    # --- breakdowns ---
    def _agg(key_fn):
        buckets: dict = defaultdict(list)
        for r in rows:
            buckets[key_fn(r)].append(r)
        return {
            k: {"n": len(v), "pnl": round(sum(x["pnl"] for x in v), 2),
                "expectancy": round(_expectancy(v), 2),
                "win_rate": round(sum(1 for x in v if x["pnl"] > 0) / len(v) * 100, 1)}
            for k, v in buckets.items()
        }

    by_strategy = _agg(lambda r: r.get("strategy") or "trend")
    by_symbol = _agg(lambda r: r.get("symbol", "?"))
    by_exit = _agg(lambda r: r.get("exit_reason", "?"))

    def _hour(r):
        t = _parse_ts(r.get("opened_at") or r.get("ts") or "")
        return f"{t.hour:02d}:00" if t else "?"
    by_hour = _agg(_hour)

    # --- suggestions (analyze only — routed to approval, never auto-applied) ---
    suggestions = []

    # Chronic-loser symbols → suggest blacklist review.
    for sym, agg in sorted(by_symbol.items(), key=lambda kv: kv[1]["pnl"]):
        if agg["n"] >= MIN_TRADES_FOR_SYMBOL_SUGGESTION and agg["pnl"] < 0:
            suggestions.append({
                "kind": "blacklist_review",
                "symbol": sym,
                "detail": f"{sym}: {agg['n']} trades, ${agg['pnl']:.0f} PnL, "
                          f"{agg['win_rate']:.0f}% win — net loser this week",
                "action": f"Consider blacklisting {sym} (or tightening its entry).",
            })

    # Strategy net-negative → flag.
    for strat, agg in by_strategy.items():
        if agg["n"] >= MIN_TRADES_FOR_SYMBOL_SUGGESTION and agg["pnl"] < 0:
            suggestions.append({
                "kind": "strategy_flag",
                "strategy": strat,
                "detail": f"strategy '{strat}': {agg['n']} trades, ${agg['pnl']:.0f}, "
                          f"{agg['win_rate']:.0f}% win — net drag this week",
                "action": f"Watch '{strat}' — if it stays negative, consider disabling.",
            })

    # Exit-reason balance: lots of stop-outs vs take-profits.
    sl = sum(v["n"] for k, v in by_exit.items() if "SL" in k.upper() or "STOP" in k.upper())
    tp = sum(v["n"] for k, v in by_exit.items() if "TP" in k.upper())
    if sl > 0 and tp >= 0 and sl > 2 * max(tp, 1):
        suggestions.append({
            "kind": "exit_balance",
            "detail": f"{sl} stop-outs vs {tp} take-profits — getting stopped a lot",
            "action": "Review entry timing / SL width — many stops may mean chasing.",
        })

    # Performance vs target.
    gap = settings_target_per_day() - per_day
    target_note = (f"${per_day:.2f}/day vs ${settings_target_per_day():.0f} target "
                   f"({'+' if gap <= 0 else '-'}${abs(gap):.2f})")

    return {
        "days": days,
        "n_trades": n,
        "total_pnl": round(total_pnl, 2),
        "per_day": round(per_day, 2),
        "win_rate": round(win_rate, 1),
        "avg_r_multiple": round(avg_r, 2),
        "by_strategy": by_strategy,
        "by_symbol": by_symbol,
        "by_hour": by_hour,
        "by_exit": by_exit,
        "suggestions": suggestions,
        "target_note": target_note,
    }


def settings_target_per_day() -> float:
    """Daily profit target — owner's goal. Env-overridable, default $50."""
    import os
    try:
        return float(os.getenv("DAILY_PROFIT_TARGET", "50"))
    except ValueError:
        return 50.0


def format_telegram(report: dict) -> str:
    if report.get("n_trades", 0) == 0:
        return (f"🪞 *每周自省* ({report.get('days', 7)}d)\n"
                f"  本周无平仓交易。")
    lines = [
        f"🪞 *每周自省* — 真实成交复盘 ({report['days']}d)",
        f"  成交: {report['n_trades']}  |  胜率: {report['win_rate']:.0f}%  |  avgR: {report['avg_r_multiple']:.2f}",
        f"  实现盈亏: ${report['total_pnl']:+.0f}  →  *{report['target_note']}*",
    ]
    bs = report.get("by_strategy", {})
    if bs:
        lines.append("  策略: " + ", ".join(
            f"{k} ${v['pnl']:+.0f}({v['n']})" for k, v in bs.items()))
    # Worst + best symbol
    bsym = report.get("by_symbol", {})
    if bsym:
        worst = min(bsym.items(), key=lambda kv: kv[1]["pnl"])
        best = max(bsym.items(), key=lambda kv: kv[1]["pnl"])
        lines.append(f"  最佳: {best[0]} ${best[1]['pnl']:+.0f}  |  最差: {worst[0]} ${worst[1]['pnl']:+.0f}")
    sg = report.get("suggestions", [])
    if sg:
        lines.append(f"\n  💡 *建议 {len(sg)} 条* (需你在 GUI/回复批准, 不会自动执行):")
        for s in sg[:6]:
            lines.append(f"   • {s['action']}")
    else:
        lines.append("\n  ✓ 无需调整 — 本周没有明显的亏损模式。")
    return "\n".join(lines)


def run_and_notify(days: int = 7) -> dict:
    """Cron entry: analyse → notify → enqueue suggestions for approval.

    Never changes live behavior. Returns the report (also for tests/GUI)."""
    from . import notifier
    report = weekly_review(days)
    try:
        notifier.send(format_telegram(report))
    except Exception as e:
        log.warning("self-review notify failed: %s", e)
    # Enqueue actionable suggestions for owner approval (analyze→notify→approve).
    try:
        from . import approvals
        for s in report.get("suggestions", []):
            approvals.enqueue(kind=s["kind"], detail=s.get("detail", ""),
                              action=s.get("action", ""), payload=s)
    except Exception as e:
        log.debug("approval enqueue skipped (channel not ready): %s", e)
    # Autonomous optimizer (DeepSeek) — proposes param tweaks into the same
    # approval queue. No-op until DEEPSEEK_API_KEY is set. Never auto-applies.
    try:
        from . import optimizer_ai
        n = optimizer_ai.propose_from_review(report)
        if n:
            from . import notifier
            notifier.send(f"🤖 DeepSeek 优化器提了 {n} 条参数建议 — 待你在 GUI/CLI 批准。")
    except Exception as e:
        log.debug("optimizer_ai skipped: %s", e)
    return report
