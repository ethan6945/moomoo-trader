#!/usr/bin/env python3
"""
Hermes Autonomous Trading Loop — core engine.
Reads bot state, runs backtest, applies validated improvements.

Called by the Hermes cron job. All decision-making happens in the AI agent
(the cron prompt); this module is the read/validate/apply toolkit.

Usage:
  python3 src/hermes_improve.py diagnose   → JSON dump of current state
  python3 src/hermes_improve.py backtest    → run backtest with current params
  python3 src/hermes_improve.py apply KEY=VALUE [...] → apply param changes
  python3 src/hermes_improve.py rollback    → revert last week's changes
"""

import json, os, subprocess, sys, shutil
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CHANGELOG = DATA / "hermes_changelog.jsonl"
SNAPSHOT_DIR = DATA / "hermes_snapshots"
BACKTEST_OUT = DATA / "hermes_backtest_result.json"

# Use project venv Python for backtest (not Hermes venv — numpy mismatch)
_VENV_PYTHON = ROOT / ".venv" / "bin" / "python3"
_PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _cmd(args: list, cwd=ROOT, timeout=300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=str(cwd), timeout=timeout)


# ── DIAGNOSE ──────────────────────────────────────────────

def diagnose() -> dict:
    """Collect full bot state for AI analysis. Returns JSON-serializable dict."""
    account = _load_json(DATA / "account.json")
    mae = _load_json(DATA / "mae_mfe_diagnostic.json")
    backtest = _load_json(DATA / "backtest_results.json")

    # Read .env non-secret params
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                # Skip secrets
                if any(s in k.upper() for s in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
                    continue
                env[k.strip()] = v.split("#")[0].strip()

    # Recent closed trades
    db_path = DATA / "trader.db"
    trades = []
    if db_path.exists():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT symbol, pnl, pnl_pct, r_multiple, exit_reason, strategy, ts "
            "FROM closed_trades ORDER BY id DESC LIMIT 30"
        ).fetchall()
        conn.close()
        trades = [dict(zip(["symbol","pnl","pnl_pct","r_multiple","exit_reason","strategy","ts"], r)) for r in rows]

    # Self-review latest
    sr_dir = DATA / "self_review"
    latest_sr = None
    if sr_dir.exists():
        srs = sorted(sr_dir.glob("*.json"), reverse=True)
        if srs:
            latest_sr = _load_json(srs[0])

    return {
        "ts": datetime.now().isoformat(),
        "account": account,
        "env": env,
        "recent_trades": trades[:20],
        "mae_mfe": mae,
        "backtest_metrics": backtest.get("metrics", {}) if backtest else {},
        "latest_self_review": latest_sr,
        "posture": _analyze_posture(trades[:20], backtest.get("metrics", {}) if backtest else {}),
    }


# ── POSTURE — conservative vs aggressive ──────────────────

def _analyze_posture(trades: list, backtest_metrics: dict) -> dict:
    """Determine if the bot should tighten (too many losers) or loosen (earning too little).

    Returns a posture recommendation with concrete parameter directions.
    """
    if not trades:
        return {"mode": "unknown", "reason": "no trades yet", "directions": []}

    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / n * 100
    avg_r = sum(t["r_multiple"] for t in trades) / n
    total_pnl = sum(t["pnl"] for t in trades)

    # Backtest reference
    bt_wr = backtest_metrics.get("win_rate_pct", 45)
    bt_pf = backtest_metrics.get("profit_factor", 1.5)

    directions = []

    # ── CONSERVATIVE triggers ──
    if wr < 35:
        directions.append({
            "direction": "tighten",
            "reason": f"win rate {wr:.0f}% well below backtest {bt_wr:.0f}%",
            "actions": [
                "raise ENTRY_SCORE_THRESHOLD (+5 to +10) = fewer but higher-quality signals",
                "reduce MAX_POSITIONS (-1) = force concentration on best ideas",
                "tighten MAX_HOLD_DAYS (-1 to -2) = cut losers faster",
            ],
        })
    if avg_r < -0.3:
        directions.append({
            "direction": "tighten",
            "reason": f"avg R {avg_r:.2f} is negative — losing more per trade than winning",
            "actions": [
                "tighten SL_ATR_MULT (reduce by 0.3-0.5) = smaller losses per trade",
                "raise ENTRY_SCORE_THRESHOLD (+5) = higher conviction entries",
            ],
        })

    # ── AGGRESSIVE triggers ──
    if wr > 50 and total_pnl > 0 and avg_r > 0.3:
        directions.append({
            "direction": "loosen",
            "reason": f"win rate {wr:.0f}% strong, avg R {avg_r:.2f} positive — earning but could earn MORE",
            "actions": [
                "widen TP_ATR_MULT (+2 to +4) = let winners run further",
                "lower ENTRY_SCORE_THRESHOLD (-5) = capture more signals while edge is hot",
                "increase RISK_PER_TRADE (+1-2pp) = scale up proven edge",
                "increase MAX_POSITIONS (+1) = more concurrent bets",
            ],
        })
    if avg_r > 0.5 and wr > 40:
        directions.append({
            "direction": "loosen_size",
            "reason": f"strong expectancy ({avg_r:.2f}R) with decent win rate — sizing too small",
            "actions": [
                "increase RISK_PER_TRADE by 1-3 percentage points",
                "widen TP_ATR_MULT to capture higher R-multiple exits",
            ],
        })

    # ── NEUTRAL / MIXED ──
    mode = "neutral"
    if directions:
        tight = [d for d in directions if d["direction"].startswith("tighten")]
        loose = [d for d in directions if d["direction"].startswith("loosen")]
        if tight and not loose:
            mode = "conservative"
        elif loose and not tight:
            mode = "aggressive"
        else:
            mode = "mixed"

    return {
        "mode": mode,
        "summary": f"{n} trades: WR={wr:.0f}%, avg_R={avg_r:.2f}, PnL=${total_pnl:.0f}",
        "backtest_ref": f"WR={bt_wr:.0f}%, PF={bt_pf:.2f}",
        "directions": directions,
    }


# ── BACKTEST ──────────────────────────────────────────────

def run_backtest(ticker_count: int = 15) -> dict:
    """Run backtest_v3 with current .env params. Returns metrics dict."""
    # backtest_v3 uses relative imports — run via module import
    # Read live settings to inject into backtest config
    sys.path.insert(0, str(ROOT))
    from src.config import settings as _bt_settings

    code = (
        "import sys, json; sys.path.insert(0, '" + str(ROOT) + "'); "
        "from src.config import settings; "
        "from src.backtest import BacktestConfig, run_backtest; "
        "cfg = BacktestConfig("
        "days=180, threshold=settings.entry_threshold, "
        "account_usd=settings.account_usd, "
        "risk_per_trade=settings.risk_per_trade, "
        "sl_atr_mult=settings.sl_atr_mult, "
        "tp_atr_mult=settings.tp_atr_mult, "
        "max_hold_days=settings.max_hold_days, "
        "max_position_pct=settings.max_position_pct, "
        "apply_momentum_strategy=True, "
        "apply_mr_strategy=" + str(_bt_settings.mr_enabled) + ", "
        "use_breakeven_stop=" + str(_bt_settings.use_breakeven_stop) + ", "
        "breakeven_trigger_r=" + str(_bt_settings.breakeven_trigger_r) + ", "
        "use_scale_out=" + str(_bt_settings.use_scale_out) + ", "
        "tp1_r=" + str(_bt_settings.tp1_r) + ", tp2_r=" + str(_bt_settings.tp2_r) + ", "
        "max_gap_pct=" + str(_bt_settings.max_gap_pct) + ", "
        "sl_cooldown_hours=6, "
        "apply_max_positions=True, "
        "apply_dd_breaker=True, "
        "realistic_limit_fills=True"
        "); "
        "result = run_backtest(cfg); "
        "print(json.dumps(result.get('metrics', result), default=str))"
    )
    rc = _cmd([_PYTHON, "-c", code], timeout=600)
    if rc.returncode != 0:
        return {"error": rc.stderr[:500], "stdout": rc.stdout[:500]}
    try:
        return json.loads(rc.stdout.strip().split("\n")[-1])
    except json.JSONDecodeError:
        return {"error": "parse failed", "stdout": rc.stdout[:500]}


def compare_backtests(before: dict, after: dict) -> dict:
    """Compare two backtest metric dicts, return delta."""
    delta = {}
    for k in ["net_pnl_usd", "win_rate_pct", "profit_factor", "sortino_ratio",
              "max_drawdown_pct", "total_return_pct", "expectancy_per_trade_usd"]:
        b = before.get(k, 0) or 0
        a = after.get(k, 0) or 0
        delta[k] = {"before": b, "after": a, "change_pct": ((a - b) / abs(b) * 100) if b != 0 else 0}
    return delta


# ── APPLY ─────────────────────────────────────────────────

def snapshot_before() -> Path:
    """Save current .env and account.json for rollback."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = SNAPSHOT_DIR / ts
    snap.mkdir()
    env_file = ROOT / ".env"
    if env_file.exists():
        shutil.copy(env_file, snap / ".env")
    account_file = DATA / "account.json"
    if account_file.exists():
        shutil.copy(account_file, snap / "account.json")
    return snap


def apply_params(changes: dict, reason: str, pnl_estimate: str) -> dict:
    """Apply parameter changes to .env. Returns applied changes."""
    snapshot = snapshot_before()
    env_file = ROOT / ".env"
    lines = env_file.read_text().splitlines()
    new_lines = []
    applied = {}

    for line in lines:
        stripped = line.strip()
        applied_this_line = False
        for key, new_val in changes.items():
            if stripped.startswith(f"{key}=") and not stripped.lstrip().startswith("#"):
                # Preserve comment if present
                comment = ""
                if "#" in line:
                    comment = " " + line[line.index("#"):]
                new_lines.append(f"{key}={new_val}{comment}")
                applied[key] = new_val
                applied_this_line = True
                break
        if not applied_this_line:
            new_lines.append(line)

    env_file.write_text("\n".join(new_lines) + "\n")

    # Log to changelog
    entry = {
        "ts": datetime.now().isoformat(),
        "snapshot": str(snapshot),
        "changes": applied,
        "reason": reason,
        "pnl_estimate": pnl_estimate,
    }
    with open(CHANGELOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return {"applied": applied, "snapshot": str(snapshot), "changelog_entry": entry}


def rollback() -> dict:
    """Revert to most recent snapshot."""
    snaps = sorted(SNAPSHOT_DIR.glob("*"), reverse=True)
    if not snaps:
        return {"error": "no snapshots found"}
    latest = snaps[0]
    for f in latest.iterdir():
        if f.name == ".env":
            shutil.copy(f, ROOT / ".env")
        elif f.name == "account.json":
            shutil.copy(f, DATA / "account.json")
    return {"rolled_back_to": str(latest)}


# ── MAIN ──────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: hermes_improve.py <diagnose|backtest|apply|rollback> [args]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "diagnose":
        print(json.dumps(diagnose(), indent=2, default=str))

    elif cmd == "backtest":
        baseline = run_backtest()
        print(json.dumps(baseline, indent=2, default=str))

    elif cmd == "apply":
        changes = {}
        for arg in sys.argv[2:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                changes[k] = v
        if not changes:
            print(json.dumps({"error": "no KEY=VALUE pairs provided"}))
            sys.exit(1)
        result = apply_params(changes, reason="auto", pnl_estimate="TBD")
        print(json.dumps(result, indent=2, default=str))

    elif cmd == "rollback":
        result = rollback()
        print(json.dumps(result, indent=2, default=str))
