# MooMoo Trader

> **AI 驱动的美股自动化短线交易系统** — 4 策略多因子打分 + Gemini AI 上下文复核 + Optuna 自动调参 + 自适应仓位/黑名单 + 多层风控 + 跳空哨兵 + Web 仪表盘 + Telegram 通知/审批。

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-private-red.svg)](#)

> ⚠️ **强烈警告：交易有亏损风险。本程序不保证盈利，必须先 paper trade（模拟盘）≥ 4 周才能切真钱。作者不对任何亏损负责。**

---

## 目录

- [这是什么](#这是什么)
- [进程模型（怎么跑起来的）](#进程模型怎么跑起来的)
- [快速上手](#快速上手)
- [买入评估：策略 + 权重](#买入评估策略--权重)
- [候选漏斗（一连串过滤闸）](#候选漏斗一连串过滤闸)
- [仓位计算](#仓位计算)
- [卖出 / 退出逻辑](#卖出--退出逻辑)
- [风控规则（硬约束）](#风控规则硬约束)
- [`.env` 配置全解（开 / 关 + 为什么）](#env-配置全解开--关--为什么)
- [AI 自治系统](#ai-自治系统)
- [回测 + 参数优化](#回测--参数优化)
- [自动定时任务](#自动定时任务)
- [审批闸（铁律）](#审批闸铁律)
- [项目结构](#项目结构)
- [上线 checklist](#上线-checklist)
- [常见问题](#常见问题)

---

## 这是什么

一个**自治的美股短线 swing 交易机器人**（不是 MCP server，不是 GUI 玩具）。它在美股交易时段每 30 分钟扫描一个动态选出的股票池，用 4 套技术策略打分，过一连串风控/上下文过滤闸，自动下限价单并管理止损/止盈，全程通过 **Web 仪表盘 + Telegram** 监控和审批。

**设计目标：~95% 自动化。** 你平时只需要看 Telegram 推送，偶尔在 Web 面板上批一下 AI 优化器提的参数建议。

**两条贯穿全系统的铁律（理解它们才能理解为什么很多功能默认关）：**

1. **Live ↔ 回测口径一致（parity）**：实盘成交的单子必须和「算出 $/day 的那套诚实回测」是同一批。任何会让 live 偏离回测的功能（如 AI 否决）默认设成「建议/不拦单」。
2. **未验证不上线**：新策略/新退出只有在 `backtest_v3` 双窗口回测里**既提升收益又不恶化回撤**才允许开。过不了这关就一直关着（代码留着，等数据）。

当前运行状态：**SIMULATE（模拟盘）**，跑在一台专用 Mac mini 上，计划稳定后再小额切 REAL。

---

## 进程模型（怎么跑起来的）

系统由**三个独立进程**组成，互不为对方的子进程：

```
┌────────────────────┐     行情 + 下单      ┌──────────────────────────┐
│   MooMoo OpenD     │◄────────────────────│  ① 调度器 scheduler       │
│ (券商网关 :11111)  │                     │  python -m src.main run   │
└────────────────────┘                     │  ・APScheduler 主循环     │
          ▲                                 │  ・每 30min 扫描+下单     │
          │ 行情                            │  ・5min / 60s 管仓        │
          │                                 │  ・所有定时任务           │
┌────────────────────┐    读 data/*.json    └────────────┬─────────────┘
│  ② Web 仪表盘       │◄───────────────────────────────── │ 写 SQLite + json
│  web/server.py     │     共享 data/ 状态                │
│  (Flask :8770)     │──── 写 .env / 审批 ───────────────►│
└────────────────────┘                                    │
          ▲                                                ▼
          │ 手机/电脑浏览器                        Telegram 推送 + 审批
          └──────────────── ③ Telegram bot ◄──────────────┘
```

- **OpenD**：富途/moomoo 官方本地网关，负责行情和下单，必须全程运行（监听 `127.0.0.1:11111`）。
- **① 调度器**（`python -m src.main run`）：真正的交易大脑。APScheduler 驱动所有扫描、管仓、定时任务。用 `start-scheduler.command` 启动（`caffeinate` 防睡眠 + `nohup` 脱离终端 + 顺带拉起菜单栏状态图标）。
- **② Web 仪表盘**（`web/server.py`）：只读监控 + 控制面板。**不交易**，只读 `data/` 里的快照、改 `.env`、处理审批。用 `start-web.command`（本机）或 `start-web-lan.command`（局域网，手机可看，需先设 `WEB_PASSWORD`）启动。
- **③ Telegram**：推送下单/止损/状态，并提供「批准/拒绝」按钮处理审批队列。

> 调度器和 Web 是**两个进程**：改了后端代码要分别重启对应进程。调度器停了不影响 Web 能看历史，但不会再交易。

调度器与 Web 之间不直接通信——通过共享的 `data/`（SQLite `trader.db` + 若干 json 快照）和 `.env` 解耦。Web 改模式/预算/审批，实际上是写一个 db-state override 或 `.env`，由调度器在下一轮扫描读到生效（热参数无需重启）。

---

## 快速上手

### Step 0. 本地依赖（装一次）

| 依赖 | 用途 | Mac 安装 |
|------|------|---------|
| **Python 3.11+** | 跑代码 | `brew install python@3.11` |
| **MooMoo OpenD** | 行情 + 下单网关 | [openapi.moomoo.com](https://openapi.moomoo.com) 下载 .dmg |
| **MooMoo App** | 开户 + 解锁 OpenD 交易 | App Store 搜 "moomoo" |

**外部 API 账户**（都有免费层）：

| 服务 | 用途 | 注册 | 免费额度 |
|------|------|------|---------|
| MooMoo OpenAPI | 行情 + 交易 | [openapi.moomoo.com](https://openapi.moomoo.com) | 免费 |
| Google Gemini | AI 复核 / 优化器 | [aistudio.google.com](https://aistudio.google.com) | 1500 req/day |
| Tavily | 新闻搜索（喂给 AI） | [app.tavily.com](https://app.tavily.com) | 1000 req/月 |
| Telegram Bot | 通知 + 审批 | [@BotFather](https://t.me/BotFather) | 免费 |

### Step 1–2. MooMoo 开户 + 装 OpenD

1. moomoo App 注册、开户（1–3 工作日审核）、在 App 内开通 **Paper Trading** 账户。
2. 装 OpenD（默认监听 `127.0.0.1:11111`，首次需短信验证码）。**交易密码是 6 位数字**，和登录密码分开（App → 我的 → 设置 → 交易密码）。OpenD 必须**全程运行**。

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
# 编辑 .env，至少填：
#   MOOMOO_TRADE_PWD       6 位交易密码
#   GEMINI_API_KEYS        逗号分隔，可多 key 轮换
#   TAVILY_API_KEY         新闻搜索
#   TELEGRAM_TOKEN/CHAT_ID @BotFather 创建
#   ACCOUNT_USD            你的预算上限（程序绝不超过这个数投入）
```

详见 [`.env` 配置全解](#env-配置全解开--关--为什么)。

### Step 5. 启动

```bash
# 调度器（交易大脑）—— 推荐用 .command 双击启动（防睡眠 + 脱离终端）
./start-scheduler.command
#   或手动：python -m src.main run

# Web 仪表盘（监控/控制，另一个进程）
./start-web.command            # 本机 http://127.0.0.1:8770
./start-web-lan.command        # 局域网，手机可看（需先在设置里设 WEB_PASSWORD）

# 单次扫描调试（不进循环）
python -m src.main scan
```

启动后调度器会：检测关机期间错过的定时任务并补跑 → 美股开盘后每 30 分钟扫一次 → Telegram 推送下单/止损/状态。

---

## 买入评估：策略 + 权重

对 watchlist 每只股票，用 **4 套独立策略**各打一个 0–100 分，取分数最高的信号代表该股票。4 套用**同一个 `Signal` 接口**，所以后面的漏斗完全不在乎是哪套打出来的（只记一个 `strategy` 标签）。

> **当前实盘只跑前两套（趋势 + 动量突破）；均值回归、形态识别默认关**（原因见 [`.env` 全解](#env-配置全解开--关--为什么)）。

> **名词速记**：**EMA** 指数移动均线（近期价权重更高）｜**MACD** 快慢均线差，判动量转向｜**RSI** 相对强弱 0–100，>70 超买 <30 超卖｜**Stochastic** 随机指标，收盘价在近期高低区间的位置｜**ADX** 平均趋向指数，衡量趋势「强不强」（非方向）｜**VWAP** 成交量加权均价（机构成本线）｜**BB** 布林带，均线 ±2σ 通道｜**ATR** 真实波幅均值，一根 K 线平均波动多少钱，用来定止损/止盈距离。

### 每套策略权重总和 = 90，剩 10 概念上属于 AI

历史上 AI 复核占最后 10 分；现在 AI 改成独立的「建议/否决」层不再并入分数，所以**实际综合分上限 ≈ 90，入场门槛 70**。

#### 策略 1 — 趋势（`indicators.py`，实盘 HOUR_1 权重）

| 因子 | 含义 | 满分条件 | 权重 |
|------|------|---------|:--:|
| trend 趋势 | EMA9 > EMA21 且斜率向上 | 金叉+向上 100 / 走平 60 / 死叉 0 | **18** |
| momentum 动量 | MACD 金叉 + RSI 40–70 + Stochastic 20–80 | 三者齐全 100 / 部分 70/40 | **18** |
| volume 量能 | 当根量 ÷ 20 根均量 | ≥1.5× → 100 / ≥1.0× → 50 | **14** |
| pattern 形态 | 突破 20 根新高 / 布林下轨反弹 | 突破 100 / 下轨反弹 90 / 收红 40 | **14** |
| adx 趋势强度 | ADX 值 | ≥25 → 100 / ≥20 → 70 / ≥15 → 30 | **13** |
| vwap | 收盘价 vs 20 周期滚动 VWAP | 高于 +0.5% → 100 / 持平 60 | **13** |

#### 策略 2 — 动量突破（`strategy_momentum.py`）
更挑，专抓爆发性突破（出手少，但单笔常常很大）：

| 因子 | 满分条件 | 权重 |
|------|---------|:--:|
| breakout 突破 | 收盘超 20 根高点 ≥ 0.3×ATR | **30** |
| volume 量能 | ≥ 2× 均量 | **25** |
| adx_trend | ADX ≥ 30（真趋势非震荡） | **20** |
| structure 结构 | EMA9 > EMA21 > EMA50 完全多头排列 | **15** |

#### 策略 3 — 均值回归（`strategy_mr.py`，**默认关**）
震荡市抄底反弹：oversold 超卖（RSI<25 + 跌破布林下轨）25 / reversal 反转（锤子线 / 收复下轨）25 / volume 20 / non_trend（ADX<25 才有效）20。

#### 策略 4 — 形态识别（`strategy_pattern.py`，**默认关**）
几何 + K 线形态（双底、上升/对称三角、头肩底、下降楔形、牛旗、区间突破、锤子/吞没/晨星）：pattern_quality 形态质量（检测器置信度）30 / trigger 是否已突破关键位 25 / volume 20 / trend_alignment 15。可选 `pattern_vision`（Gemini 渲染 K 线图后看图确认），也默认关。

**入场门槛**：综合分 ≥ `ENTRY_SCORE_THRESHOLD`（实盘 **70**，Optuna 调出）。硬门槛，无「边缘带」，所以分数没到 70 的连漏斗都进不去。

---

## 候选漏斗（一连串过滤闸）

打分 ≥ 70 的信号按分数从高到低，逐一过下面的闸，**任何一道不过就跳过该股票**：

| # | 闸 | 规则 | 模块 |
|---|----|------|------|
| 1 | 交易时段 | 09:45–15:30 ET；周五 14:00 后不开新仓（防周末跳空） | `kill_switch.in_trade_phase` |
| 2 | 市场环境 regime | SPY 同时跌破 50 & 200 日均线 = BEAR → 禁开新仓 | `regime.assess` |
| 3 | 自适应黑名单 | 近期连亏股在观察期内 → 跳过 | `blacklist` |
| 4 | 熔断 kill_switch | 日内回撤 / 账户 DD 停盘 / 3 连亏 → 禁开 | `kill_switch.evaluate` |
| 5 | MTF 多周期确认 | 日线 EMA20 > EMA50（HOUR_1 模式必查） | `indicators.daily_trend_bullish` |
| 6 | 隔夜跳空 | \|跳空\| > `MAX_GAP_PCT`(4.0%) → 拒（追高/接刀） | `indicators.check_gap` |
| 7 | 财报屏蔽 | 距下次财报 ≤ 2 天 → 拒 | `earnings.earnings_block` |
| 8 | 买卖价差 | bid-ask spread > 0.5% → 拒（流动性差） | `moomoo_client.get_spread_pct` |
| 9 | AI 复核 | Gemini + Tavily 新闻查「指标看不到的雷」。**默认建议模式**（不拦单），下单后才问以省延迟 | `ai_validator.validate` |
| 10 | 风控闸 | 24h 止损冷却 / 持仓数上限 / 加仓门槛 / 现金 / 预算上限 | `risk_manager.can_open_new` |
| 11 | 每轮新股上限 | `MAX_NEW_NAMES_PER_SCAN`(2)，加仓老仓不占额度 | `main.scan_once` |

> 已下线的旧闸：**ML 概率否决**（子系统已删，证明无效）、**板块集中度**（结构上永不触发，已移除调用）。

---

## 仓位计算

`risk_manager.calc_position_size` — 多层独立相乘的「按风险定股数」：

```
基础风险金额 = 可用资金 × RISK_PER_TRADE(5%)
   × 连亏降温     (2 连亏 0.75× / 3 连亏 0.5×)
   × 账户DD减仓   (账户回撤 ≥ 10% → 0.5×)
   × 自适应仓位   (近 30 笔交易日级 Sortino → 0.5×~1.25×)
   × conviction   (满分信号 1.0)
   × 顺势加压      REGIME_BULL_MULT(1.4)  ← 仅「强牛 + 低 VIX」时
按风险股数 = 风险金额 ÷ (入场价 − 止损价)
按上限股数 = 资金 × MAX_POSITION_PCT(40%) ÷ 入场价   ← 单股集中度上限
最终股数  = min(按风险, 按上限)，再按 VIX 调（VIX>25 减半，>35 砍到 1/4）
```

- **资金口径**：用「你分配的预算 `ACCOUNT_USD`」，但绝不超过实时账户净值（大模拟盘余额不会乱放大，回撤中的真账户自动缩仓）。预算可在 Web 改，下一轮扫描生效，无需重启。
- **单股上限 `MAX_POSITION_PCT=40%`**：这是真正防隔夜跳空的护栏——止损挡不住跳空（价格跳过止损位才成交），所以靠「不押太大」而非「盯得更勤」来防。

---

## 卖出 / 退出逻辑

止损/止盈都用 ATR 缩放（`executor.py` 管理）：

| 退出方式 | 触发 | 开关（当前） |
|---------|------|:--:|
| 软止损 SL | 价 ≤ 入场 − `SL_ATR_MULT`(3.5)×ATR | 常开 |
| 止盈 TP | 价 ≥ 入场 + `TP_ATR_MULT`(10.0)×ATR | 常开 |
| 分批止盈 scale-out | +3R 卖 1/3、+6R 再卖 1/3、剩 1/3 跑到 TP（R = 入场−初始止损） | ⚠️ 当前空转（各档高于满仓 TP 天花板，运行时自动 DISABLED，详见下文）|
| 保本移动止损 | 浮盈到 +1R → 止损上移到入场价（赢单不再变亏单） | `USE_BREAKEVEN_STOP` ✅ |
| 最长持仓 | 满 `MAX_HOLD_DAYS`(7) 个**交易日**强平 | 常开 |
| 跳空哨兵 | 持仓股财报 ≤1 天，或盘前 AI 判定有实锤利空 → 开盘清仓 | `GAP_SENTINEL_ENABLED` ✅ |
| 黑名单平仓 | 持仓股进黑名单 → 强平 | 常开 |
| 超额平仓 | 持仓数超过 MAX_POSITIONS → 平掉最差的 | 常开 |
| 快速止损环 | 每 `FAST_STOP_SECONDS`(60s) 只查软止损 + 保本（补 SIMULATE 无原生 STOP 单的成交延迟） | ✅ |
| 智能退出 smart_exit | 盘中 AI 实锤利空 / 浮盈中技术破位锁盈 | ❌ 默认关 |
| 停滞退出 stall-out | 几天没动的仓位提前平掉腾资金 | ❌ 默认关 |

> **SIMULATE vs REAL 的退出**：模拟盘不支持原生 STOP 单，全部用「主循环检测价≤止损 → 市价软平」。REAL 默认也用同一套软退出（`REAL_USE_SOFT_EXITS=true`）以保持和回测一致；关掉它则用券商 OCO bracket（挂硬止损+止盈，进程死了也有券商兜底，但和回测口径有差）。

**手动仓位接管**：你自己在 moomoo App 里买的票，对账时会被「收养」并标记 `user_managed`（对所有自动退出免疫），直到 `review_adopted` 判断风险——正常则交给 bot 管止损；高风险（熊市/已破位/深套/弱分/临近财报）则发 Telegram **接管审批**让你决定。

---

## 风控规则（硬约束）

AI **不能**绕过这些：

| 规则 | 阈值 | 实现 |
|------|------|------|
| 单笔风险 | 5% (`RISK_PER_TRADE`) | `calc_position_size` |
| 单股仓位 | 40% (`MAX_POSITION_PCT`) | 股数上限 |
| 同时持仓数 | 5 (`MAX_POSITIONS`，随预算缩放) | `can_open_new` |
| 日内回撤 | -6% (`DAILY_DRAWDOWN_STOP`) → 当日停 | `kill_switch` |
| 账户 DD 减仓 | ≥ 10% (`DD_SIZE_CUT_PCT`) → 0.5× | `_dd_size_multiplier` |
| 账户 DD 停盘 | ≥ 18% (`DD_HALT_PCT`)，7 天后自动解除 | `can_open_new` |
| 3 连亏 | 连续 3 天净亏 → 暂停 | `record_trade_close` |
| 止损冷却 | 同一股止损后 24h 内不再买 | `in_sl_cooldown` |
| 财报屏蔽 | ≤ 2 天 | `earnings_block` |
| Regime BEAR | 禁开新仓 | `regime.assess` |
| VIX 调仓 | >25 减半，>35 砍 1/4 | `calc_position_size` |

---

## `.env` 配置全解（开 / 关 + 为什么）

> 下表是 **SIMULATE 当前状态**。理解前述两条铁律（parity + 未验证不上线）就能理解大多数「为什么默认关」。

### 当前**开着**的关键功能

| 配置 | 值 | 说明 |
|------|----|------|
| `MOOMOO_TRADE_ENV` | `SIMULATE` | 模拟盘。切 REAL 在 Web 设置里做（2 次确认 + 交易密码 + 空仓才允许）。 |
| `GAP_SENTINEL_ENABLED` | `true` | 跳空哨兵：财报临近或实锤利空 → 收盘前/开盘清仓。 |
| `GAP_SENTINEL_AI_INTRADAY` | `true` | 盘中也跑 AI 跳空检查（默认设计是只盘前跑省钱；此处打开）。 |
| `USE_BREAKEVEN_STOP` | `true` | +1R 保本移动止损。 |
| `REAL_USE_SOFT_EXITS` | `true` | REAL 也用软退出，和回测口径一致。 |
| `DYNAMIC_UNIVERSE_ENABLED` | `true` | 每周日按 6-1 动量从流动性池选 top-N 重建 watchlist。 |
| `UNIVERSE_TOP_N` | `15` | 选 15 只（10/15/20 平台验证：15 最优）。 |
| `AUTO_APPLY_PARAMS` | `true` | 过了双窗口回测且在允许边界内的优化器建议自动应用 + 通知 + 退化自动回滚；越界的仍需人工批。 |
| `REGIME_BULL_MULT` | `1.4` | 顺势加压（仅强牛 + 低 VIX，已批准的保守倍数）。 |
| `HEALTH_CHECK_ENABLED` | `true`（默认） | 每 30min 探测 moomoo 期权订阅 + Gemini 额度，掉了边沿触发 Telegram。 |
| `FAST_STOP_SECONDS` | `60` | 快速止损环每 60 秒查一次软止损/保本。 |

### 当前**关着**的功能（及原因）

| 配置 | 状态 | 关闭原因 |
|------|------|---------|
| **`AI_VETO_BLOCKING`** | `false` | **不是怕 AI，是 parity**：诚实回测没有 AI 否决层，若 live 让 Gemini 拦单，实盘成交就和算出 $/day 的回测不是同一批了。所以 AI 照跑、记录、显示在买入卡，但不拦单；且改到**下单后**才问（避免 42–99s 延迟错失成交）。 |
| **`MR_ENABLED`** | `false` | 均值回归。combo sweep 显示在当前半导体偏多头池里净亏（约 −$4/天）。留着等转震荡。 |
| **`PATTERN_ENABLED`** | `false` | 形态策略。2026-06-23 回测：不筛选净亏（$16.4→$8.7/天，maxDD 5.8%→17%）；收紧后也只打平，**maxDD 仍翻倍**，过不了「不恶化回撤」关。无 edge，不上。 |
| **`PATTERN_VISION_ENABLED`** | `false` | Gemini 看图确认。形态策略本身就关；且 moomoo HOUR_1 历史仅 ~679 根/~150 天，凑不出第二个独立回测窗口做双窗口验证。 |
| **`SMART_EXIT_ENABLED`** | `false` | 盘中 AI/技术智能退出（Phase 2A）。未验证；隔夜跳空已由哨兵覆盖，不急叠加。 |
| **`SENTIMENT_SCORING_ENABLED`** / `SENTIMENT_SIZING` | `false` | moomoo 式看好/看空打分（Phase 2B）。纯建议、不改下单，默认关省调用。 |
| **`OPTIONS_FLOW_ENABLED`** | `false` | 期权异动。期权订阅虽已开通可用，但它只被 smart_exit / sentiment（都关着）消费——单独开它什么也不做还加延迟。等那两个开了再开。 |
| **`STALL_OUT_ENABLED`** | `false` | 停滞退出。exit parity：验证引擎没有它，且其 max-hold 桶净赚；实盘仅有的 2 次停滞退出全亏。 |
| **`USE_SCALE_OUT`** | `true` → **运行时空转** | 分批止盈。`TP1_R`=3.0 × `SL_ATR_MULT`=3.5 = +10.5 ATR ≥ 满仓 TP 天花板（`TP_ATR_MULT`=10.0 ATR，即 10.0/3.5=2.86R，原 2.29R），任何一档都打不到 → `config.py` 检测到后打印告警并按 **DISABLED** 处理。2026-06-25 sweep：把梯子压到天花板下让它真能触发，反而降 $/天（过早落袋、杀掉肥尾），故 .env 虽 `true` 但**故意保持空转**。 |

### 已**删除**（不再是开关）

- **ML / XGBoost 子系统**（2026-06-03）：证明无效，AUC≈0.5，从不否决/调仓。
- **DeepSeek**（2026-06-08）：全系统统一到 **Gemini 3.5 Flash**（owner 要求 Gemini ≥3.5-flash，不用 lite 档）。
- **DAILY 交易周期**（2026-06-07）：HOUR_1 回测完胜（$36.5 vs $22.9/天，回撤更低）。DAILY 数据仍用于 MTF/gap/SPY-regime。

### 其它关键数值（Optuna / 回测调出）

`ENTRY_SCORE_THRESHOLD=70`｜`SCAN_INTERVAL_MIN=30`｜`MAX_HOLD_DAYS=7`｜`TIMEFRAME=HOUR_1`｜`TP_ATR_MULT=10.0`｜`SL_ATR_MULT=3.5`｜`MAX_GAP_PCT=4.0`｜`RISK_PER_TRADE=0.05`｜`MAX_POSITIONS=5`｜`MAX_POSITION_PCT=0.40`｜`DAILY_DRAWDOWN_STOP=0.06`｜`DD_SIZE_CUT_PCT=10`｜`DD_HALT_PCT=18`。

---

## AI 自治系统

> 目标：95% 自动化，你只看 Telegram。

1. **自适应仓位**（`adaptive_sizing.py`）：读近 30 笔已平仓，按交易日级 Sortino 调风险倍数（>5 → 1.25× 热手加仓；2–5 → 1.0×；0–2 → 0.75×；≤0 → 0.5× 保护；<10 笔 → 1.0× 热身）。与 DD/连亏/VIX 4 层独立叠加。
2. **自适应黑名单**（`blacklist.py`）：连亏股进观察期（门槛随整体 Sortino 浮动）；恢复立即移除，还不行延长复评（最长 90 天）。持仓股若进黑名单会被强平。
3. **每周动态选股**（`universe.py`，Phase 1）：周日 22:00 ET 从流动性池 `config/universe_pool.json` 按 6-1 动量选 top-N 重建 watchlist，**走样本外**回放（避免用选股窗口本身回测的幸存者偏差）。每次变动 Telegram 通知（铁律：不静默漂移）。
4. **Gemini 优化器**（`optimizer_ai.py`）：每周日 23:00 复盘真实成交 → Gemini 提小幅参数建议 → 每条都在诚实引擎上回测（180d + 360d）→ 只有**击败基线 $/day 且不恶化回撤**才进队列。`AUTO_APPLY_PARAMS=true` 时边界内自动应用 + 退化自动回滚，越界仍需人工批。**绝不**盲目套用最新 Optuna 赢家（那是过拟合上月行情）。
5. **每月 Optuna 调参**（`optimizer.py`）：每月 1 号 03:00 TPE 贝叶斯走样本外，只 Telegram 建议，**不自动改 .env**。
6. **启动自动补任务**（`cron_state.py`）：关机期间错过的定时任务开机自动补，多次错过只补一次（coalesce）。
7. **API/订阅健康哨兵**（`health_check.py`）：期权订阅或 Gemini 额度掉了 → 边沿触发 Telegram「续订/充值」提醒（带去抖，不刷屏）。

---

## 回测 + 参数优化

```bash
# 诚实账户级回测（时间步进 portfolio simulation，与 live 行为一致）
python -m src.backtest --days 180
python -m src.backtest_v3            # v3 引擎（含 scale-out / breakeven / 动态池走样本外）

# Optuna 调参（样本外 K 折）
python -m src.optimizer --days 180 --trials 20 --folds 3 --min-trades 60
```

输出含：Win rate / Profit factor / Expectancy、Sharpe / Sortino / Calmar / MAR / Ulcer、Max DD / 水下天数、**Monte Carlo 1000× P(profitable)**、月度 PnL。

> **诚实预期**：不会月月赚。年化 ~15–25% + 最大回撤 ~10% 是真实区间。`.env` 里的各项 $/day 数字（如顺势加压、单股上限 40%、动态池）都是特定回测窗口的相对结论，不是承诺。市场结构变化（regime change）会让任何策略亏钱。

---

## 自动定时任务

| 时间（美东 ET） | 任务 | 模块 |
|----------------|------|------|
| 每 30 分钟（:01/:31，交易时段）| 扫描信号 + 管仓 + 下单 | `main.run_loop` |
| 每 5 分钟（盘中持仓时）| 管仓 tick（止损/止盈/分批/最长持仓）| `_manage_tick` |
| 每 60 秒（盘中持仓时）| 快速止损环（仅软止损+保本）| `_fast_stop_tick` |
| 工作日 09:00 | 盘前跳空哨兵分析 | `_premarket_gap_sentinel_job` |
| 工作日 09:31 | 开盘清仓被标记的跳空风险股 | `_open_gap_exit_job` |
| 每 30 分钟 + 启动 | API/订阅健康检查 | `_api_health_job` |
| 每天 23:00 | 黑名单复评 | `_daily_blacklist_review_job` |
| 周日 22:00 | 动态选股刷新（Phase 1）| `_universe_refresh_job` |
| 周日 22:30 | 90 天回测健康检查 | `_weekly_backtest_validation_job` |
| 周日 23:00 | 复盘真实成交 + 优化器建议 | `_weekly_self_review_job` |
| 每月 1 号 03:00 | Optuna 调参（仅建议）| `_monthly_optuna_job` |
| 工作日 17:30 | Autopilot 健康守护 | `_watchdog_job` |
| 每 5 分钟 | Telegram 审批同步 | `_tg_sync` |
| **启动时** | 错过任务自动补 | `cron_state` |

---

## 审批闸（铁律）

任何**非静默的改动**（参数变更、手动仓位接管、优化器建议）都必须经过审批队列（`approvals.py` + `tg_approvals.py`）才会执行——这是整个系统唯一的「变更入口」。

- 边界：**bot 自己的单子 → bot 自己决定**（从不问你）；只有**你手动下的单**才可能触发 Telegram 决策。
- 审批可在 Web 面板或 Telegram「批准/拒绝」按钮处理；批准后下一轮扫描（或每 5 分钟的 `_tg_sync`）生效。

---

## 项目结构

```
moomoo-trader/
├── src/
│   ├── main.py              # 入口：扫描 + 调度 + catchup（python -m src.main run/scan）
│   ├── config.py            # .env → settings（含各功能开关 + 默认）
│   ├── moomoo_client.py     # MooMoo OpenD 封装（行情/下单/快照）
│   ├── timeframe.py         # 时间框架预设（HOUR_1 实盘 / MIN_30 / MIN_10）
│   │
│   ├── indicators.py        # 策略1 趋势（6 因子打分）+ MTF/gap/RS 辅助
│   ├── strategy_momentum.py # 策略2 动量突破
│   ├── strategy_mr.py       # 策略3 均值回归（默认关）
│   ├── strategy_pattern.py  # 策略4 形态识别（默认关）
│   ├── pattern_detect.py    # 纯 numpy 几何/K线形态检测
│   ├── pattern_vision.py    # Gemini 看图确认（默认关）
│   │
│   ├── ai_validator.py      # Gemini+Tavily：买入复核 / 跳空 / 智能退出 / 情绪打分
│   ├── regime.py            # SPY 50/200MA 市场环境
│   ├── earnings.py          # 财报屏蔽（yfinance）
│   ├── gap_sentinel.py      # 跳空哨兵（财报层 + AI 层）
│   ├── smart_exit.py        # 盘中智能退出（默认关）
│   ├── options_flow.py      # 期权异动（默认关，需订阅）
│   │
│   ├── risk_manager.py      # 仓位计算 + 硬风控 + DD 断路器
│   ├── adaptive_sizing.py   # 自适应 RISK_PER_TRADE
│   ├── blacklist.py         # 自适应黑名单
│   ├── kill_switch.py       # 统一熔断（时段/regime/halt/回撤）
│   ├── portfolio.py         # 组合热度 + R-multiple
│   │
│   ├── executor.py          # 下单 + OCO/软退出 + 分批/保本/最长持仓
│   ├── reconcile.py         # 券商 vs 本地对账（含孤儿仓收养）
│   ├── manual_positions.py  # 手动仓位接管审批
│   ├── approvals.py / tg_approvals.py  # 审批队列 + Telegram 同步
│   ├── notifier.py          # Telegram 推送
│   ├── audit.py             # 每次决策审计
│   │
│   ├── universe.py          # 动态选股（Phase 1）
│   ├── backtest.py / backtest_v3.py    # 诚实账户级回测引擎
│   ├── optimizer.py         # Optuna 调参
│   ├── optimizer_ai.py      # Gemini 优化器（建议→回测→审批）
│   ├── self_review.py / self_improve.py / autopilot.py  # 自治复盘/改进/守护
│   ├── health_check.py      # API/订阅健康哨兵
│   ├── metrics.py           # Sharpe/Sortino/MC 等指标
│   ├── cron_state.py        # 错过任务自动补
│   ├── runtime_config.py    # 热参数 db-state override（无需重启）
│   ├── db.py                # SQLite（trader.db）
│   ├── clock.py             # NY 时间 + NTP 校时 + NYSE 日历
│   ├── menubar_app.py       # macOS 菜单栏状态图标
│   └── ...
│
├── web/
│   ├── server.py            # Flask 仪表盘后端（监控/改 .env/审批）
│   └── static/index.html    # 单页前端
├── config/
│   ├── watchlist.json       # 自动生成的交易池（勿手改，改 pool 或 TOP_N）
│   └── universe_pool.json   # 流动性候选池（选股的输入）
├── data/                    # 运行时状态（gitignored）：trader.db / *.jsonl / *.json
├── logs/                    # 运行日志（gitignored）
├── scripts/                 # 一次性回测/验证脚本（sweep / ablation / test_*）
│
├── start-scheduler.command  # 启动调度器（caffeinate 防睡眠）
├── start-web.command        # 启动 Web（本机）
├── start-web-lan.command    # 启动 Web（局域网，手机可看）
├── requirements.txt
├── .env.example             # 配置模板（含每项详细注释）
└── README.md
```

---

## 上线 checklist

> 不要跳步骤。每一项都是为了保护你的钱。

- [ ] OpenD 在 Paper Trading 账户跑通（Web 状态栏全绿）
- [ ] `python -m src.main scan` 看到信号被评分
- [ ] Telegram 收到测试推送
- [ ] `python -m src.backtest --days 180`，看 Sortino > 2 且 P(profitable) > 90%
- [ ] **Paper trade 至少 4 周**，记录每笔
- [ ] 实盘 Sortino > 1.5 且 win_rate > 55%
- [ ] Web 设置里切 REAL（2 次确认 + 交易密码 + 空仓）
- [ ] 首周只投 **USD $500** 验证下单链路
- [ ] 稳定两周后逐步加到目标资金
- [ ] 每月看一次 Optuna / 优化器建议

---

## 常见问题

**Q1：OpenD 启动后要求「解锁交易」？**
GUI 版 OpenD 不能通过 API 解锁，需手动点一次 OpenD 窗口的「解锁交易」。之后 bot 用 MD5 哈希后的 `MOOMOO_TRADE_PWD` 自动登录。

**Q2：Paper Trading 不支持 STOP 单？**
是的。模拟盘没有原生 STOP 单，bot 用「主循环检测价≤止损 → 市价软平」，并有 60 秒快速止损环把延迟压到秒级。REAL 默认也用这套软退出以对齐回测（`REAL_USE_SOFT_EXITS=true`）；关掉则改用券商 OCO bracket。

**Q3：调度器和 Web 是同一个进程吗？**
不是。**两个独立进程**。调度器（`src.main run`）交易；Web（`web/server.py`）只监控/控制。改后端代码要分别重启。Web 停了不影响交易。

**Q4：Gemini 配额会爆吗？**
免费层 1500 RPD。买入 AI 复核每轮最多 10 次，交易时段 ~12 轮/天，远在额度内。AI 是 FAIL-SAFE：额度耗尽就降级为中性「pass」，绝不因 AI 不可用而乱卖。

**Q5：DD 断路器和 3 连亏熔断的区别？**
3 连亏：连续 3 天净亏 → 暂停（按天）。DD 断路器：账户总回撤 ≥10% 半仓、≥18% 停盘（按账户级 peak，7 天后自动解除）。两者独立叠加。

**Q6：watchlist 能手改吗？**
不能直接改 `config/watchlist.json`（每周自动覆盖）。要调就改流动性池 `config/universe_pool.json` 或 `UNIVERSE_TOP_N`。

**Q7：Mac 睡眠会断 OpenD 吗？**
会。`start-scheduler.command` 用 `caffeinate -is` 防系统睡眠（屏幕可睡）。建议接电源、专机常驻。

**Q8：为什么这么多功能默认关？**
两条铁律：①live 必须和回测口径一致；②没通过双窗口回测（提升收益且不恶化回撤）的功能不上线。关着的代码都留着，等数据够了再验证开启。详见 [`.env` 全解](#env-配置全解开--关--为什么)。

---

## 技术栈

```
Python 3.11 + APScheduler（调度）
MooMoo OpenAPI SDK（行情 + 交易）
Optuna（贝叶斯调参）
Google Gemini 3.5 Flash（AI 复核 / 优化器 / 看图）
Tavily（新闻）
pandas + pandas-ta-classic（技术指标）
yfinance（财报 + 选股数据）
matplotlib（K 线渲染给 vision）
SQLite（持久化）
Flask（Web 仪表盘）
python-telegram-bot（通知 + 审批）
rumps（macOS 菜单栏图标）
```

---

## ⚠️ Disclaimer

This is a personal research project. It does NOT constitute financial advice.

- 交易股票存在亏损本金的实质风险；历史回测不代表未来表现。
- 盈利预期假设市场延续过去回测窗口的特征；regime change 可能让任何策略亏钱。
- 始终 paper trade ≥ 4 周再用真钱；真钱第一次只投你能完全亏掉的金额。

**作者不对任何使用本程序产生的金钱亏损负责。**
