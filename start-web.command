#!/bin/bash
# Double-click to (re)launch the web dashboard + official OpenD app.
# - Starts moomoo_OpenD.app automatically if not already running
# - Unlocks US stock trading
# - RESTARTS cleanly so the latest backend code is always loaded (Flask doesn't
#   auto-reload). Runs detached — survives closing the Terminal AND the browser.
# - The trading scheduler is a SEPARATE process; click ▶ Start in the web UI to
#   launch it. Click ■ Stop to stop both scheduler + OpenD.
# - To stop everything including the web UI: Settings → Exit UI.
#
#   Stop the web server manually:  kill $(cat logs/web.pid)

cd "$(dirname "$0")"

# ── Load .env ──────────────────────────────────────────────────────
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# ── OpenD auto-start ────────────────────────────────────────────────
# 2026-07-09: back to the OFFICIAL moomoo_OpenD.app. The headless OpenD-rs
# gateway (v1.4.122) silently dropped every order (need_op_confirm stub with
# order_id=0, purged after ~30s) → each buy became a fake MANUAL_SELL ghost
# + re-buy loop. The official app keeps its own login session; we just launch
# it and wait for port 11111.
mkdir -p logs

if nc -z 127.0.0.1 11111 2>/dev/null; then
    echo "✓ OpenD already reachable on 127.0.0.1:11111"
else
    echo "→ Launching official moomoo_OpenD.app ..."
    open -a moomoo_OpenD
    for i in $(seq 1 60); do
        if nc -z 127.0.0.1 11111 2>/dev/null; then
            echo "✓ OpenD ready on 127.0.0.1:11111 (${i}s)"
            break
        fi
        if [ $i -eq 60 ]; then
            echo "✘ OpenD 端口 11111 未就绪 — 请到 OpenD 窗口完成登录后重试"
        fi
        sleep 1
    done
fi

# ── Web server ──────────────────────────────────────────────────────
PORT=${WEB_PORT:-8770}

# Kill any existing web server so we always start fresh on the new code.
if [ -f logs/web.pid ] && kill -0 "$(cat logs/web.pid)" 2>/dev/null; then
    kill "$(cat logs/web.pid)" 2>/dev/null
    sleep 1
fi
# Belt-and-suspenders: kill any stray server on this port's script.
pkill -f "web/server.py" 2>/dev/null
sleep 1

nohup .venv/bin/python web/server.py > logs/web.log 2>&1 &
echo $! > logs/web.pid
disown

# Wait until the server actually responds before opening the browser.
# Flask imports (src.ai, src.db, etc.) can take 3-6 seconds on a cold start.
echo -n "Waiting for web server..."
for i in $(seq 1 20); do
    if curl -s -o /dev/null -w '' --max-time 1 "http://127.0.0.1:$PORT/login" 2>/dev/null; then
        echo " ready (${i}s)"
        break
    fi
    echo -n "."
    sleep 1
done

open "http://127.0.0.1:$PORT"
echo "✓ Web dashboard launched (PID $(cat logs/web.pid)) on http://127.0.0.1:$PORT"
