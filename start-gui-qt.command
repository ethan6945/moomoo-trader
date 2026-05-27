#!/bin/bash
# Double-click to launch the MooMoo Trader PyQt6 GUI (modern redesign).

cd "$(dirname "$0")"

if pgrep -f "gui_qt.py" > /dev/null; then
    echo "Qt GUI is already running."
    osascript -e 'tell application "System Events" to display notification "Qt GUI is already running" with title "moomoo-trader"'
    sleep 2
    exit 0
fi

mkdir -p logs
nohup .venv/bin/python gui_qt.py > logs/gui_qt.log 2>&1 &
disown
echo "✓ Qt GUI launched (PID $!)"
sleep 2
