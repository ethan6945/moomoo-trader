"""Web dashboard backend (Flask) — monitor + control the bot from a browser.

Runs as its OWN background process (start-web.command). The trading scheduler
(`python -m src.main run`) is a SEPARATE background process — closing the browser
does NOT stop trading; this server just reads the bot's state (data/account.json
+ SQLite) and sends control actions (start/stop, approvals, budget).

Endpoints (JSON):
  GET  /                     → dashboard page
  GET  /api/status           → account snapshot + scheduler running flag
  GET  /api/approvals        → approval queue (pending first)
  POST /api/approvals/<id>/<approve|reject>
  GET  /api/closed?n=        → recent closed trades (History + Equity)
  GET  /api/log?n=           → tail of logs/trader.log (compact activity)
  GET  /api/signal-log?n=    → tail of logs/signal_reporter.log
  POST /api/budget           → {value} set runtime budget (no restart)
  POST /api/scheduler/<start|stop>
  POST /api/signal-run       → fire the signal reporter once (background)
  POST /api/backtest         → run a 180d honest backtest (background thread)
  GET  /api/backtest         → last backtest result/status
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, make_response, redirect, request, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import approvals, db, risk_manager  # noqa: E402
from src.config import settings  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
# Override only for running an isolated/secondary instance (e.g. tests). Default = repo .env.
ENV_FILE = Path(os.getenv("WEB_ENV_FILE") or (ROOT / ".env"))
ACCOUNT_FILE = ROOT / "data" / "account.json"
TRADER_LOG = ROOT / "logs" / "trader.log"
SIGNAL_LOG = ROOT / "logs" / "signal_reporter.log"
SCHED_PID = ROOT / "logs" / "scheduler.pid"
VENV_PY = ROOT / ".venv" / "bin" / "python"

app = Flask(__name__, static_folder=None)


@app.after_request
def _no_cache(resp):
    # Always serve the freshest dashboard (no stale cached HTML/JS in the browser).
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ── auth: optional password gate (needed when exposed beyond localhost) ────────
# Zero new deps. A signed cookie = HMAC(secret, password). If WEB_PASSWORD is not
# set the whole gate is OFF (localhost-only dev convenience). Changing the password
# instantly invalidates every old cookie. The server REFUSES to bind to a non-local
# host without a password (see main()).
AUTH_COOKIE = "wt_auth"
_PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico"}
_pw_cache = {"mtime": -1.0, "v": ""}
_secret_cache = {"v": ""}


def _web_password() -> str:
    """Configured access password (re-read from .env when the file changes, so
    setting it in the panel takes effect without a restart). Empty = no auth."""
    try:
        m = ENV_FILE.stat().st_mtime
    except Exception:
        m = 0.0
    if m != _pw_cache["mtime"]:
        _pw_cache["mtime"] = m
        _pw_cache["v"] = (_read_env().get("WEB_PASSWORD", "") or os.getenv("WEB_PASSWORD", "")).strip()
    return _pw_cache["v"]


def _web_secret() -> str:
    """Stable per-install signing secret. Generated once and persisted to .env so
    cookies survive restarts."""
    if _secret_cache["v"]:
        return _secret_cache["v"]
    s = (_read_env().get("WEB_SECRET", "") or os.getenv("WEB_SECRET", "")).strip()
    if not s:
        s = _secrets.token_hex(32)
        try:
            _write_env_key("WEB_SECRET", s)
        except Exception:
            pass
    _secret_cache["v"] = s
    os.environ["WEB_SECRET"] = s
    return s


def _auth_token(pw: str) -> str:
    return hmac.new(_web_secret().encode(), pw.encode(), hashlib.sha256).hexdigest()


def _authed() -> bool:
    pw = _web_password()
    if not pw:
        return True   # auth disabled
    return hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _auth_token(pw))


@app.before_request
def _require_auth():
    if not _web_password():
        return None                       # gate off
    if request.path in _PUBLIC_PATHS or _authed():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "auth required"}), 401
    return redirect("/login")


@app.route("/login")
def login_page():
    return send_from_directory(STATIC, "login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    pw_cfg = _web_password()
    if not pw_cfg:
        return jsonify({"ok": True})      # no auth configured
    pw = (request.json or {}).get("password", "")
    if isinstance(pw, str) and hmac.compare_digest(pw, pw_cfg):
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie(AUTH_COOKIE, _auth_token(pw_cfg),
                        max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax")
        return resp
    return jsonify({"ok": False, "error": "wrong password"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(AUTH_COOKIE)
    return resp


# ── helpers ──────────────────────────────────────────────────────────────────

def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _tail(p: Path, n: int) -> list[str]:
    try:
        return p.read_text(errors="replace").splitlines()[-n:]
    except Exception:
        return []


def _scheduler_running() -> bool:
    try:
        pid = int(SCHED_PID.read_text().strip())
        os.kill(pid, 0)   # signal 0 = liveness check
        return True
    except Exception:
        return False


def _opend_status(acct: dict, sched_running: bool) -> tuple[str, str]:
    """3-state OpenD light: red=not started, yellow=connected-but-not-unlocked,
    green=unlocked & trading. Inferred WITHOUT opening a second broker connection
    (which wouldn't share the scheduler's unlock). Green = OpenD socket up AND the
    scheduler recently wrote a fresh snapshot with cash (a successful accinfo query
    requires an unlocked trade context)."""
    try:
        with socket.create_connection((settings.moomoo_host, settings.moomoo_port), timeout=0.6):
            reachable = True
    except Exception:
        reachable = False
    if not reachable:
        return "red", "OpenD 未启动"
    interval = (acct.get("scan_interval_min") or 30) * 60
    try:
        fresh = (time.time() - ACCOUNT_FILE.stat().st_mtime) < (interval * 2 + 300)
    except Exception:
        fresh = False
    if sched_running and fresh and acct.get("cash"):
        return "green", "已解锁 · 可交易"
    return "yellow", "已连接 · 未解锁/未确认"


def _trade_summary() -> dict:
    """Gross won / gross lost / net / count + current consecutive streak.
    Respects a 'stats_reset_at' baseline (counts only trades closed after it)."""
    rows = db.closed_trades(limit=10_000)
    reset_at = db.get_state().get("stats_reset_at")
    if reset_at:
        rows = [r for r in rows if (r.get("ts") or "") >= reset_at]
    wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
    won = sum(r["pnl"] for r in rows if (r.get("pnl") or 0) > 0)
    lost = sum(r["pnl"] for r in rows if (r.get("pnl") or 0) < 0)
    n = len(rows)
    streak, kind = 0, None
    for r in reversed(rows):
        s = 1 if (r.get("pnl") or 0) > 0 else (-1 if (r.get("pnl") or 0) < 0 else 0)
        if s == 0:
            continue
        if kind is None:
            kind, streak = s, 1
        elif s == kind:
            streak += 1
        else:
            break
    return {"count": n, "wins": wins,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "gross_won": round(won, 2), "gross_lost": round(lost, 2),
            "net": round(won + lost, 2),
            "streak": streak,
            "streak_kind": "win" if kind == 1 else ("loss" if kind == -1 else "none")}


# ── pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/static/<path:fn>")
def static_files(fn):
    return send_from_directory(STATIC, fn)


# ── read API ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    acct = _read_json(ACCOUNT_FILE, {})
    sched = _scheduler_running()
    acct["scheduler_running"] = sched
    acct["opend_status"], acct["opend_label"] = _opend_status(acct, sched)
    try:
        acct["budget"] = risk_manager.budget_usd()   # live override beats stale snapshot
    except Exception:
        pass
    try:
        acct["summary"] = _trade_summary()
    except Exception:
        acct["summary"] = {}
    # Live realized today/total from db state (reflects a stats reset instantly,
    # not waiting for the next snapshot write).
    try:
        st = db.get_state()
        acct["realized_pnl_today"] = float(st.get("realized_pnl_today") or 0)
        acct["realized_pnl_total"] = float(st.get("realized_pnl_total") or 0)
    except Exception:
        pass
    return jsonify(acct)


@app.route("/api/approvals")
def api_approvals():
    items = approvals.list_all()
    items.sort(key=lambda a: (a.get("status") != "pending", a.get("created_at", "")))
    return jsonify(items)


@app.route("/api/closed")
def api_closed():
    n = int(request.args.get("n", 100))
    rows = db.closed_trades(limit=10_000)
    return jsonify(rows[-n:])


@app.route("/api/log")
def api_log():
    n = int(request.args.get("n", 40))
    return jsonify(_tail(TRADER_LOG, n))


@app.route("/api/signal-log")
def api_signal_log():
    n = int(request.args.get("n", 120))
    return jsonify(_tail(SIGNAL_LOG, n))


# ── action API ───────────────────────────────────────────────────────────────

@app.route("/api/approvals/<item_id>/<action>", methods=["POST"])
def api_resolve(item_id, action):
    if action not in ("approve", "reject"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    ok = approvals.resolve(item_id, approved=(action == "approve"))
    if ok and action == "approve":
        try:
            approvals.apply_approved()
        except Exception:
            pass
    return jsonify({"ok": ok})


@app.route("/api/reset-stats", methods=["POST"])
def api_reset_stats():
    """Reset the cumulative trade stats: set a baseline so the Trade Record
    counts only trades from now on, and zero the bot's realized PnL totals +
    DD peak. Closed-trade HISTORY is preserved (History tab still shows it)."""
    from src import clock
    now = clock.ny_now().isoformat()
    db.update_state({
        "stats_reset_at": now,
        "realized_pnl_total": 0.0,
        "realized_pnl_today": 0.0,
        "loss_streak_days": 0,
        "peak_equity": risk_manager.budget_usd(),
    })
    return jsonify({"ok": True, "reset_at": now})


# ── settings: .env key management ─────────────────────────────────────────────
SETTING_KEYS = {
    "WEB_PASSWORD": "网页访问密码 — 设了之后，从手机/局域网打开面板要先登录(本机也是)。空=不需要密码(仅本机可用)。这是开放手机访问的前提。",
    "DEEPSEEK_API_KEY": "DeepSeek 优化器 Key — 填了之后每周自动产参数建议(回测验证通过才进审批队列)。空=优化器不调用 LLM。",
    "GEMINI_API_KEYS": "Gemini AI Key(逗号分隔多个)— 盘前/盘中信号卡的新闻+宏观分析。",
    "TAVILY_API_KEY": "Tavily 新闻搜索 Key — 给 AI 提供实时新闻上下文。",
    "TELEGRAM_TOKEN": "Telegram Bot Token — 推送交易通知 + 审批卡片。",
    "TELEGRAM_CHAT_ID": "Telegram Chat ID — 接收通知的聊天 ID。",
}


def _read_env() -> dict:
    out = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.split("  #")[0].strip()
    except Exception:
        pass
    return out


def _mask(v: str) -> str:
    if not v:
        return ""
    return ("•" * max(0, min(len(v) - 4, 12))) + v[-4:] if len(v) > 4 else "set"


def _write_env_key(key: str, value: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    for i, l in enumerate(lines):
        st = l.strip()
        if st.startswith(key + "=") or st.startswith("#" + key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


@app.route("/api/settings")
def api_settings():
    env = _read_env()
    keys = [{"key": k, "desc": d, "masked": _mask(env.get(k, "")), "set": bool(env.get(k))}
            for k, d in SETTING_KEYS.items()]
    return jsonify({"keys": keys})


@app.route("/api/settings/key", methods=["POST"])
def api_set_key():
    body = request.json or {}
    k, v = body.get("key"), body.get("value", "")
    if k not in SETTING_KEYS:
        return jsonify({"ok": False, "error": "unknown key"}), 400
    try:
        _write_env_key(k, v.strip())
        if k == "WEB_PASSWORD":
            note = ("访问密码已更新 — 下次打开面板需要登录(本机也是)。"
                    if v.strip() else "已清除访问密码 — 面板将不再需要登录。")
        else:
            note = "已写入 .env — 重启 bot 后生效"
        return jsonify({"ok": True, "note": note})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/budget", methods=["POST"])
def api_budget():
    try:
        val = float(request.json.get("value"))
        assert val > 0
    except Exception:
        return jsonify({"ok": False, "error": "bad value"}), 400
    db.update_state({"budget_usd": val})
    return jsonify({"ok": True, "budget": val})


@app.route("/api/scheduler/<action>", methods=["POST"])
def api_scheduler(action):
    if action == "start":
        if _scheduler_running():
            return jsonify({"ok": True, "note": "already running"})
        log = (ROOT / "logs" / "scheduler.log").open("a")
        proc = subprocess.Popen(
            ["/usr/bin/caffeinate", "-is", str(VENV_PY), "-m", "src.main", "run"],
            cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
        )
        SCHED_PID.write_text(str(proc.pid))
        return jsonify({"ok": True, "pid": proc.pid})
    if action == "stop":
        try:
            pid = int(SCHED_PID.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": False, "error": "bad action"}), 400


# ── web access: toggle LAN/phone exposure from the panel (restarts this server) ─
def _lan_ip() -> str | None:
    """Primary LAN IPv4 (the address phones on the same WiFi would use)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _tailscale_ip() -> str | None:
    """Tailscale IPv4 (100.64.0.0/10) — lets the phone connect from anywhere."""
    for c in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
              "tailscale", "/usr/local/bin/tailscale"):
        try:
            out = subprocess.run([c, "ip", "-4"], capture_output=True, text=True, timeout=2)
            for line in out.stdout.splitlines():
                ip = line.strip()
                if ip.startswith("100."):
                    return ip
        except Exception:
            continue
    return None


def _current_web_host() -> str:
    return os.getenv("WEB_HOST") or _read_env().get("WEB_HOST") or "127.0.0.1"


def _schedule_web_restart(port: int, host: str) -> None:
    """Detached relauncher: wait for the HTTP response to flush, kill this server,
    then start a fresh one bound to `host`. Survives our death via start_new_session.
    The host is passed EXPLICITLY (not via .env) because dotenv loads .env into
    os.environ with override=False, so a stale inherited WEB_HOST would otherwise win
    over the freshly-written .env value. Werkzeug sets SO_REUSEADDR for prompt rebind."""
    oldpid = os.getpid()
    p = int(port)
    h = shlex.quote(host)
    script = (
        f"sleep 1; kill {oldpid} 2>/dev/null; "
        # wait up to 5s for graceful exit, then force-kill
        f"for i in $(seq 1 10); do kill -0 {oldpid} 2>/dev/null || break; sleep 0.5; done; "
        f"kill -9 {oldpid} 2>/dev/null; "
        # wait until the port is actually released (avoids EADDRINUSE on rebind)
        f"for i in $(seq 1 20); do lsof -nP -iTCP:{p} -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.5; done; "
        f"cd {shlex.quote(str(ROOT))}; "
        f"WEB_HOST={h} WEB_PORT={p} nohup {shlex.quote(str(VENV_PY))} web/server.py "
        f"> logs/web.log 2>&1 & echo $! > logs/web.pid"
    )
    subprocess.Popen(["/bin/bash", "-lc", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.route("/api/web-access", methods=["GET", "POST"])
def api_web_access():
    port = int(os.getenv("WEB_PORT", "8770"))
    if request.method == "GET":
        host = _current_web_host()
        return jsonify({
            "mode": "lan" if host != "127.0.0.1" else "local",
            "password_set": bool(_web_password()),
            "port": port,
            "lan_ip": _lan_ip(),
            "tailscale_ip": _tailscale_ip(),
        })
    mode = (request.json or {}).get("mode")
    if mode not in ("lan", "local"):
        return jsonify({"ok": False, "error": "mode must be lan|local"}), 400
    if mode == "lan" and not _web_password():
        return jsonify({"ok": False,
                        "error": "请先设置「网页访问密码」再开启手机/局域网访问。"}), 400
    host = "0.0.0.0" if mode == "lan" else "127.0.0.1"
    _write_env_key("WEB_HOST", host)   # persist for future manual launches
    _schedule_web_restart(port, host)  # but relaunch binds `host` explicitly
    return jsonify({"ok": True, "mode": mode, "restarting": True})


@app.route("/api/signal-run/<mode>", methods=["POST"])
def api_signal_run(mode):
    if mode not in ("premarket", "intraday", "scalp"):
        return jsonify({"ok": False, "error": "mode must be premarket|intraday|scalp"}), 400
    subprocess.Popen([str(VENV_PY), "-m", "src.signal_reporter", mode],
                     cwd=str(ROOT), start_new_session=True)
    return jsonify({"ok": True, "mode": mode})


# ── backtest (background thread) ──────────────────────────────────────────────
_bt = {"status": "idle", "result": None}


def _run_bt(days: int):
    _bt["status"] = "running"
    try:
        from dataclasses import replace
        from src.backtest import BacktestConfig, prefetch_data, _run_live_engine
        P = ["SNDK", "MU", "INTC", "LRCX", "DDOG", "AMD", "WDC", "SWKS", "PANW", "MCHP"]
        cfg = BacktestConfig(
            days=days, timeframe=settings.timeframe, threshold=settings.entry_threshold,
            tickers=P, account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
            max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
            tp_atr_mult=settings.tp_atr_mult, sl_atr_mult=settings.sl_atr_mult,
            max_gap_pct=settings.max_gap_pct, apply_mr_strategy=False,
            use_scale_out=settings.use_scale_out, tp1_r=settings.tp1_r, tp2_r=settings.tp2_r)
        m = _run_live_engine(cfg, prefetch_data(cfg), rich_metrics=True)["metrics"]
        _bt["result"] = {
            "days": days, "per_day": round(m["net_pnl_usd"] / days, 2),
            "net": m["net_pnl_usd"], "trades": m.get("total_trades", 0),
            "win": m.get("win_rate_pct", 0), "dd": m.get("max_dd_mtm_pct", 0),
            "pf": m.get("profit_factor", 0), "sortino": m.get("sortino_ratio", 0),
        }
        _bt["status"] = "done"
    except Exception as e:
        _bt["status"] = "error"
        _bt["result"] = {"error": str(e)}


@app.route("/api/backtest", methods=["GET", "POST"])
def api_backtest():
    if request.method == "POST":
        if _bt["status"] != "running":
            days = int((request.json or {}).get("days", 180))
            threading.Thread(target=_run_bt, args=(days,), daemon=True).start()
        return jsonify({"ok": True, "status": _bt["status"]})
    return jsonify(_bt)


def main():
    port = int(os.getenv("WEB_PORT", "8770"))
    # Env override wins; otherwise the in-app toggle (persisted to .env) decides;
    # default localhost-only.
    host = os.getenv("WEB_HOST") or _read_env().get("WEB_HOST") or "127.0.0.1"
    # Safety: never expose the control panel to the network without a password.
    if host != "127.0.0.1" and not _web_password():
        print(f"⚠️  WEB_HOST={host} would expose the panel to the network, but no "
              f"WEB_PASSWORD is set.\n    Refusing — falling back to 127.0.0.1. Set a "
              f"password (Settings ⚙ on this Mac, or WEB_PASSWORD in .env) then restart.",
              file=sys.stderr)
        host = "127.0.0.1"
    if host != "127.0.0.1":
        print(f"🌐 Dashboard exposed on http://{host}:{port}  (password protected)")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
