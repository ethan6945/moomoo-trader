#!/bin/bash
# Double-click to launch the scheduler WITHOUT the GUI.
# - Wraps with `caffeinate -is` so macOS won't sleep while it's running.
# - Detaches via `nohup` + `&` so closing the Terminal doesn't kill it.
# - PID is written to logs/scheduler.pid (same convention as the GUI's Start button).
# - To stop: open the GUI and click ■ Stop, OR `kill -TERM $(cat logs/scheduler.pid)`.

cd "$(dirname "$0")"

# Already running?
if [ -f logs/scheduler.pid ]; then
    PID=$(cat logs/scheduler.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo "✘ Scheduler is already running (PID $PID)."
        osascript -e 'tell application "System Events" to display notification "Scheduler is already running" with title "moomoo-trader"'
        sleep 3
        exit 0
    fi
fi

# OpenD reachable?
if ! nc -z 127.0.0.1 11111 2>/dev/null; then
    echo "✘ OpenD is not reachable on 127.0.0.1:11111 — start OpenD first."
    osascript -e 'tell application "System Events" to display notification "OpenD not reachable — start OpenD first" with title "moomoo-trader"'
    sleep 4
    exit 1
fi

mkdir -p logs

# caffeinate -is:
#   -i  prevent system idle sleep
#   -s  prevent system sleep (entire process tree)
# Note: pair with `sudo pmset -c disablelidsleep 1` for laptop "lid-closed run" use.
nohup /usr/bin/caffeinate -is .venv/bin/python -m src.main run \
    > logs/scheduler.log 2>&1 &
PID=$!
disown
echo "$PID" > logs/scheduler.pid

echo "✓ Scheduler launched (PID $PID, wrapped in caffeinate)"
echo "  Log:        logs/scheduler.log"
echo "  Stop:       open the GUI and click ■ Stop, or:"
echo "              kill -TERM \$(cat logs/scheduler.pid)"
osascript -e 'tell application "System Events" to display notification "Scheduler started (caffeinate)" with title "moomoo-trader"'
sleep 3
