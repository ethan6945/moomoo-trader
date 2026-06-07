#!/bin/bash
# Double-click to launch the dashboard EXPOSED TO YOUR LOCAL NETWORK so your phone
# (on the same WiFi) can open it. Identical to start-web.command except it binds to
# 0.0.0.0. The server REFUSES to expose without a WEB_PASSWORD — set one first via
# the Settings ⚙ panel (run ./start-web.command on this Mac), then use this script.
#   Stop the web server:  kill $(cat logs/web.pid)
cd "$(dirname "$0")"
mkdir -p logs
PORT=${WEB_PORT:-8770}
export WEB_PORT=$PORT
export WEB_HOST=0.0.0.0

if ! grep -qE '^WEB_PASSWORD=.+' .env 2>/dev/null; then
    echo "⚠️  .env 里没有 WEB_PASSWORD —— 服务器会拒绝对外暴露，手机将连不上。"
    echo "    请先用 ./start-web.command 在本机打开，到 设置⚙ 里设好「网页访问密码」，再跑这个脚本。"
    echo
fi

# Restart cleanly so the latest backend code is always loaded.
if [ -f logs/web.pid ] && kill -0 "$(cat logs/web.pid)" 2>/dev/null; then
    kill "$(cat logs/web.pid)" 2>/dev/null
    sleep 1
fi
pkill -f "web/server.py" 2>/dev/null
sleep 1

nohup .venv/bin/python web/server.py > logs/web.log 2>&1 &
echo $! > logs/web.pid
disown
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
echo "✓ Web dashboard launched (PID $!)"
echo "   本机:   http://127.0.0.1:$PORT"
echo "   手机:   http://${IP:-<Mac的局域网IP>}:$PORT   (需同一 WiFi + 密码)"
sleep 2
open "http://127.0.0.1:$PORT"
