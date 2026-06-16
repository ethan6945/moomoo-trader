"""Autopilot: self-maintenance layer (2026-06-11, owner mandate "全智能化").

Two jobs, both notify-always (铁律: no silent action):

1. check_and_rollback() — weekly. For every ACTIVE runtime param change in
   param_history: look at live STRATEGY trades closed since it was applied.
   If there is enough evidence (>= MIN_TRADES) and expectancy is clearly bad
   (avg R <= ROLLBACK_AVG_R), revert to the previous value and put the key in
   a cooldown so the optimizer can't immediately re-propose it. Evidence
   floors exist because short windows are sign-unstable (measured: the same
   config flipped +$28/day → −$8/day on a one-day window slide); we roll back
   on clear damage, not on noise.

2. health_check() — daily. Detects the failure modes that already bit us:
   silent scan stalls, garbage backtest results overwriting the GUI number,
   overdue cron jobs, reconcile drift, forgotten runtime overrides.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import db, runtime_config
from .config import settings

log = logging.getLogger(__name__)

# Rollback evidence floors (see module docstring for why they are not lower).
MIN_TRADES = 15            # need at least this many closed strategy trades
ROLLBACK_AVG_R = -0.30     # ... averaging at/below this R-multiple
COOLDOWN_DAYS = 28         # re-proposal lockout after a rollback

_STRATEGY_NAMES = ("trend", "momentum_break")


def _strategy_trades_since(ts_iso: str) -> list[dict]:
    rows = db.closed_trades(500)
    out = []
    for r in rows:
        if r.get("strategy") not in _STRATEGY_NAMES:
            continue
        if r.get("exit_reason") == "MANUAL":
            continue
        if str(r.get("ts", "")) >= ts_iso:
            out.append(r)
    return out


def param_cooldowns() -> dict:
    try:
        return db.get_state().get("param_cooldowns", {}) or {}
    except Exception:
        return {}


def in_cooldown(key: str) -> bool:
    until = param_cooldowns().get(key)
    if not until:
        return False
    try:
        return datetime.now(timezone.utc).isoformat() < str(until)
    except Exception:
        return False


def check_and_rollback() -> list[str]:
    """Returns human-readable lines describing any rollbacks performed."""
    notes: list[str] = []
    try:
        hist = db.get_state().get("param_history", []) or []
    except Exception as e:
        log.warning("autopilot: param_history unreadable: %s", e)
        return notes
    for h in hist:
        if not h.get("active") or h.get("source") == "owner-approved":
            # Owner choices are never auto-reverted — only optimizer changes.
            continue
        trades = _strategy_trades_since(str(h.get("applied_at", "")))
        if len(trades) < MIN_TRADES:
            continue
        avg_r = sum(float(t.get("r_multiple") or 0) for t in trades) / len(trades)
        if avg_r <= ROLLBACK_AVG_R:
            key = h["key"]
            rec = runtime_config.revert_param(
                key, reason=f"live avg R {avg_r:.2f} over {len(trades)} trades")
            if rec:
                until = (datetime.now(timezone.utc)
                         + timedelta(days=COOLDOWN_DAYS)).isoformat()
                cds = param_cooldowns()
                cds[key] = until
                db.update_state({"param_cooldowns": cds})
                notes.append(
                    f"⏪ 自动回滚: {key} {rec['new']} → {rec['old']} — 应用后实盘 "
                    f"{len(trades)} 笔平均 R={avg_r:.2f} (阈值 {ROLLBACK_AVG_R})。"
                    f"该参数 {COOLDOWN_DAYS} 天内不再自动调整。")
                log.warning("autopilot rollback: %s", notes[-1])
    return notes


# ── daily health watchdog ────────────────────────────────────────────────────

def _age_minutes(path) -> float | None:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - mtime).total_seconds() / 60
    except Exception:
        return None


def health_check() -> list[str]:
    """Returns a list of issue strings; empty list = all healthy."""
    issues: list[str] = []
    root = settings.root

    # 1. Scan liveness: account snapshot should refresh every scan while the
    # scheduler is up. Stale > 24h (covers weekends/holidays loosely; the
    # point is catching the silent multi-day stall, not minute precision).
    age = _age_minutes(root / "data" / "account.json")
    if age is None:
        issues.append("account.json 缺失 — scheduler 可能从未跑过")
    elif age > 24 * 60:
        issues.append(f"上次扫描快照已 {age/60:.0f} 小时前 — scheduler/OpenD 可能挂了")

    # 2. Backtest results sanity: 0-ticker garbage or stale health check.
    try:
        bt = json.loads((root / "data" / "backtest_results.json").read_text())
        if not (bt.get("config", {}).get("tickers") or []) and \
                not bt.get("metrics", {}).get("total_trades"):
            issues.append("backtest_results.json 是 0-ticker 垃圾结果 — 周回测在断网时跑过")
    except FileNotFoundError:
        pass
    except Exception as e:
        issues.append(f"backtest_results.json 不可读: {e}")
    age = _age_minutes(root / "data" / "backtest_results.json")
    if age is not None and age > 9 * 24 * 60:
        issues.append(f"周回测健康检查已 {age/1440:.0f} 天未更新")

    # 3. Overdue scheduled jobs (cron_state ages vs expected cadence).
    try:
        from . import cron_state
        state = cron_state._load()
        cadence_days = {"daily_blacklist": 4, "weekly_backtest": 9,
                        "self_review": 9, "universe_refresh": 9,
                        "monthly_optuna": 35}
        now = datetime.now(timezone.utc)
        for job, max_d in cadence_days.items():
            lr = state.get(job)
            if not lr:
                continue
            try:
                last = datetime.fromisoformat(str(lr))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last).days > max_d:
                    issues.append(f"定时任务 {job} 已 {(now - last).days} 天未成功运行")
            except ValueError:
                continue
    except Exception as e:
        log.debug("watchdog cron check failed: %s", e)

    # 4. Reconcile drift left unresolved.
    try:
        rec = json.loads((root / "data" / "reconcile.json").read_text())
        n_drift = sum(len(rec.get(k, []) or []) for k in ("orphans", "ghosts", "mismatches"))
        if n_drift:
            issues.append(f"reconcile 报告 {n_drift} 项账实偏差未消化")
    except Exception:
        pass

    # 5. Active runtime overrides — a reminder, not an alarm (these are easy
    # to forget and they make .env lie about what the bot actually runs).
    try:
        hist = db.get_state().get("param_history", []) or []
        active = [h for h in hist if h.get("active")]
        if active:
            ov = ", ".join(f"{h['key']}={h['new']}({h['source']})" for h in active)
            issues.append(f"运行时参数覆盖生效中: {ov}")
    except Exception:
        pass

    return issues
