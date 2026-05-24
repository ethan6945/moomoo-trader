# MooMoo Trader

> **AI 驱动的美股自动化短线交易系统** — 多因子打分 + ML + Gemini AI + Optuna 自动调参 + 自适应仓位 + 自动止损 + Telegram 通知。

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-private-red.svg)](#)

> ⚠️ **强烈警告：交易有亏损风险。本程序不保证盈利，必须先 paper trade ≥ 4 周才能切真钱。**
> 作者不对任何亏损负责。

---

## 目录

- [系统亮点](#系统亮点)
- [架构总览](#架构总览)
- [快速上手](#快速上手)
- [核心功能详解](#核心功能详解)
- [AI 自治系统](#ai-自治系统)
- [回测 + 参数优化](#回测--参数优化)
- [自动定时任务](#自动定时任务)
- [真实性能预期](#真实性能预期)
- [项目结构](#项目结构)
- [上线 checklist](#上线-checklist)
- [常见问题](#常见问题)

---

## 系统亮点

| 模块 | 描述 |
|------|------|
| 🎯 **6 因子评分** | 趋势 / 动量 / 量能 / 形态 / ADX / VWAP，多周期组合 |
| 🤖 **ML XGBoost 验证层** | 历史胜率预测，每月自动重训 |
| 🧠 **Gemini AI 上下文审核** | 用 Tavily 新闻 + LLM 否决可疑信号 |
| 📊 **Optuna 贝叶斯调参** | TPE 算法走样本外验证，每月自动建议新参数 |
| 🚦 **8 道过滤漏斗** | trade_phase / regime / MTF / gap / sector / earnings / ML / AI |
| 🛡 **多层风控** | 2% 单笔风险 + 8% 组合热度 + DD 断路器 + 3 连亏熔断 + VIX 调仓 |
| 💸 **真实成交模拟** | 限价单实际可成交模型 + ATR 缩放滑点 + 止损穿透 |
| 🧮 **诚实指标** | 日收益级 Sharpe + Sortino + Calmar + MAR + Ulcer + 1000× Monte Carlo |
| 🔄 **时间步进回测器** | 账户级 portfolio simulation，与 live 行为完全一致 |
| 🎚 **自适应仓位** | 跟随近 30 笔 Sortino 自动调 RISK_PER_TRADE（0.5×~1.25×）|
| 🚫 **自适应黑名单** | 输家股自动观察期 30-90 天，恢复了自动剔出 |
| 🔁 **错过任务自动补** | 关机过的定时任务，开机检测并自动补跑 |
| 📱 **GUI + Telegram** | Tkinter 控制台 + 实时手机推送 |
| 🌐 **每周自动选股** | yfinance 拉 S&P 500，按动量 + ATR + RS 选 top 30 |

---

## 架构总览

```
                    ┌────────── APScheduler ──────────┐
                    │                                  │
                    ▼                                  ▼
        ┌─── 每 30 分钟扫描 ───┐         ┌─── 定时任务 ───┐
        │                       │         │                │
        ▼                       │         ▼                ▼
  ┌───────────┐                 │   Daily 23:00         Sun 22:00
  │  Tickers  │                 │   blacklist           watchlist
  │  (30 只)  │                 │   review              refresh
  └─────┬─────┘                 │                          │
        │                       │   Sun 22:30           Month-1 02:00
        ▼                       │   backtest            ML retrain
  ┌─────────────────┐           │                          │
  │ 6-Factor Score  │           │   Month-1 03:00       Startup
  │  + ML proba     │           │   Optuna              catch-up
  │  + AI veto      │           │   suggest             missed jobs
  └─────────────────┘           │                          │
        │                       └──────────────────────────┘
        ▼
  ┌─── 8 道过滤 ───┐
  │ trade phase    │
  │ regime         │
  │ blacklist      │
  │ MTF + gap      │
  │ sector cap     │
  │ earnings block │
  │ ML veto        │
  │ AI veto        │
  └────────┬───────┘
           ▼
  ┌─── 仓位计算 ───┐
  │ × DD breaker   │
  │ × VIX mult     │
  │ × Adaptive     │
  │ × Loss streak  │
  └────────┬───────┘
           ▼
  ┌─── MooMoo OpenD ───┐
  │ 限价单 + OCO 止损  │
  │ Paper / Real       │
  └────────┬───────────┘
           ▼
     Telegram + SQLite + audit log
```

---

## 快速上手

### Step 0. 本地必备依赖（每个人安装一次）

bot 本身只是脚本，它依赖以下东西**先在你本地装好**：

| 依赖 | 用途 | Mac 安装 |
|------|------|---------|
| **Python 3.11+** | 跑代码 | `brew install python@3.11`（Mac 自带 3.x 也行）|
| **MooMoo OpenD** | 行情 + 下单服务 | [openapi.moomoo.com](https://openapi.moomoo.com) 下载 .dmg 安装 |
| **MooMoo App** | 账户开户 + 解锁 OpenD | App Store 搜 "moomoo" |
| **Git** | 拉代码 | Mac 自带 |
| **GitHub Desktop**（可选）| 图形化推代码 | [desktop.github.com](https://desktop.github.com) |
| **Homebrew**（可选但推荐）| 装其他工具 | `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` |

**外部 API 账户**（免费层够用）：

| 服务 | 用途 | 注册地址 | 免费额度 |
|------|------|---------|---------|
| MooMoo OpenAPI | 行情 + 交易 | [openapi.moomoo.com](https://openapi.moomoo.com) | 完全免费 |
| Google Gemini | AI 信号验证 | [aistudio.google.com](https://aistudio.google.com) | 1500 req/day |
| Tavily | 新闻搜索 | [app.tavily.com](https://app.tavily.com) | 1000 req/month |
| Telegram Bot | 通知推送 | 在 Telegram 找 [@BotFather](https://t.me/BotFather) | 完全免费 |

全部免费。最多花你 30 分钟注册。

---

### Step 1. 注册 MooMoo + 开通 OpenAPI

1. 去 [moomoo.com](https://www.moomoo.com)（马来西亚：[moomoo.com/my](https://www.moomoo.com/my)）下载 App 注册账户。
2. 完成开户审核（1-3 个工作日）。
3. 打开 [openapi.moomoo.com](https://openapi.moomoo.com)，登录 → 完成问卷 → 下载 **OpenD**。
4. 在 App 内开通 **Paper Trading** 账户（默认 USD 1,000,000 虚拟资金）。

### Step 2. 安装 OpenD（Mac）

下载 [OpenD for macOS](https://openapi.moomoo.com/moomoo-api-doc/intro/install.html)，双击运行：

- 默认监听 `127.0.0.1:11111`
- 首次登录需要短信/邮箱验证码
- **交易密码**（6 位数字）和登录密码是分开的：App → 我的 → 设置 → 交易密码

OpenD 必须**全程运行**。

### Step 3. 克隆 + 安装

```bash
git clone <your-repo-url> moomoo-trader
cd moomoo-trader

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 4. 配置 `.env`

```bash
cp .env.example .env
# 用编辑器打开 .env 填入：
#   - MOOMOO_TRADE_PWD（6 位交易密码）
#   - GEMINI_API_KEYS（https://aistudio.google.com 申请，免费 1500 RPD）
#   - TAVILY_API_KEY（https://app.tavily.com，免费 1000 req/mo）
#   - TELEGRAM_TOKEN + TELEGRAM_CHAT_ID（找 @BotFather 创建）
```

### Step 5. 启动

```bash
# 方法 A：GUI（推荐）
python gui.py

# 方法 B：命令行单次扫描
python -m src.main scan

# 方法 C：调度器自动循环（每 30 分钟扫一次）
python -m src.main run
```

启动后会自动：
- 检测上次开机后错过的定时任务 → 自动补跑
- 美股开盘后每 30 分钟扫一次信号
- Telegram 推送下单 / 止损 / 状态变化

---

## 核心功能详解

### 6 因子评分（HOUR_1 模式）

| 维度 | 指标 | 权重 |
|------|------|------|
| 趋势 | EMA9 > EMA21 + 斜率向上 | 18 |
| 动量 | MACD 金叉 + RSI 40-70 + Stochastic | 18 |
| 成交量 | 当日量 > 20 日均量 × 1.5 | 14 |
| 形态 | 突破 20 日新高 / BB 下轨反弹 | 14 |
| ADX | ADX ≥ 25 强趋势确认 | 13 |
| VWAP | 价格 ≥ rolling VWAP(20) | 13 |
| AI 验证 | Gemini + Tavily 新闻 + 财报检查 | 10 |

入场阈值：**综合分数 ≥ ENTRY_SCORE_THRESHOLD**（Optuna 调优后默认 60）

### 8 道过滤漏斗

```
33 只候选股 × 每 30 分钟扫一次
   │
   ├─ 1. 交易时段 (09:45-15:30 ET)
   ├─ 2. Market regime (SPY 200MA → 不在 BEAR)
   ├─ 3. 自适应黑名单
   ├─ 4. 隔夜跳空 > 2.5% → 拒
   ├─ 5. 板块集中度 < 25%
   ├─ 6. 财报 ≤ 2 天 → 拒
   ├─ 7. ML 概率 ≥ 0.35
   ├─ 8. AI veto pass
   │
   └─ → 风控 + 仓位计算 → 下单
```

### 风控规则（硬约束，AI 不可否决）

| 规则 | 阈值 | 实现 |
|------|------|------|
| 单笔风险 | 2% (`RISK_PER_TRADE`) | `risk_manager.calc_position_size` |
| 单股仓位 | 10% (`MAX_POSITION_PCT`) | qty 上限 |
| 同时持仓数 | 10 (`MAX_POSITIONS`) | portfolio cap |
| 组合热度 | 8% | `portfolio.heat_check` |
| 日回撤 | -3% | `kill_switch.daily_drawdown_stop` |
| 账户 DD 减仓 | ≥ 10% → 0.5× | `risk_manager._dd_size_multiplier` |
| 账户 DD 停盘 | ≥ 15% | `risk_manager.can_open_new` |
| 3 连亏 | 暂停 | `risk_manager._drawdown_risk_multiplier` |
| 财报屏蔽 | ≤ 2 天 | `earnings.earnings_block` |
| Regime BEAR | 禁开仓 | `regime.assess` |
| VIX 调仓 | >25 减半，>35 四分之一 | `risk_manager.calc_position_size` |

---

## AI 自治系统

> **目标：95% 自动化，用户只需要看 Telegram。**

### 1. 自适应仓位（`adaptive_sizing.py`）

读取近 30 笔已平仓交易，计算日收益级 Sortino：

```
Sortino > 5     → 1.25× (热手期加仓)
Sortino 2-5     → 1.00× 基线
Sortino 0-2     → 0.75× 降温
Sortino ≤ 0     → 0.50× 保护模式
<10 笔          → 1.00× 热身
```

跟 DD 断路器、3 连亏、VIX 4 层独立叠加。

### 2. 自适应黑名单（`blacklist.py`）

**核心：不下死决定，让 AI 变通**

输家股自动进入观察名单：
- 加入门槛跟随整体 Sortino 浮动（健康时严格 $-25，亏损期宽松 $-200）
- 每条记录有 `next_review_days`（从 30 天起）
- 恢复了立刻移除
- 还不行 → 复评期延长 15 天（最长 90 天）
- 持久化到 `data/blacklist.json`

### 3. 每周智能选股（`watchlist_updater.py`）

每周日 22:00 ET 自动跑：
- yfinance 拉 S&P 500 完整名单
- 过滤：日均成交量 ≥ 500 万股、价格 ≥ $15、ATR% ≥ 1.5%、5d/20d 都正收益
- 排序：5 日相对 SPY 强度 + 20 日动量 + ATR%
- 锚定 10 只大盘股 + 排名前 20 = 30 只
- **自动跳过黑名单股**

### 4. 每月 Optuna 调参（`optimizer.py`）

每月 1 号 03:00 ET 自动跑：
- TPE 贝叶斯算法搜索 5 个参数
- 180 天数据 × 3 折样本外验证
- 输出最优参数到 Telegram（**不自动应用**，需人工审核）

### 5. 启动自动补任务（`cron_state.py`）

关机错过的定时任务，开机自动补：
- Daily blacklist review
- Weekly watchlist refresh
- Weekly backtest
- Monthly ML retrain
- Monthly Optuna

每个任务多次错过只补一次（coalesce）。

---

## 回测 + 参数优化

### 跑回测

```bash
# 默认参数 + 最近 180 天
python -m src.backtest --days 180

# 指定时间框架 / threshold
python -m src.backtest --days 90 --timeframe HOUR_1 --threshold 65

# 指定特定股票
python -m src.backtest --tickers AAPL NVDA TSLA --days 60
```

输出包含：
- Win rate / Profit factor / Expectancy
- Sharpe / Sortino / Calmar / MAR / Ulcer Index
- Max DD / Underwater days
- **Monte Carlo 1000× P(profitable) / P(ruin)**
- 月度 PnL bar chart
- Top 5 标的

### 跑 Optuna 优化

```bash
# 推荐：180 天 × 20 trials × 3 折
python -m src.optimizer --days 180 --trials 20 --folds 3 --min-trades 60

# 快速测试（仅 10 trials）
python -m src.optimizer --days 90 --trials 10 --folds 3 --min-trades 30
```

结果存到 `data/optimizer/` 并 Telegram 推送 Top 5 trials。

---

## 自动定时任务

| 时间（美东 ET）| 任务 | 模块 |
|---------------|------|------|
| 每 30 分钟（交易时段）| 扫描信号 + 管理仓位 | `main.run_loop` |
| 每天 23:00 | 黑名单复评 | `blacklist.evaluate_all` |
| 周日 22:00 | Watchlist 自动刷新 | `watchlist_updater.refresh` |
| 周日 22:30 | 90 天回测健康检查 | `_weekly_backtest_validation_job` |
| 每月 1 号 02:00 | ML XGBoost 重训 | `_monthly_retrain_job` |
| 每月 1 号 03:00 | Optuna 调参（建议）| `_monthly_optuna_job` |
| **启动时** | 错过任务自动补 | `cron_state` |

---

## 真实性能预期

> **基于 180 天回测 + 时间步进 portfolio simulator（与 live 行为一致）**

| 指标 | 数值 |
|------|------|
| 总 Trades | 584 |
| Win Rate | 60.9% |
| Profit Factor | 1.28 |
| Sortino | 3.48 |
| Sharpe (daily) | 2.53 |
| Net PnL (180d) | +$991 (+22.0%) |
| CAGR (年化) | +49.7% |
| Max Drawdown | -9.31% |
| Underwater 天数 | 57 |
| P(profitable) Monte Carlo | 99.5% |

**月度分布（注意：有亏有赚是正常的）：**

```
2025-09 +$229
2025-10 +$362
2025-11 -$138  ← 板块轮动期亏损月
2025-12 +$9
2026-01 +$269
2026-02 -$254  ← 持续震荡
2026-03 -$202
2026-04 +$242
2026-05 +$473
```

⚠ **诚实预期：不会月月赚。年化 15-25% + 最大回撤 10% 是真实区间。**

---

## 项目结构

```
moomoo-trader/
├── src/
│   ├── main.py                 # 入口：扫描 + 调度 + catchup
│   ├── moomoo_client.py        # MooMoo OpenD 封装
│   ├── config.py               # 环境变量 → settings
│   ├── timeframe.py            # 时间框架预设
│   │
│   ├── indicators.py           # 6 因子打分
│   ├── strategy_mr.py          # 均值回归策略
│   ├── ai_validator.py         # Gemini + Tavily AI 审核
│   ├── regime.py               # SPY 市场环境探测
│   ├── earnings.py             # 财报屏蔽
│   ├── sector.py               # 板块集中度
│   │
│   ├── ml/
│   │   ├── features.py         # 特征工程
│   │   ├── dataset.py          # 训练集构造
│   │   ├── train.py            # XGBoost 训练
│   │   ├── predict.py          # 推理
│   │   └── calibration.py      # 概率校准
│   │
│   ├── risk_manager.py         # 仓位计算 + 风控
│   ├── adaptive_sizing.py      # 🆕 自适应 RISK_PER_TRADE
│   ├── blacklist.py            # 🆕 自适应黑名单
│   ├── kill_switch.py          # 统一熔断
│   ├── portfolio.py            # 组合热度 + R-multiple
│   │
│   ├── executor.py             # 下单 + OCO 止损
│   ├── notifier.py             # Telegram
│   ├── reconcile.py            # 券商 vs 本地对账
│   ├── audit.py                # 决策审计
│   │
│   ├── backtest.py             # 时间步进 portfolio backtest
│   ├── optimizer.py            # Optuna 调参
│   ├── metrics.py              # Sharpe/Sortino/MC
│   ├── watchlist_updater.py    # 🆕 yfinance 自动选股
│   ├── cron_state.py           # 🆕 错过任务自动补
│   │
│   ├── db.py                   # SQLite
│   ├── history.py              # 资金曲线
│   ├── clock.py                # NY 时间 + NTP
│   ├── news_fetcher.py         # Tavily
│   └── i18n.py                 # 中英文翻译
│
├── config/
│   └── watchlist.json          # 自动更新的 30 只候选股
├── data/                       # 运行时状态（gitignored）
│   ├── trader.db               # SQLite 主存储
│   ├── audit.jsonl             # 每次决策记录
│   ├── trades.jsonl            # 已平仓交易
│   ├── blacklist.json          # 当前黑名单
│   ├── cron_state.json         # 定时任务上次运行
│   ├── ml/                     # XGBoost 模型
│   └── optimizer/              # Optuna 结果
├── logs/                       # 运行日志（gitignored）
│
├── gui.py                      # Tkinter 控制台
├── start-gui.command           # Mac 双击启动
├── start-scheduler.command     # 命令行启动
├── requirements.txt
├── .env.example                # 配置模板
└── README.md
```

---

## 上线 checklist

> **不要跳步骤。每一项都是为了保护你的钱。**

- [ ] OpenD 在 Paper Trading 账户跑通（GUI 状态栏全绿）
- [ ] 用 `python -m src.main scan` 看到信号被评分
- [ ] 配置 Telegram，能收到测试推送
- [ ] 跑一次 `python -m src.backtest --days 180`，看 Sortino > 2 + P(profitable) > 90%
- [ ] **Paper trade 至少 4 周**，记录每笔交易
- [ ] 实盘 Sortino > 1.5 + win_rate > 55%
- [ ] 切真实账户：`MOOMOO_TRADE_ENV=REAL`
- [ ] 首周只投 **USD $500** 验证下单链路
- [ ] 连续两周稳定后逐步加到目标资金
- [ ] 每月看一次 Optuna 建议是否要应用

---

## 常见问题

### Q1：OpenD GUI 启动后要求"解锁交易"怎么办？
A：GUI 版的 OpenD 不能通过 API 解锁，需要手动点 OpenD 窗口的"解锁交易"按钮一次。bot 之后会用 MD5 哈希后的 `MOOMOO_TRADE_PWD` 自动登录。

### Q2：Paper Trading 不支持 STOP 单？
A：是的，MooMoo 的模拟账户不支持 STOP 单。bot 通过主循环检测当前价 ≤ stop_loss 后市价平仓（软止损）。Real 账户支持 OCO Bracket，自动挂止损 + 止盈。

### Q3：Gemini API 配额会爆吗？
A：免费层 1500 RPD。bot 每次扫描最多 5 次 AI 调用，每天 ~13 次扫描（市场时段）= 65 calls/day。完全在配额内。

### Q4：DD 断路器和 3 连亏熔断有什么区别？
A：
- **3 连亏**：连续 3 天净亏损 → 暂停（按天计）
- **DD 断路器**：账户总回撤 ≥ 10% → 半仓，≥ 15% → 停盘（按账户级 peak）
- 两者独立叠加，任何一个触发都减仓

### Q5：每月 Optuna 建议是否要自动应用？
A：**不要**。当前设计是手动审核。Optuna 可能找到过拟合短期窗口的参数，连续 2-3 个月建议同一方向才安全切换。

### Q6：Mac 睡眠会断开 OpenD 吗？
A：会。`start-scheduler.command` 用 `caffeinate -is` 防止系统睡眠，但屏幕可以睡。建议接电源跑。

### Q7：可以跑 24 小时不间断吗？
A：可以。OpenD + scheduler + caffeinate 设计就是为了这个。但建议每天看一次 Telegram 状态确保健康。

### Q8：哪些股票适合？
A：watchlist 自动选 S&P 500 高流动性 + 高 ATR 的 30 只，每周更新。算法已经替你选。

---

## 技术栈

```
Python 3.11 + asyncio + APScheduler
MooMoo OpenAPI Python SDK
XGBoost + scikit-learn (ML 验证)
Optuna (Bayesian hyperopt)
yfinance (watchlist 自动选股)
Google Generative AI (Gemini)
Tavily Search API (新闻)
pandas + pandas-ta-classic (技术指标)
SQLite (持久化)
Tkinter (GUI)
python-telegram-bot
```

---

## License

Private use only. Not for redistribution.

---

## ⚠️ Disclaimer

This is a personal research project. It does NOT constitute financial advice.
- 交易股票存在亏损本金的实质风险
- 历史回测结果不代表未来表现
- 本程序的盈利预期 (Sortino 3+) 假设市场状况延续过去 180 天的特征
- 市场结构变化（regime change）可能让任何策略亏钱
- 始终 paper trade ≥ 4 周再用真钱
- 真钱第一次只投你能完全亏掉的金额

**作者不对任何使用本程序产生的金钱亏损负责。**
