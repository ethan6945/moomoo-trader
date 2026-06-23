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

from src import approvals, clock, db, keepawake, risk_manager  # noqa: E402
from src.config import settings  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
# Override only for running an isolated/secondary instance (e.g. tests). Default = repo .env.
ENV_FILE = Path(os.getenv("WEB_ENV_FILE") or (ROOT / ".env"))
ACCOUNT_FILE = ROOT / "data" / "account.json"
OPEN_TRADES_FILE = ROOT / "data" / "open_trades.json"
TRADER_LOG = ROOT / "logs" / "trader.log"
SIGNAL_LOG = ROOT / "logs" / "signal_reporter.log"
SIGNAL_PID = ROOT / "logs" / "signal_reporter.pid"
SIGNAL_WL_FILE = ROOT / "config" / "signal_watchlist.json"
SELF_REVIEW_FILE = ROOT / "data" / "self_review_last.json"
SCHED_PID = ROOT / "logs" / "scheduler.pid"
MENUBAR_PID = ROOT / "logs" / "menubar.pid"
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

# Login throttle — lock an IP out after repeated wrong passwords, so the control
# panel (which can stop the scheduler / change budget / write .env keys) can't be
# brute-forced online. In-memory: a restart resets it, which is fine since brute
# force needs sustained attempts. Concurrency: dict ops are atomic under CPython's
# GIL; an occasional miscount can't weaken the lock.
_login_fails: dict = {}          # ip → [fail_count, locked_until_epoch, last_seen_epoch]
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SEC = 300


def _login_locked(ip: str) -> bool:
    rec = _login_fails.get(ip)
    return bool(rec and rec[1] > time.time())


def _login_record(ip: str, ok: bool) -> None:
    if ok:
        _login_fails.pop(ip, None)
        return
    now = time.time()
    # Bound memory: an attacker rotating source IPs (only ever failing, never
    # authenticating) would otherwise grow this dict without limit. Prune every
    # OTHER entry that is not currently locked and has been idle for a full lock
    # window — never the current ip (pruning it here would reset its own streak
    # before the count below reads it, so the lockout could never trigger).
    for k, v in list(_login_fails.items()):
        if k != ip and v[1] < now and (now - v[2]) > LOGIN_LOCK_SEC:
            _login_fails.pop(k, None)
    rec = _login_fails.get(ip)
    # Rolling window: if this ip is unlocked and has been quiet for a full lock
    # window, start its fail streak fresh rather than accumulating forever.
    if rec and rec[1] < now and (now - rec[2]) > LOGIN_LOCK_SEC:
        rec = None
    count = (rec[0] if rec else 0) + 1
    locked = now + LOGIN_LOCK_SEC if count >= LOGIN_MAX_FAILS else 0.0
    _login_fails[ip] = [count, locked, now]


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
    ip = request.remote_addr or "?"
    if _login_locked(ip):
        return jsonify({"ok": False, "error": "尝试过多，请稍后再试"}), 429
    pw = (request.json or {}).get("password", "")
    if isinstance(pw, str) and hmac.compare_digest(pw, pw_cfg):
        _login_record(ip, True)
        resp = make_response(jsonify({"ok": True}))
        # secure follows the actual transport: on plain HTTP (LAN dev) we must NOT
        # set Secure or the browser would drop the cookie; behind HTTPS it engages
        # automatically for defence-in-depth.
        resp.set_cookie(AUTH_COOKIE, _auth_token(pw_cfg),
                        max_age=60 * 60 * 24 * 30, httponly=True,
                        samesite="Lax", secure=request.is_secure)
        return resp
    _login_record(ip, False)
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


def _pid_running(pid_file: Path) -> int | None:
    """Return the live PID recorded in pid_file, or None if not running."""
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)   # signal 0 = liveness check
        return pid
    except Exception:
        return None


def _scheduler_running() -> bool:
    return _pid_running(SCHED_PID) is not None


def _launch_menubar() -> None:
    """Spawn the menu-bar status app if it isn't already running. It mirrors the
    scheduler's life: it quits itself once the scheduler stops, so its presence in
    the menu bar == 'scheduler is running in the background'. Best-effort — a
    failure here must never block starting the scheduler."""
    if _pid_running(MENUBAR_PID) is not None:
        return
    try:
        log = (ROOT / "logs" / "menubar.log").open("a")
        proc = subprocess.Popen(
            [str(VENV_PY), "-m", "src.menubar_app"],
            cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
        )
        MENUBAR_PID.write_text(str(proc.pid))
    except Exception as e:
        print(f"menubar launch skipped: {e}", file=sys.stderr)


def _stop_pid(pid_file: Path) -> bool:
    """Gracefully stop the process group recorded in pid_file (mirrors the GUI):
    SIGTERM the whole group, wait up to ~3s, then remove the pid file. Returns
    True if there was a live process to signal."""
    pid = _pid_running(pid_file)
    if pid is None:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(15):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.2)
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    return True


def _opend_status(acct: dict, sched_running: bool) -> tuple[str, str]:
    """OpenD light, inferred WITHOUT opening a second broker connection (a fresh
    connection has its own unlock state and wouldn't reflect the scheduler's).
    Evidence that OpenD is unlocked = the scheduler has written an account
    snapshot with a real cash figure, since the writer only persists after a
    successful accinfo query and that requires an unlocked trade context.

      red    — OpenD socket unreachable (not started)
      green  — unlocked + market open + snapshot fresh → live & trading
      blue   — unlocked but market closed → the scheduler stops scanning off-hours
               so the snapshot ages out BY DESIGN; this is normal rest, not a lock
               problem (this is the state that used to mislead as 未解锁/未确认)
      yellow — reachable but worth a look; the label says which of three, and only
               claims 未解锁 when we genuinely never confirmed an unlock:
                 · 已连接 · 未解锁/未确认 — no successful accinfo ever (no cash written)
                 · 已解锁 · 调度器已停    — was unlocked but the scheduler isn't running
                 · 已解锁 · 连接中…       — scheduler up but snapshot stale (just
                                            restarted / catching up / stalled)
    """
    try:
        with socket.create_connection((settings.moomoo_host, settings.moomoo_port), timeout=0.6):
            pass
    except Exception:
        return "red", "OpenD 未启动"

    # A written snapshot always reflects a SUCCESSFUL accinfo query (the writer
    # bails before writing if the query raises), so a present cash figure — even
    # 0.0 on a fully-invested account — proves the context was unlocked.
    unlocked_ever = acct.get("cash") is not None
    interval = (acct.get("scan_interval_min") or 30) * 60
    try:
        age = time.time() - ACCOUNT_FILE.stat().st_mtime
    except Exception:
        age = float("inf")
    fresh = age < (interval * 2 + 300)

    session = clock.market_session()   # plain system NY time — no network/drift cost
    market_open = session == "open"

    if sched_running and unlocked_ever and market_open and fresh:
        return "green", "已解锁 · 可交易"

    # Off-hours: scanning intentionally pauses, so a stale snapshot is expected.
    # As long as the last session proved unlock and the snapshot isn't ancient
    # (tolerate a Fri-close→Mon-open weekend plus an adjacent holiday), report
    # unlocked-but-resting instead of the alarming 未解锁.
    OFF_HOURS_GRACE = 4 * 24 * 3600   # 4 days
    if sched_running and unlocked_ever and not market_open and age < OFF_HOURS_GRACE:
        label = {
            "premarket":  "已解锁 · 待开盘",
            "afterhours": "已解锁 · 已收盘",
            "weekend":    "已解锁 · 周末休市",
            "holiday":    "已解锁 · 假期休市",
        }.get(session, "已解锁 · 休市")
        return "blue", label

    # 走到这里 = 既非绿(可交易)也非蓝(休市)。快照里写过 cash 就证明 OpenD 曾被
    # 成功解锁,这类情况别再谎报 未解锁 —— 真正的问题在调度器/快照,不在锁本身。
    if unlocked_ever:
        if not sched_running:
            return "yellow", "已解锁 · 调度器已停"     # OpenD 正常;调度器没跑 → 去重启调度器
        return "yellow", "已解锁 · 连接中…"            # 调度器在跑但快照不新鲜:刚重启/追赶中/卡住
    return "yellow", "已连接 · 未解锁/未确认"            # 从没成功取过资金 → 确实无法确认已解锁


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
    # Enrich per_position with the GUI's open-trade fields (entry/stop/tp/atr) so the
    # web positions table mirrors the desktop GUI exactly. account.per_position only
    # carries live price/PnL; the static trade params live in open_trades.json.
    try:
        trades = _read_json(OPEN_TRADES_FILE, {})
        pp = acct.get("per_position") or {}
        for sym, tr in trades.items():
            cell = pp.setdefault(sym, {})
            cell.setdefault("qty", tr.get("qty"))
            cell["entry_price"] = tr.get("entry_price")
            cell["stop_loss"]   = tr.get("stop_loss")
            cell["take_profit"] = tr.get("take_profit")
            cell["atr"]         = tr.get("atr")
            cell["strategy"]    = tr.get("strategy")
            cell["pattern"]     = tr.get("pattern")   # chart pattern (pattern strategy only)
            # Manual-adoption status so the web can badge YOUR own moomoo-app buys
            # and show whether the bot has taken over or you're self-managing.
            cell["manual_adopted"] = bool(tr.get("manual_adopted"))
            cell["user_managed"]   = bool(tr.get("user_managed"))
            cell["adopt_risk"]     = tr.get("adopt_risk")
        acct["per_position"] = pp
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


def _spawn_scheduler() -> int:
    """Launch the trading scheduler as a detached, caffeinated process and record
    its pid. Shared by the start + restart actions so they stay byte-identical."""
    log = (ROOT / "logs" / "scheduler.log").open("a")
    proc = subprocess.Popen(
        ["/usr/bin/caffeinate", "-is", str(VENV_PY), "-m", "src.main", "run"],
        cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
    )
    SCHED_PID.write_text(str(proc.pid))
    _launch_menubar()
    return proc.pid


@app.route("/api/scheduler/<action>", methods=["POST"])
def api_scheduler(action):
    if action == "start":
        if _scheduler_running():
            return jsonify({"ok": True, "note": "already running"})
        return jsonify({"ok": True, "pid": _spawn_scheduler()})
    if action == "stop":
        had = _stop_pid(SCHED_PID)
        return jsonify({"ok": True, "running": _scheduler_running(),
                        "note": "not running" if not had else "stopped"})
    if action == "restart":
        # _stop_pid blocks until the old process group is gone (or ~3s), so a
        # fresh spawn afterwards can't collide with it. Picks up the latest .env
        # (e.g. a just-flipped MOOMOO_TRADE_ENV).
        _stop_pid(SCHED_PID)
        return jsonify({"ok": True, "pid": _spawn_scheduler(), "note": "restarted"})
    return jsonify({"ok": False, "error": "bad action"}), 400


@app.route("/api/trade-env", methods=["GET", "POST"])
def api_trade_env():
    """SIMULATE ⟷ REAL toggle. The trade env is read from .env when the scheduler
    process starts, so a change is written to .env here and applied by restarting
    the scheduler (the toggle UI offers the restart). GET reports the .env value
    (what the next start uses), the live value the running scheduler last reported,
    and the open-position count for the go-live safety check."""
    if request.method == "GET":
        env_file = (_read_env().get("MOOMOO_TRADE_ENV") or "SIMULATE").upper()
        acct = _read_json(ACCOUNT_FILE, {})
        env_live = (acct.get("trade_env") or "").upper() or None
        try:
            n_open = len(db.load_open_trades())
        except Exception:
            n_open = 0
        return jsonify({
            "env_file": env_file, "env_live": env_live,
            "running": _scheduler_running(), "open_positions": n_open,
            "pending_restart": bool(env_live and env_live != env_file),
        })

    body = request.json or {}
    target = (body.get("env") or "").upper()
    if target not in ("SIMULATE", "REAL"):
        return jsonify({"ok": False, "error": "env 必须是 SIMULATE 或 REAL"}), 400

    # Going REAL is real money — guard it: explicit confirm, trade password set,
    # and a FLAT book (so the local open-trades state can't be mistaken for / act
    # on the real account it was never opened in).
    if target == "REAL":
        if not body.get("confirm"):
            return jsonify({"ok": False, "error": "切换到实盘需要二次确认"}), 400
        if not _read_env().get("MOOMOO_TRADE_PWD"):
            return jsonify({"ok": False,
                            "error": "未设置 MOOMOO_TRADE_PWD（6 位交易密码）— 无法切到实盘"}), 400
        try:
            n_open = len(db.load_open_trades())
        except Exception:
            n_open = 0
        if n_open > 0:
            return jsonify({"ok": False,
                            "error": f"当前还有 {n_open} 个未平仓持仓（模拟盘）。请先全部平仓再切实盘，"
                                     f"否则本地持仓状态会和实盘账户串号。"}), 409

    _write_env_key("MOOMOO_TRADE_ENV", target)
    note = ("已切到实盘 💵 — 点「重启」后生效。首次实盘务必只放小额，先验证下单链路。"
            if target == "REAL"
            else "已切回模拟 🧪 — 点「重启」后生效。")
    return jsonify({"ok": True, "env": target, "note": note,
                    "restart_required": True, "running": _scheduler_running()})


# ── keep-awake toggle ("caffeinate" — Amphetamine-style) ───────────────────────
# All the logic lives in src/keepawake.py so the web dashboard and the menu-bar app
# behave identically. Layer 1 = `caffeinate -i -s` (no idle/system sleep, screen may
# still turn off). Layer 2 = `sudo pmset disablesleep` (also blocks lid-closed sleep)
# via a one-time, tightly-scoped sudoers rule that the first enable installs through
# a native macOS auth dialog.
@app.route("/api/caffeinate", methods=["GET", "POST"])
def api_caffeinate():
    if request.method == "POST":
        on = bool((request.get_json(silent=True) or {}).get("on"))
        return jsonify(keepawake.turn_on() if on else keepawake.turn_off())
    return jsonify(keepawake.status())


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
            # Plain HTTP: on open WiFi the password + cookie travel unencrypted.
            # Prefer the Tailscale address (encrypted tunnel) over the raw LAN IP.
            "plaintext_warning": "局域网为明文 HTTP，密码/会话在同网段可被嗅探；"
                                 "公共 WiFi 下建议走 Tailscale 地址而非裸 LAN IP。",
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
    if mode not in ("brief", "review", "close", "premarket", "intraday"):
        return jsonify({"ok": False,
                        "error": "mode must be brief|review|close|premarket|intraday"}), 400
    subprocess.Popen([str(VENV_PY), "-m", "src.signal_reporter", mode],
                     cwd=str(ROOT), start_new_session=True)
    return jsonify({"ok": True, "mode": mode})


# ── signal reporter scheduler (persistent loop) — mirrors the desktop GUI ──────
@app.route("/api/signal-status")
def api_signal_status():
    pid = _pid_running(SIGNAL_PID)
    return jsonify({"running": pid is not None, "pid": pid})


@app.route("/api/signal-scheduler/<action>", methods=["POST"])
def api_signal_scheduler(action):
    if action == "start":
        if _pid_running(SIGNAL_PID) is not None:
            return jsonify({"ok": True, "running": True, "note": "already running"})
        SIGNAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = SIGNAL_LOG.open("a")
        proc = subprocess.Popen(
            [str(VENV_PY), "-m", "src.signal_reporter", "run"],
            cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
        )
        SIGNAL_PID.write_text(str(proc.pid))
        return jsonify({"ok": True, "running": True, "pid": proc.pid})
    if action == "stop":
        had = _stop_pid(SIGNAL_PID)
        return jsonify({"ok": True, "running": _pid_running(SIGNAL_PID) is not None,
                        "note": "not running" if not had else "stopped"})
    return jsonify({"ok": False, "error": "bad action"}), 400


# ── signal watchlist editor (config/signal_watchlist.json) ─────────────────────
@app.route("/api/signal-watchlist", methods=["GET", "POST"])
def api_signal_watchlist():
    if request.method == "GET":
        data = _read_json(SIGNAL_WL_FILE, {})
        tickers = [str(t).strip().upper() for t in (data.get("tickers") or []) if str(t).strip()]
        return jsonify({"tickers": tickers})
    body = request.get_json(silent=True) or {}
    raw = body.get("tickers")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "tickers must be a list"}), 400
    # Normalize: uppercase, strip, dedupe (preserve order) — same shape the bot reads.
    seen, tickers = set(), []
    for t in raw:
        s = str(t).strip().upper()
        if s and s not in seen:
            seen.add(s); tickers.append(s)
    try:
        SIGNAL_WL_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIGNAL_WL_FILE.write_text(
            json.dumps({"tickers": tickers}, indent=2, ensure_ascii=False)
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "tickers": tickers})


# ── weekly self-review ("retrain") — analyze real fills → suggestions ──────────
@app.route("/api/self-review")
def api_self_review():
    """Last persisted weekly self-review result (for the dashboard pill)."""
    data = _read_json(SELF_REVIEW_FILE, {})
    return jsonify(data)


@app.route("/api/self-review/run", methods=["POST"])
def api_self_review_run():
    """Trigger the weekly self-review now — same job as the Sunday cron
    (analyze last 7d fills → notify → enqueue suggestions → optimizer proposals).
    Runs detached; the dashboard polls /api/self-review for the fresh result."""
    log = (ROOT / "logs" / "self_review.log").open("a")
    subprocess.Popen(
        [str(VENV_PY), "-m", "src.main", "review"],
        cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
    )
    return jsonify({"ok": True, "started": True})


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


def _self_review_catchup_on_boot() -> None:
    """If the weekly self-review is overdue (laptop was off on its scheduled day),
    fire it ONCE on startup — detached, so it never blocks the server. The trading
    scheduler has its own catchup; this covers the common case where the user only
    opens the web dashboard. cron_state.record_run() makes it idempotent: once it
    runs, last_run is fresh and a quick restart won't re-fire it."""
    try:
        from src import cron_state
        expected = cron_state.expected_last_fire_weekly(6, 23, 0)  # Sun 23:00 ET
        if not cron_state.needs_catchup("self_review", expected):
            return
        last = cron_state.last_run("self_review")
        print(f"🧠 Self-review overdue (last run: {last or 'never'}) — running catchup now.")
        log = (ROOT / "logs" / "self_review.log").open("a")
        subprocess.Popen(
            [str(VENV_PY), "-m", "src.main", "review"],
            cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True,
        )
    except Exception as e:
        print(f"self-review catchup check skipped: {e}", file=sys.stderr)


def main():
    _self_review_catchup_on_boot()
    # If the scheduler is already running (e.g. started yesterday), make sure the
    # menu-bar status icon is up too — so opening the dashboard reattaches the icon.
    if _scheduler_running():
        _launch_menubar()
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
        print("⚠️  Plain HTTP — password + session cookie travel UNENCRYPTED on the "
              "LAN. On open WiFi, reach it via Tailscale (encrypted) rather than the "
              "raw LAN IP.", file=sys.stderr)
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
