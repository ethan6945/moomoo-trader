"""
AI dynamic param correction — DeepSeek reads news → adjusts trading params in real-time.

Mechanism:
  1. DeepSeek analyzes market news + regime state + recent PnL
  2. Proposes param adjustments within ALLOWED_PARAMS bounds
  3. Owner approves via Telegram (or bounded-autonomy auto-apply if within 10% leeway)
  4. runtime_config.set_param() writes to db state
  5. Next scan uses the new value

Usage:
  from src.ai_dynamic_params import analyze_and_adjust
  result = analyze_and_adjust(regime_label="BULL", vix=18.5, recent_pnl=-150.0)

LIVE-ONLY — this module calls DeepSeek API and reads db. Never used in sandbox.
"""
from __future__ import annotations

import json, logging, os
from datetime import datetime, timezone
from pathlib import Path

from . import db, runtime_config
from .config import settings

log = logging.getLogger(__name__)

# Leeway: % change the optimizer is allowed to apply WITHOUT owner approval.
# Beyond this, a Telegram approval is required.
BOUNDED_AUTONOMY_LEEWAY = 0.10  # 10%

# Cache: don't re-analyze more than once per 4 hours
_CACHE_FILE = settings.root / "data" / "ai_param_cache.json"
_CACHE_TTL_HOURS = 4


def _load_cache() -> dict:
    if not _CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_CACHE_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def _save_cache(data: dict) -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(data, indent=2))


def _cache_valid(cache: dict) -> bool:
    last_ts = cache.get("last_analysis_at", "")
    if not last_ts:
        return False
    try:
        last = datetime.fromisoformat(last_ts)
        return (datetime.now(timezone.utc) - last).total_seconds() < _CACHE_TTL_HOURS * 3600
    except ValueError:
        return False


def _build_prompt(regime_label: str, vix: float, recent_pnl: float,
                  n_trades: int, win_rate: float, current_params: dict) -> str:
    """Build the analysis prompt for DeepSeek."""
    return (
        "You are a quantitative trading parameter optimizer. "
        "Given the current market state and recent trading performance, "
        "suggest adjustments to the live trading parameters.\n\n"
        f"Market: regime={regime_label}, VIX proxy={vix:.1f}\n"
        f"Recent: PnL=${recent_pnl:.0f}, {n_trades} trades, win_rate={win_rate:.0f}%\n"
        f"Current params: {json.dumps(current_params)}\n"
        f"Allowed ranges: {json.dumps(runtime_config.ALLOWED_PARAMS)}\n\n"
        "Rules:\n"
        "1. If regime=BULL and win_rate > 60%: slightly increase risk (lower entry_threshold, higher tp)\n"
        "2. If regime=BEAR or losses > $200/5trades: tighten (raise threshold, lower tp, tighten sl)\n"
        "3. If recent_pnl positive and win_rate > 50%: stay\n"
        "4. Never suggest changes outside ALLOWED_PARAMS bounds\n"
        "5. Max change per call: ±2 on threshold, ±1 on tp/sl multiplier\n\n"
        "Reply with ONLY a JSON object in this exact format:\n"
        '{"changes": {"entry_threshold": 72, "tp_atr_mult": 9.5}, "reasoning": "brief explanation"}'
    )


def _call_deepseek(prompt: str) -> dict:
    """Call DeepSeek API and parse the response."""
    import openai  # lazy import — only installed on server

    api_key = settings.deepseek_api_key if hasattr(settings, 'deepseek_api_key') else os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        log.warning("DeepSeek API key not configured — skipping AI analysis")
        return {"changes": {}, "reasoning": "no API key"}

    try:
        client = openai.OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com/v1",
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        content = resp.choices[0].message.content.strip()
        # Strip markdown if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content.strip())
    except Exception as e:
        log.warning("DeepSeek API call failed: %s", e)
        return {"changes": {}, "reasoning": f"API error: {e}"}


def _apply_changes(changes: dict, reasoning: str) -> list[dict]:
    """Apply AI-suggested changes via runtime_config. Returns list of applied changes."""
    applied = []
    cache = _load_cache()

    for key, new_val in changes.items():
        try:
            new_val = float(new_val)
        except (TypeError, ValueError):
            continue

        if not runtime_config.is_valid(key, new_val):
            log.warning("AI suggested out-of-bounds: %s=%s — skipping", key, new_val)
            continue

        old_val = runtime_config.current(key)
        if old_val == new_val:
            continue

        pct_change = abs(new_val - old_val) / max(abs(old_val), 1)

        if pct_change <= BOUNDED_AUTONOMY_LEEWAY:
            # Auto-apply within leeway
            try:
                rec = runtime_config.set_param(key, new_val, "deepseek_ai_auto")
                applied.append({"key": key, "old": old_val, "new": new_val,
                                "pct": round(pct_change * 100, 1), "auto_applied": True})
                log.info("AI auto-applied: %s %.1f → %.1f (%s)", key, old_val, new_val, reasoning)
            except ValueError as e:
                log.warning("AI auto-apply failed: %s — %s", key, e)
        else:
            # Requires approval — queue it (future: Telegram approval integration)
            log.info("AI proposed (needs approval): %s %.1f → %.1f (%.0f%% change) — %s",
                     key, old_val, new_val, pct_change * 100, reasoning)
            applied.append({"key": key, "old": old_val, "new": new_val,
                            "pct": round(pct_change * 100, 1), "auto_applied": False,
                            "needs_approval": True})

    return applied


def analyze_and_adjust(
    regime_label: str = "NEUTRAL",
    vix: float = 15.0,
    recent_pnl: float = 0.0,
    n_trades: int = 0,
    win_rate: float = 50.0,
    force: bool = False,
) -> dict:
    """Main entry point. Analyze market + recent PnL → propose param adjustments.

    Returns {"applied": [...], "proposed": [...], "reasoning": "..."}
    """
    cache = _load_cache()

    if not force and _cache_valid(cache):
        log.debug("AI param cache hit — skipping analysis")
        return cache.get("last_result", {"applied": [], "reasoning": "cached"})

    current_params = {
        "entry_threshold": runtime_config.entry_threshold(),
        "tp_atr_mult": runtime_config.tp_atr_mult(),
        "sl_atr_mult": runtime_config.sl_atr_mult(),
    }

    prompt = _build_prompt(regime_label, vix, recent_pnl, n_trades, win_rate, current_params)
    result = _call_deepseek(prompt)
    changes = result.get("changes", {})
    reasoning = result.get("reasoning", "no reasoning provided")

    applied = _apply_changes(changes, reasoning)

    output = {
        "last_analysis_at": datetime.now(timezone.utc).isoformat(),
        "market_snapshot": {
            "regime": regime_label,
            "vix": vix,
            "recent_pnl": recent_pnl,
            "n_trades": n_trades,
            "win_rate": win_rate,
        },
        "applied": applied,
        "reasoning": reasoning,
    }

    cache["last_analysis_at"] = output["last_analysis_at"]
    cache["last_result"] = output
    _save_cache(cache)

    return output


# ── Cron-friendly entry point ──────────────────────────

def run_daily_ai_check() -> dict:
    """Called by cron. Reads db for latest performance, runs analysis."""
    try:
        rows = db.closed_trades(limit=50)
        if rows:
            recent_pnl = sum(float(r.get("pnl", 0)) for r in rows if r.get("pnl"))
            n_trades = len(rows)
            wins = sum(1 for r in rows if float(r.get("pnl", 0)) > 0)
            win_rate = (wins / n_trades * 100) if n_trades else 50.0
        else:
            recent_pnl, n_trades, win_rate = 0.0, 0, 50.0
    except Exception:
        recent_pnl, n_trades, win_rate = 0.0, 0, 50.0

    # Determine current regime
    try:
        from .regime import assess as regime_assess
        from .moomoo_client import client
        from moomoo import KLType
        # P1 fix 2026-07-07: `client` is a @contextmanager FACTORY, not an
        # instance — the old `client.get_kline(...)` raised AttributeError on
        # every call, was swallowed by this except, and the AI never saw the
        # real regime (always "NEUTRAL").
        with client() as c:
            spy = c.get_kline("SPY", bars=250, ktype=KLType.K_DAY)
        if spy is not None and len(spy) >= 200:
            reg = regime_assess(spy)
            regime_label = reg.confirmed if reg.confirmed else reg.label
        else:
            regime_label = "NEUTRAL"
    except Exception as e:
        log.warning("regime fetch failed (falling back to NEUTRAL): %s", e)
        regime_label = "NEUTRAL"

    return analyze_and_adjust(
        regime_label=regime_label,
        vix=15.0,
        recent_pnl=recent_pnl,
        n_trades=n_trades,
        win_rate=win_rate,
        force=True,
    )
