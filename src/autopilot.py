"""Autopilot — P2-1 (2026-06-26): DeepSeek autonomous portfolio manager.

WEEKLY SCHEDULE (Monday 09:00 ET = Monday 21:00 KL GMT+8):
  1. COLLECT: self_review + breadth + strategy perf + ML training + backtest
  2. PROMPT: send the full report to DeepSeek with guardrails
  3. VALIDATE: each proposal gets a backtest_v3 honest-engine check
  4. APPLY: within guardrails → auto-apply (runtime override, no restart);
           outside guardrails → approval queue (owner decides)
  5. NOTIFY: Telegram summary of everything that happened

SETUP: the owner connects DeepSeek API keys in .env (DEEPSEEK_API_KEY).
When no DeepSeek key is configured, the autopilot is a no-op — the rules-
based self_review and monthly Optuna still run unchanged.

GUARDRAILS (hardcoded — DeepSeek cannot override):
  • risk_per_trade:      1% – 8%
  • entry_threshold:     55 – 85
  • tp_atr_mult:         2 – 12
  • sl_atr_mult:         2 – 6
  • max_hold_days:       5 – 10
  • universe_top_n:      10 – 20
  • max_position_pct:    0.20 – 0.55
  • max_changes/week:    3
  • drawdown > 10%:      FREEZE all autonomous changes
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ai, approvals, breadth, db, notifier, relative_strength, runtime_config, strategy_gate
from .config import settings

log = logging.getLogger(__name__)

# ── Guardrails ────────────────────────────────────────────────────────────────
GUARDRAILS = {
    "risk_per_trade":      (0.01,  0.08),
    "entry_threshold":     (55,    85),
    "tp_atr_mult":         (2,     12),
    "sl_atr_mult":         (2,     6),
    "max_hold_days":       (5,     10),
    "universe_top_n":      (10,    20),
    "max_position_pct":    (0.20,  0.55),
}

MAX_CHANGES_PER_WEEK = 3
DRAWDOWN_FREEZE_PCT = 0.10   # freeze all auto-changes when DD ≥ 10%

# ── System prompt (sent to DeepSeek) ─────────────────────────────────────────
SYSTEM_PROMPT = """You are the autonomous portfolio manager for moomoo-trader.

ACCOUNT ($5,000 US cash, no leverage, long-only, no shorting):
- Max 5 concurrent positions
- Single position ≤ 55% of account
- Single trade risk ≤ 8% (half-Kelly ceiling)
- Daily max loss stop: -5% (auto-halts)
- Friday after 14:00 ET: no new entries (weekend gap protection)

ADJUSTABLE PARAMETERS (you propose 0-3 changes per week, SMALL moves only):
- entry_threshold (55-85): {entry_threshold} — lower = more trades, more noise
- tp_atr_mult (2-12): {tp_atr_mult} — take-profit in ATR units
- sl_atr_mult (2-6): {sl_atr_mult} — stop-loss in ATR units
- breakeven_trigger_r (0.75-1.5): {breakeven_trigger_r}
- max_hold_days (5-10): {max_hold_days}
- universe_top_n (10-20): {universe_top_n}
- max_position_pct (0.20-0.55): {max_position_pct}

KNOWN PLATEAU PEAKS (don't move these without NEW evidence):
- entry_threshold=70 is optimal (180d sweep: 55→−$11/day, 85→cuts trades)
- tp_atr_mult=8 and max_hold_days=7 sit on measured plateau peaks
- max_position_pct=0.40 beat 0.30/0.50/0.70 in the 2026-06-11 sweep

ACTIVE STRATEGIES:
{strategy_status}

THIS WEEK'S REAL PERFORMANCE:
{weekly_report}

CURRENT MARKET ENVIRONMENT:
{market_context}

RECENT BACKTEST (health check):
{backtest_health}

ML SCORER STATUS:
{ml_status}

INSTRUCTIONS:
1. Look at the real-fill data FIRST. If a strategy is losing money over
   15+ trades, propose pausing it. If all strategies are healthy, do nothing.
2. Only propose parameter changes with SPECIFIC evidence from the report.
   "The win rate is low" is not evidence. "6 of 8 stop-outs happened on
   trades held past day 5, and avg win is $153 vs avg loss $55 — suggest
   reducing max_hold_days from 7 to 5" IS evidence.
3. Prefer SMALL moves (±10% or less).
4. If everything looks healthy and no change is warranted, return [].
5. Remember: the backtest is an estimate. The real fills are the truth.

Reply with STRICT JSON, no markdown, no prose outside the array:
[{{"key":"param_name", "value":new_value, "rationale":"one SHORT sentence citing specific data"}}]

Iron rule: fewer proposals is better. One good change beats three guesses."""


def _current_params() -> dict:
    return {
        "entry_threshold": runtime_config.entry_threshold(),
        "tp_atr_mult": runtime_config.tp_atr_mult(),
        "sl_atr_mult": runtime_config.sl_atr_mult(),
        "breakeven_trigger_r": runtime_config.breakeven_trigger_r(),
        "max_hold_days": runtime_config.max_hold_days(),
        "universe_top_n": runtime_config.universe_top_n(),
        "max_position_pct": runtime_config.max_position_pct(),
    }


def _within_guardrails(key: str, value: float) -> bool:
    """Check if a proposed value is inside the hardcoded guardrail bounds."""
    if key not in GUARDRAILS:
        return True  # unknown key → approval queue, not auto-apply
    lo, hi = GUARDRAILS[key]
    return lo <= value <= hi


def _current_drawdown() -> float:
    """Return the current realized drawdown as a fraction (0.0 = no loss)."""
    try:
        state = db.get_state()
        realized = float(state.get("realized_pnl_total", 0))
    except Exception:
        realized = 0.0
    # DD = negative realized / seed capital
    if realized >= 0:
        return 0.0
    seed = float(settings.account_usd)
    if seed <= 0:
        return 0.0
    return abs(realized) / seed


def _run_backtest_with_param(key: str, value: float) -> dict | None:
    """Run a quick backtest with one parameter changed. Returns metrics or None."""
    try:
        from .backtest_v3 import _base_cfg, simulate_v3
        cfg = _base_cfg(days=90)
        cfg_params = {
            "threshold": runtime_config.entry_threshold(),
            "tp_atr_mult": runtime_config.tp_atr_mult(),
            "sl_atr_mult": runtime_config.sl_atr_mult(),
            "max_hold_days": runtime_config.max_hold_days(),
            "risk_per_trade": runtime_config.risk_per_trade(),
            "max_position_pct": runtime_config.max_position_pct(),
            "universe_top_n": runtime_config.universe_top_n(),
            "breakeven_trigger_r": runtime_config.breakeven_trigger_r(),
        }
        # Map the key to the backtest config field
        key_map = {
            "entry_threshold": "threshold",
            "tp_atr_mult": "tp_atr_mult",
            "sl_atr_mult": "sl_atr_mult",
            "max_hold_days": "max_hold_days",
            "risk_per_trade": "risk_per_trade",
            "max_position_pct": "max_position_pct",
            "universe_top_n": "universe_top_n",
            "breakeven_trigger_r": "breakeven_trigger_r",
        }
        if key in key_map:
            cfg_params[key_map[key]] = value
        from dataclasses import replace
        cfg = replace(cfg, **{k: v for k, v in cfg_params.items()
                              if hasattr(cfg, k)})
        # Prefetch + simulate
        from .backtest import prefetch_data
        cache = prefetch_data(cfg)
        result = simulate_v3(cfg, cache, enforce_cash=True)
        metrics = result.get("metrics", {})
        return {
            "sortino": metrics.get("sortino_ratio", 0),
            "net_pnl": metrics.get("net_pnl_usd", 0),
            "per_day": metrics.get("per_day_usd", 0),
            "max_dd": metrics.get("max_drawdown_pct", 0),
            "n_trades": metrics.get("total_trades", 0),
            "profit_factor": metrics.get("profit_factor", 0),
        }
    except Exception as e:
        log.warning("backtest validation failed for %s=%s: %s", key, value, e)
        return None


def weekly_autopilot() -> dict:
    """Run ONE weekly autopilot cycle. Called every Sunday 18:00 ET by cron.

    Returns a dict summary of what happened, also pushed to Telegram.
    """
    if not ai.has_key("deepseek"):
        log.info("Autopilot: no DeepSeek key — skipping (self_review still runs)")
        return {"status": "skipped", "reason": "no DeepSeek key"}

    # ── 0. Pre-flight: drawdown check ──
    dd = _current_drawdown()
    if dd >= DRAWDOWN_FREEZE_PCT:
        notifier.send(f"🛑 Autopilot FROZEN: drawdown {dd:.1%} ≥ {DRAWDOWN_FREEZE_PCT:.0%}")
        return {"status": "frozen", "drawdown": round(dd, 3)}

    log.info("=== Autopilot weekly cycle start ===")

    # ── 1. Collect performance data ──
    # Self-review (real fills from past 7 days)
    try:
        from . import self_review
        report = self_review.weekly_review(days=7)
    except Exception as e:
        report = {"error": str(e)}
        log.warning("self_review failed: %s", e)

    # Strategy performance (90-day)
    try:
        strat_perf = relative_strength.strategy_performance(90)
    except Exception as e:
        strat_perf = {"error": str(e)}

    # Market context (latest breadth + regime)
    market_ctx = "market data unavailable"
    try:
        from moomoo import KLType
        from .moomoo_client import client
        with client() as c:
            bv = breadth.assess(c)
            market_ctx = (
                f"VIX: {bv.vix:.1f} | Breadth: {bv.note}\n"
                f"A/D ratio: {bv.ad_ratio:.2f} | %>50MA: {bv.pct_above_50ma:.0f}%"
            )
    except Exception as e:
        log.warning("market context fetch failed: %s", e)

    # Backtest health check (90-day)
    bt_health = "not available"
    try:
        from .optimizer_ai import _base_cfg
        from .backtest import run_backtest
        cfg = _base_cfg(days=90)
        bt_result = run_backtest(cfg)
        m = bt_result.get("metrics", {})
        bt_health = (
            f"90d: {m.get('total_trades',0)} trades, "
            f"${m.get('net_pnl_usd',0):+.0f}, "
            f"Sortino {m.get('sortino_ratio',0):.1f}, "
            f"PF {m.get('profit_factor',0):.2f}, "
            f"DD {m.get('max_drawdown_pct',0):.1f}%"
        )
    except Exception as e:
        log.warning("backtest health check failed: %s", e)

    # ML scorer status
    ml_status = "model not trained"
    try:
        from . import ml_scorer
        if ml_scorer.model_exists():
            n_trades = ml_scorer._trade_count()
            w = ml_scorer._current_ml_weight()
            ml_status = f"model active | {n_trades} labeled trades | ML weight {w:.0%}"
        else:
            ml_status = f"not trained yet ({ml_scorer._trade_count()} trades)"
    except Exception:
        pass

    # Strategy status
    strategy_status_lines = []
    for strat, data in sorted(strat_perf.items(), key=lambda kv: kv[1].get("expectancy", 0)):
        if isinstance(data, dict):
            strategy_status_lines.append(
                f"  {strat}: {data.get('n',0)} trades, "
                f"${data.get('expectancy',0):.2f}/trade, "
                f"{data.get('win_rate',0):.0f}% win"
            )
    strategy_status = "\n".join(strategy_status_lines) or "no trade data"

    # P2-2/3: extreme market lock + live/backtest deviation check
    # Run BEFORE asking DeepSeek so the gate results feed into the prompt
    gate_result = {"extreme_market": False, "live_vs_bt": {}}
    try:
        gate_result = strategy_gate.weekly_gate_check()
        if gate_result.get("extreme_market", {}).get("locked"):
            market_ctx += f"\n🚨 EXTREME MARKET LOCK: {gate_result['extreme_market']['reason']}"
        lvb = gate_result.get("live_vs_bt", {})
        if lvb.get("alert"):
            market_ctx += f"\n⚠ Live/BT deviation: {lvb.get('note','')}"
    except Exception as e:
        log.warning("strategy gate check failed: %s", e)

    # ── 2. Ask DeepSeek ──
    prompt = SYSTEM_PROMPT.format(
        weekly_report=json.dumps(report, indent=2, default=str)
        if isinstance(report, dict) else str(report),
        market_context=market_ctx,
        backtest_health=bt_health,
        ml_status=ml_status,
        strategy_status=strategy_status,
        **_current_params(),
    )

    proposals = []
    log.info("Autopilot: asking DeepSeek...")
    try:
        text, _ = ai.generate(prompt)
        # Extract JSON array from the response
        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            proposals = json.loads(match.group(0))
        else:
            log.info("Autopilot: DeepSeek returned no proposals (empty)")
    except Exception as e:
        log.error("Autopilot: DeepSeek call failed: %s", e)
        notifier.send(f"⚠ Autopilot: DeepSeek unavailable — skipping this week ({e})")
        return {"status": "ai_error", "error": str(e)}

    if not proposals:
        log.info("Autopilot: no changes proposed — all healthy")
        notifier.send("🤖 *Autopilot*: no changes this week — everything looks healthy")
        return {"status": "no_changes", "n_proposals": 0}

    # ── 3. Validate and apply ──
    if len(proposals) > MAX_CHANGES_PER_WEEK:
        proposals = proposals[:MAX_CHANGES_PER_WEEK]
        log.warning("Autopilot: %d proposals → capped at %d",
                     len(proposals), MAX_CHANGES_PER_WEEK)

    applied = []
    queued = []
    skipped = []

    for p in proposals:
        key = p.get("key", "")
        value = p.get("value")
        rationale = p.get("rationale", "no rationale")

        if key not in GUARDRAILS:
            queued.append({**p, "reason": "unknown parameter"})
            continue

        # Guardrail check
        if not _within_guardrails(key, value):
            approvals.enqueue(
                kind="param_change",
                detail=f"Autopilot: {key} {_current_params().get(key, '?')} → {value} (OUTSIDE guardrails {GUARDRAILS[key]})",
                action=f"Set {key} = {value} — requires manual review",
                payload={"key": key, "value": value},
            )
            queued.append({**p, "reason": f"outside guardrails {GUARDRAILS[key]}"})
            continue

        # Backtest validation
        bt = _run_backtest_with_param(key, value)
        if bt is not None and bt.get("sortino", 0) <= 0:
            skipped.append({**p, "reason": f"backtest Sortino={bt['sortino']:.1f} ≤ 0"})
            continue

        # Auto-apply
        try:
            current = _current_params().get(key)
            runtime_config.set_param(key, float(value),
                                     f"autopilot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
            applied.append({
                **p,
                "old_value": current,
                "backtest": bt,
            })
            log.info("Autopilot: APPLIED %s %.2f → %.2f (%s)",
                     key, current, value, rationale)
        except Exception as e:
            skipped.append({**p, "reason": str(e)})

    # ── 4. Notify ──
    lines = ["🤖 *Autopilot 周报*"]
    if applied:
        lines.append("\n✅ *已自动执行:*")
        for a in applied:
            lines.append(
                f"  • {a['key']}: {a.get('old_value','?')} → {a['value']}"
            )
            if a.get("backtest"):
                bt = a["backtest"]
                lines.append(
                    f"    回测: ${bt.get('net_pnl',0):+.0f}, "
                    f"Sortino {bt.get('sortino',0):.1f}, "
                    f"DD {bt.get('max_dd',0):.1f}%"
                )
            lines.append(f"    理由: {a.get('rationale','')}")
    if queued:
        lines.append("\n📥 *待审批 (需你在 Telegram/GUI 点一下):*")
        for q in queued:
            lines.append(f"  • {q.get('key','?')} → {q.get('value')}: {q.get('reason','')}")
    if skipped:
        lines.append("\n⏭ *跳过 (回测恶化):*")
        for s in skipped:
            lines.append(f"  • {s.get('key','?')} → {s.get('value')}: {s.get('reason','')}")
    if not applied and not queued:
        lines.append("\n✓ 无变更 — 所有策略健康")

    msg = "\n".join(lines)
    notifier.send(msg)
    log.info("=== Autopilot cycle complete: %d applied, %d queued, %d skipped ===",
             len(applied), len(queued), len(skipped))

    return {
        "status": "complete",
        "n_proposals": len(proposals),
        "applied": len(applied),
        "queued": len(queued),
        "skipped": len(skipped),
        "applied_details": applied,
        "queued_details": queued,
    }


# ── Support functions (called by main.py cron) ───────────────────────────────

def check_and_rollback() -> list[str]:
    """Called from _weekly_self_review_job. Checks whether any auto-applied
    param change from a prior autopilot run has hurt live results (e.g.
    realized PnL worsened after the change). If so, reverts it. Returns
    Telegram messages (empty = no action needed)."""
    notes: list[str] = []
    try:
        from . import db
        state = db.get_state()
        hist = list(state.get("param_history", []))
        # Find recently auto-applied changes (last 7 days)
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for h in reversed(hist):
            if not h.get("active"):
                continue
            if h.get("source", "").startswith("autopilot_"):
                applied_at = h.get("applied_at", "")
                try:
                    at = datetime.fromisoformat(applied_at)
                except (ValueError, TypeError):
                    continue
                if at < cutoff:
                    continue
                # Check if there's evidence of harm (simplified: check if
                # any losses happened since). Full backtest is too heavy
                # for a pre-review quick check.
                rows = db.closed_trades(limit=200)
                losses_since = sum(
                    1 for r in rows
                    if r.get("pnl", 0) < 0
                    and (datetime.fromisoformat(r.get("ts", "")) >= at)
                )
                if losses_since >= 3:
                    runtime_config.revert_param(
                        h["key"],
                        f"auto-rollback: {losses_since} losses since autopilot change"
                    )
                    notes.append(
                        f"🔄 Autopilot 自动回滚: {h['key']} "
                        f"{h.get('new','?')} → {h.get('old','?')} "
                        f"({losses_since} losses since change)"
                    )
    except Exception as e:
        log.warning("autopilot rollback check failed: %s", e)
    return notes


def health_check() -> list[str]:
    """Called from _watchdog_job (17:30 ET weekdays). Checks for:
    - Silent scan stalls (no trade activity for 2+ market days)
    - Outdated backtest results (>14d old)
    - Overdue cron jobs
    - Active runtime overrides that have been in place >30d
    Returns list of issue strings (empty = healthy)."""
    issues: list[str] = []
    try:
        from . import db
        from datetime import datetime, timedelta, timezone

        # Check for active runtime overrides older than 30 days
        state = db.get_state()
        hist = list(state.get("param_history", []))
        now = datetime.now(timezone.utc)
        for h in hist:
            if h.get("active"):
                applied = h.get("applied_at", "")
                try:
                    at = datetime.fromisoformat(applied)
                except (ValueError, TypeError):
                    continue
                if (now - at) > timedelta(days=30):
                    issues.append(
                        f"⏳ runtime override '{h['key']}={h.get('new','?')}' "
                        f"active since {at.strftime('%Y-%m-%d')} — review if still valid"
                    )

        # Check overdue cron jobs
        try:
            last_sr = state.get("cron_self_review")
            if last_sr:
                from datetime import datetime as dt2
                try:
                    lr = dt2.fromisoformat(str(last_sr))
                    if (now - lr).days > 8:
                        issues.append(f"📋 self-review overdue (>8d)")
                except Exception:
                    pass
        except Exception:
            pass

    except Exception as e:
        log.warning("health_check failed: %s", e)
        issues.append(f"health_check error: {e}")

    return issues


# ── Cooldown tracker for post-rollback parameters ────────────────────────────
# When autopilot auto-rolls back a param change (because it caused losses),
# the key enters a 14-day cooldown. During cooldown, the optimizer MUST NOT
# re-propose the same parameter — prevents thrashing between two bad values.

_COOLDOWN_DAYS = 14


def in_cooldown(key: str) -> bool:
    """Return True if `key` was rolled back recently and is still cooling down.
    Called by optimizer_ai before re-proposing a param change."""
    try:
        state = db.get_state()
        hist = list(state.get("param_history", []))
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=_COOLDOWN_DAYS)
        for h in reversed(hist):
            if h.get("key") == key and h.get("rolled_back"):
                rb_time_str = h.get("applied_at", "")
                try:
                    rb_time = datetime.fromisoformat(rb_time_str)
                except (ValueError, TypeError):
                    continue
                if rb_time >= cutoff:
                    return True
    except Exception:
        pass
    return False
