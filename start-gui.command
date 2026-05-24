#!/bin/bash
# Double-click to launch the moomoo-trader GUI.
# This terminal window can be closed after the GUI opens.

cd "$(dirname "$0")"

if pgrep -f "gui.py" > /dev/null; then
    echo "GUI is already running."
    osascript -e 'tell application "System Events" to display notification "GUI is already running" with title "moomoo-trader"'
    sleep 2
    exit 0
fi

mkdir -p logs
nohup .venv/bin/python gui.py > logs/gui.log 2>&1 &
disown
echo "✓ GUI launched (PID $!)"
sleep 2
