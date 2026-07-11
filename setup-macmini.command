#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  moomoo-trader 迁移安装脚本 — 在 Mac mini 上双击运行一次即可
#
#  自动完成：
#    1. 确保 uv 可用（没有就自动安装）
#    2. 删掉随U盘带过来的旧 .venv（软链已失效）→ 用 Python 3.11 重建 + 装依赖
#    3. 按当前文件夹实际路径安装 crontab（每日/每周优化任务）
#    4. 修正脚本执行权限
#    5. 检查 OpenD(11111) 与 .env
#
#  前提：moomoo_OpenD.app 已在本机安装并登录（脚本只检测，不重装）
#  重复运行安全（幂等）。
# ═══════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "═══════════════════════════════════════════════════"
echo "   moomoo-trader 迁移安装 (Mac mini)"
echo "   目录: $DIR"
echo "═══════════════════════════════════════════════════"
echo ""

# ── 1. 确保 uv 可用 ────────────────────────────────────────────────
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
else
    echo "→ 未找到 uv，正在自动安装（需要联网）..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    if [ -x "$HOME/.local/bin/uv" ]; then
        UV_BIN="$HOME/.local/bin/uv"
    else
        echo "✘ uv 安装失败，请手动安装后重试： https://astral.sh/uv"
        read -n1 -r -p "按任意键关闭..."; exit 1
    fi
fi
echo "✓ uv: $("$UV_BIN" --version)"
echo ""

# ── 2. 重建虚拟环境 (Python 3.11) ─────────────────────────────────
if [ -d .venv ]; then
    echo "→ 删除随U盘带来的旧 .venv（软链已失效）..."
    rm -rf .venv
fi
echo "→ 用 Python 3.11 创建虚拟环境（首次会下载 3.11，稍等）..."
"$UV_BIN" venv --python 3.11 .venv
echo "→ 安装依赖 requirements.txt ..."
"$UV_BIN" pip install --python .venv/bin/python -r requirements.txt
echo "✓ 虚拟环境就绪：$(.venv/bin/python --version)"
echo ""

# ── 3. 安装 crontab（自适应当前路径）───────────────────────────────
echo "→ 配置 crontab 定时优化任务（路径自动指向本文件夹）..."
CRON_TMP="$(mktemp)"
# 保留现有其它任务，去掉旧的 moomoo 行避免重复
crontab -l 2>/dev/null | grep -v "moomoo-trader" | grep -v "optimize_and_apply.sh" > "$CRON_TMP" || true
# 若没有 MAILTO 设置，加一条抑制邮件
if ! grep -q '^MAILTO=' "$CRON_TMP" 2>/dev/null; then
    printf 'MAILTO=""\n' | cat - "$CRON_TMP" > "${CRON_TMP}.2" && mv "${CRON_TMP}.2" "$CRON_TMP"
fi
cat >> "$CRON_TMP" <<EOF
# moomoo-trader 周优化（全网格 quick sweep）— 周一 07:00 MYT（美股休市）
0 7 * * 1 cd $DIR && ./cron/optimize_and_apply.sh weekly >> logs/cron_optimize.log 2>&1
# moomoo-trader 每日增量优化（补昨日数据 + 邻域校验）— 周二至周六 09:00 MYT
0 9 * * 2-6 cd $DIR && ./cron/optimize_and_apply.sh daily >> logs/cron_optimize.log 2>&1
EOF
crontab "$CRON_TMP"
rm -f "$CRON_TMP"
echo "✓ crontab 已安装。当前 moomoo 任务："
crontab -l | grep -A0 "optimize_and_apply.sh" | sed 's/^/    /'
echo ""

# ── 4. 修正权限 + 目录 ─────────────────────────────────────────────
chmod +x cron/optimize_and_apply.sh start-web.command setup-macmini.command 2>/dev/null || true
mkdir -p logs
# 清掉从旧机带过来的失效 pid（旧机进程号在新机无意义）
rm -f logs/*.pid 2>/dev/null || true

# ── 5. 检查 OpenD ─────────────────────────────────────────────────
if nc -z 127.0.0.1 11111 2>/dev/null; then
    echo "✓ OpenD 已在 127.0.0.1:11111 运行"
else
    echo "⚠ OpenD 端口 11111 未就绪 — 启动时 start-web.command 会自动拉起"
    echo "   （请确保 moomoo_OpenD.app 已登录，且登录的是【模拟账户】）"
fi

# ── 6. 检查 .env ──────────────────────────────────────────────────
if [ -f .env ]; then
    ENVLINE="$(grep -E '^MOOMOO_TRADE_ENV' .env | head -1)"
    echo "✓ .env 存在 — $ENVLINE"
    case "$ENVLINE" in
        *REAL*) echo "   ⚠⚠ 注意：当前是 REAL 实盘模式！确认无误再启动。";;
    esac
else
    echo "✘ 缺少 .env！请从旧机把 .env 复制到本文件夹后重跑本脚本。"
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "   ✅ 安装完成"
echo "   下一步：双击  start-web.command  启动网页面板"
echo "   （交易调度器仍需在网页里点 ▶ Start 才开始跑）"
echo "═══════════════════════════════════════════════════"
read -n1 -r -p "按任意键关闭本窗口..."
echo ""
