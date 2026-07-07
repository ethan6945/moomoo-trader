#!/bin/bash
# Double-click to (re)launch the web dashboard + OpenD-rs (headless).
# - Starts OpenD-rs automatically if not already running
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
OPEND_BIN="/opt/homebrew/bin/futu-opend"
OPEND_LOG="$PWD/logs/opend.log"
OPEND_PID_FILE="$PWD/logs/opend.pid"

mkdir -p logs

# Password comes from macOS Keychain (service "moomoo-opend") — never from
# .env plaintext or argv (2026-07-07 security hardening). Legacy .env
# OPEND_LOGIN_PWD is honoured as a fallback for fresh installs only.
OPEND_PWD="$(security find-generic-password -s moomoo-opend -w 2>/dev/null || true)"
if [ -z "$OPEND_PWD" ] && [ -n "${OPEND_LOGIN_PWD:-}" ]; then
    OPEND_PWD="$OPEND_LOGIN_PWD"
    echo "⚠ Using OPEND_LOGIN_PWD from .env — move it to Keychain with:"
    echo "  security add-generic-password -U -a \"$OPEND_LOGIN_ACCOUNT\" -s moomoo-opend -w '<password>'"
fi

# Already reachable?
if nc -z 127.0.0.1 11111 2>/dev/null; then
    echo "✓ OpenD already reachable on 127.0.0.1:11111"
elif [ -x "$OPEND_BIN" ] && [ -n "$OPEND_LOGIN_ACCOUNT" ] && [ -n "$OPEND_PWD" ]; then
    echo "→ Starting OpenD-rs (headless) as $OPEND_LOGIN_ACCOUNT ..."
    # --login-pwd-file: argv shows only a path (ps-safe). 600-perm temp file,
    # deleted as soon as OpenD is up (it reads the file once at startup).
    OPEND_PWD_FILE="$(mktemp "${TMPDIR:-/tmp}/opend_pwd.XXXXXX")"
    chmod 600 "$OPEND_PWD_FILE"
    printf '%s' "$OPEND_PWD" > "$OPEND_PWD_FILE"
    nohup "$OPEND_BIN" \
        --login-account "$OPEND_LOGIN_ACCOUNT" \
        --login-pwd-file "$OPEND_PWD_FILE" \
        --platform moomoo \
        --rest-port 22222 \
        --grpc-port 33333 \
        > "$OPEND_LOG" 2>&1 &
    echo $! > "$OPEND_PID_FILE"
    disown

    # Wait for port 11111
    for i in $(seq 1 15); do
        if nc -z 127.0.0.1 11111 2>/dev/null; then
            echo "✓ OpenD ready on 127.0.0.1:11111 (${i}s)"
            break
        fi
        if [ $i -eq 15 ]; then
            echo "✘ OpenD failed to start — check $OPEND_LOG"
        fi
        sleep 1
    done
    rm -f "$OPEND_PWD_FILE"

    # Unlock US trading
    echo "→ Unlocking US trade..."
    /opt/homebrew/bin/futucli unlock-trade --security-firm us >> "$OPEND_LOG" 2>&1 && \
        echo "✓ US trade unlocked" || \
        echo "⚠ unlock-trade returned non-zero (may already be unlocked)"
else
    echo "⚠ OpenD not running and cannot auto-start (install with: brew install futuleaf/tap/futu-opend-rs)"
    echo "  Set OPEND_LOGIN_ACCOUNT in .env and store the password in Keychain:"
    echo "  security add-generic-password -U -a <account> -s moomoo-opend -w '<password>'"
fi
unset OPEND_PWD OPEND_LOGIN_PWD

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
echo "  OpenD PID:  $(cat "$OPEND_PID_FILE" 2>/dev/null || echo 'n/a')"
echo "  OpenD log:  $OPEND_LOG"
