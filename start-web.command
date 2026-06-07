#!/bin/bash
# Double-click to (re)launch the web dashboard. RESTARTS cleanly so the latest
# backend code is always loaded (Flask doesn't auto-reload). Runs detached —
# survives closing the Terminal AND the browser. The trading scheduler is a
# SEPARATE process; this is only the monitor/control panel.
#   Stop the web server:  kill $(cat logs/web.pid)
cd "$(dirname "$0")"
mkdir -p logs
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
echo "✓ Web dashboard launched (PID $!) on http://127.0.0.1:$PORT"
sleep 2
open "http://127.0.0.1:$PORT"
