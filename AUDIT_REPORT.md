# moomoo-trader 全codebase审计报告

> 2026-06-03 多agent审计(11子系统并行 + 对抗式kill验证 + 架构师综合)。新chat可直接读。

I'll write the master report directly. I have all the audit material and the adversarial kill-verification needed. No further file reading is required since the verification already grep-confirmed every line reference.

# moomoo-trader 总体重构报告 (Master Report)

# 1. 执行摘要

这个 bot 的真实交易路径其实很小:`src/main.py::scan_once` 一条扫描漏斗 → `indicators.evaluate`(技术 0–90 打分)→ 一串闸门 → `risk_manager` 现金墙 → `executor` 下单,诚实回测引擎只有一条 `backtest_v3.simulate_v3(enforce_cash=True)`。围绕它堆了大量**死重**:整个 ML 子系统(AUC≈0.516,等于随机,从不否决/减仓)、SEC EDGAR 内部人特征(实测零重要性)、RS/SOXX 板块闸门(实测亏 $/day)、一个永远开不了火的 sector 闸门和 heat_check、一条被丢弃输出的每周选股流水线、以及若干零调用的死函数。最大的结构性问题是**动态本金完全没实现**——所有仓位/预算/风控数学都锚定 `config.py:45` 那个静态的 `ACCOUNT_USD`(默认 4500,连 5000 都不是),实盘余额每次扫描都抓了却只用于单笔买得起的检查,改本金要手动改 `.env` 再重启。通往 $50/day 最真实的杠杆**不是再调那 5 个已榨干的参数**(现金墙锁死在 ~2 仓,加仓/加票只稀释质量),而是三件事:(a) 真本金动态化,(b) 把 `portfolio.record_close` 已经在记的真实成交喂进**每周真实成交自省 + half-Kelly 风险建议**循环,(c) 用「analyze→notify→approve→execute」默认 suggest-mode 把所有自动动作收口到 owner 审批。复活 ML 只在「换标签/换 target/在线学习」有具体新边际时才值得,且必须先修标签保真缺口。

# 2. 架构地图

**Live 交易路径 (end-to-end):**

```
gui_qt.py (subprocess.Popen ["python","-m","src.main","run"])
   │
   └─► main.run_loop()  [BlockingScheduler @ NY tz]
          ├─ scan job (interval 15min, 仅 in_market_hours)
          │     └─► scan_once():
          │           1. clock.ny_now()  → 时间基准
          │           2. c.get_account_cash() + get_positions()   ← 抓到真余额(但只当现金墙天花板)
          │           3. reconcile(positions, auto_fix=True)       ← ⚠ 静默改仓
          │           4. kill_switch.in_trade_phase / evaluate(regime_block) → 时间/regime/halt/DD 总闸
          │           5. executor.manage_open_trades()             ← 管理已有仓:OCO/scale-out/stall/max-hold
          │           6. 对每个 watchlist ticker:
          │                indicators.evaluate (trend) + strategy_momentum.evaluate → max() 取高分
          │                gates: MTF(daily_trend_bullish) → check_gap → sector(几乎不开火)
          │                       → earnings_block → spread → ML(inert) → ai_validator(布尔否决,预算10)
          │                → risk_manager.can_open_new + calc_position_size → portfolio.heat_check(永不binding)
          │                → executor.open_position (limit BUY + REAL OCO bracket)
          │           7. 写 data/account.json (GUI 心跳) + history.append + audit.record
          └─ 5 个 cron: 月度ML重训 / 周度选股刷新 / 周度回测校验 / 月度Optuna / 日度黑名单复盘
                + 启动时 _run_catchup_on_startup (补跑漏掉的 cron)
```

**支撑系统精炼地图:**
- **持久化/状态**: `db.py`(SQLite WAL,5 表,`atomic_state` 安全写,closed_trades 是自省的底料);`history.py`/`audit.py`(双写 SQLite+JSONL,GUI 读 JSONL)。
- **风控**: `kill_switch`(时间/regime/halt 总闸)、`risk_manager`(现金墙+多层减仓乘子)、`adaptive_sizing`(Sortino 乘子,数学被 hardcoded 4500 打坏)、`portfolio`(record_close 账本 + 永不binding 的 heat_check)。
- **券商 I/O**: `moomoo_client`(唯一 broker 接口 + 限流 + SIMULATE/REAL 开关)、`executor`(下单/出场,REAL 与 SIMULATE 两套出场逻辑有保真缺口)。
- **反馈**: `notifier`(唯一 Telegram 通道,**只出不进**,无审批回路)、`i18n`。
- **诚实回测/优化**: `backtest_v3`(诚实引擎)、`backtest.py`(hub + frozen oracle `simulate_time_stepped` + `_run_live_engine`)、`metrics`、`optimizer`(已正确接诚实引擎,suggest-only)。
- **告警/盘前(不参与下单)**: `signal_reporter`(独立进程,自带第二套指标栈+第二份 watchlist)、`finbert_sentiment`、`news_fetcher`、`glossary`、`sector` 的查表部分。

# 3. KILL LIST (已对抗验证)

仅列出 kill-verification **确认安全**的条目。验证把若干 R4 次级项从 delete 降级为 KEEP(见末尾),此处尊重该降级。

## A. 立即 DELETE — 零调用、删除不改变任何 live 行为

| module/target | action | reason | caveat |
|---|---|---|---|
| `backtest.py::backtest_ticker` (~220行) | delete | 死函数,src/scripts/GUI/字符串全无调用,只有注释自引用;`simulate_with_cache` 路由到 `simulate_time_stepped` 而非它 | 无 |
| `notifier.py::send_html` | delete | 零外部调用;其 docstring 称「signal_reporter 用它」是假的(实际用 `notifier.send`) | 无 |
| `glossary.py::all_categories` / `all_entries` | delete | 零调用;HelpDialog 只用 `GLOSSARY`+`get_entry` | 无 |
| `blacklist.py::force_review` / `manual_remove` | delete | 零调用;号称「for GUI」但从未接线(也删掉 `:17` 过时 docstring) | 或改为接一个 GUI 审批卡——owner 决定;删除不改 live 行为 |
| `indicators.py::min30_trend_bullish` | delete | 死函数,零调用(仅 `:8` docstring 提及) | 它是核心文件里的函数,不是独立模块 |

## B. 立即 DISABLE — 停注册/停调用即可,不动 import(可逆,比 delete 破坏小)

| module/target | action | reason | caveat |
|---|---|---|---|
| cron `_monthly_retrain_job` | disable cron | **铁律优先项**:`save_model()` 无质量/审批闸静默覆盖 live `model.joblib`,连 ML_ENABLED=false 都照跑 | 注销 `main.py:925` add_job + 删 catch-up `main.py:858` + 从 `cron_state.KNOWN_JOBS` 去掉 `"ml_retrain"`;**保留** `ml/train.py` 代码 + 隐藏 GUI Train 按钮 `gui_qt.py:2037` |
| cron `_weekly_watchlist_refresh_job` | disable cron | **铁律优先项**:`refresh()` 会重写 `config/watchlist.json`(`watchlist_updater.py:266`),可静默换掉钉死宇宙 | 注销 `main.py:930` + catch-up `main.py:852` + 去 KNOWN_JOBS 键;**保留**模块 + 手动 GUI "Refresh WL" 按钮(owner 发起=可接受) |
| `portfolio.heat_check` 闸 | disable gate | cap=20%×4500=$900,2 仓现金墙下真实 open-risk ≈$180,永远开不了火 | gate/移除 `main.py:569` 调用点,**保留函数**(唯一 heat 原语);硬编码 0.20 违反动态本金 |
| `sector.check_sector_exposure` 闸 | disable gate | 7 semis+3 tech vs 2 仓墙、MAX_PER_SECTOR=5,永远 binding 不了 | 移除 `main.py:504` 调用 + import `main.py:30`;**必须保留** `SECTOR_MAP`/`SECTOR_TO_ETF`/`get_sector`/`get_sector_etf`(5 个 live 消费者) |
| `finbert_sentiment` 默认加载 | disable load | 仅经手动触发的 signal_reporter 子进程可达,产出是盘前卡一行装饰,零 $/day | 强制 `is_available()` 返 False;`signal_reporter.py:918` 已优雅降级;**不删文件** |
| `ml/predict.py` live veto/conviction 调用点 | strip call sites | 模块 inert(ML_ENABLED=false ⇒ predict_proba 永不跑),但在 main 里 veto/减仓接线是死戏 | 删 `main.py:399-402,524-538`;**保留** `predict_proba/is_available/model_meta` 作为未来学习 stub;**保留** `main.py:27` import(否则启动崩) |

## C. 多文件 rewire 后可 DELETE(可辩护但需先改多处)

| module/target | action | reason | caveat |
|---|---|---|---|
| `sec_edgar.py` (整模块 + insider 管道) | delete (with rewire) | 实测零重要性、零 $/day;唯一出口是已禁用的 ML;盘前卡内部人走 yfinance 不调它 | 3 个 import 站点都是 try/except→0.0,删不会崩;但**必须先**:从 `ml/features.py:80` 去掉 `insider_30d_net_log`、删注入块 `backtest.py:1141-1171`/`predict.py:138-152,177-178`、删/中和 `scripts/insider_ablation.py` + `train_lgbm_compare.py:128-160` |

**验证降级为 KEEP(不要删)**:`relative_strength`/`sector_regime_bullish`(`backtest_v3.py:200,373,382` 仍 flag-gated 调用)、`MIN_10`/`MIN_30` timeframe 预设(`backtest.py:1011,1014` `_TF_BY_NAME` + `main.py:35` 引用)、clock drift-correction(`ny_now()` 全局 live,只能 demote 不能删)、`strategy_mr.py`(`main.py:22` 无条件 top-level import,删会让 bot 起不来)。这些是「在保留文件内剪枝」,不是独立模块 kill。

# 4. KEEP — 核心 (the modules that ARE the bot)

- **`main.py::scan_once` / `run_loop`** — 交易漏斗本体 + 调度宿主。这是 THE process。(simplify,不删:去掉重复 50 行 snapshot、死 `ai_score`、ML 死戏)
- **`indicators.py`** — 唯一被诚实引擎打分的 alpha;`evaluate` + `check_gap` + `daily_trend_bullish`(MTF)load-bearing。~$40/$23 基本就是这个模块。
- **`risk_manager.py`** — 唯一执行现金墙 + 单笔风险的硬闸;SL-cooldown 已被证实避免 -$425 rebleed;budget cap 是诚实引擎之所以诚实的原因。
- **`backtest_v3.py` + `backtest.py::_run_live_engine` + `metrics.py`** — 唯一诚实 $/day 来源;frozen oracle 与诚实引擎的隔离是本仓最强设计属性,必须保留。
- **`optimizer.py`** — 已正确接诚实引擎、suggest-to-Telegram 不自动写 `.env`,是未来 AI 循环的天然挂载点。
- **`kill_switch.py`** — 时间/regime/halt/DD 单一真相源(已消除 ghost-halt bug)。
- **`db.py`** — 唯一事务真相源;`closed_trades` 是自省循环的底料;`atomic_state` 是 AI 建议安全写入路径。
- **`blacklist.py`** — 唯一从「真实已实现 PnL」反馈到宇宙选择的活机制,自省循环的胚胎。analyze→notify 已有,值得 EXTEND。
- **`portfolio.record_close` / `trade_stats`** — 每次平仓记 R-multiple/MFE/MAE/strategy 的账本,每个学习循环都依赖它。
- **`executor.py`** — 唯一下真单的路径(simplify:统一 SIMULATE/REAL 两套出场)。
- **`moomoo_client.py`** — 唯一 broker I/O + 限流 + env 开关。
- **`reconcile.py`** — 漂移整合安全检查(simplify:改 suggest-mode)。
- **`notifier.py`** — 强制反馈通道(铁律的载体,需加 inbound 审批)。
- **`clock.py::ny_now` / `cron_state.py` / `audit.py` / `history.py` / `config.py` / `i18n.py` / `earnings.py`** — 时间/补跑/日志/配置/语言/财报闸的必要基础设施。`earnings_block` 是唯一在诚实引擎里 ON 且被计数、且无证据是 net drag 的宇宙闸,保留。

# 5. 改进路线图 (toward $50/day, AI-driven, self-improving)

**诚实前提**:现金墙锁在 ~2 仓,调那 5 个标量(threshold/tp/sl/gap/slip)已榨干;加 size 或加票只稀释质量。以下路线**不重走**已证伪的死路:ML 现状 inert、RS/SOXX 闸亏 $/day、insider 零重要性。

| # | title | rationale | effort | impact | phase |
|---|---|---|---|---|---|
| 1 | **真本金动态化** | `config.account_usd` 静态 4500,所有 sizing/budget/heat/DD 锚定它;实盘余额已抓但只当现金墙天花板。换成 `equity()=get_account_cash()+持仓MTM`,全链自动重算 | med | high(满足 directive #6,移除最普遍硬编码) | **now** |
| 2 | **修保真缺口** | (a) honest 引擎只打 trend,live 跑 trend+momentum_break → $40/$23 描述的不是在跑的 bot;(b) REAL bracket 路径无 scale-out/trailing 而 SIMULATE 有 → 翻 REAL 时 $/day 数字立即失效。统一一套出场引擎 + 让 `_run_live_engine` 打与 live 相同策略集 | med | high(没修则任何 $50 决策都建在错的回测上) | **now** |
| 3 | **每周真实成交自省循环** | `_weekly_backtest_validation_job` 复盘的是回测不是真单(#7 未满足)。复用 `portfolio.record_close` 账本 + `audit.gate_summary`,按 strategy/symbol/regime/hour 算真实 $/day,diff 回测 vs 实盘,产建议(analyze→notify→approve) | med | high(通往 $50 最诚实的路 + 保真校验) | **now** |
| 4 | **inbound 审批通道** | notifier 只出不进;suggest→approve→execute 需要 Telegram inline 按钮/GUI 审批写 pending-action 文件供 scan loop 消费。同一 handler 预留给未来 Anthropic 后台循环 | med | high(directive #9 的执行半边,目前完全缺) | **now** |
| 5 | **adaptive_sizing 重建为自学习风险脑** | 现状被 hardcoded 4500 打坏、统计欠功率、静默改 qty。重建为:用 live equity + 真实 R-multiple/胜率 + half-Kelly 估边际,**建议** `risk_per_trade` 让 owner 批。这是本子系统唯一可能出新边际而不重走死路的地方,Anthropic 接口挂这里 | high | high | **next** |
| 6 | **因子权重自学习重拟合** | 因子权重(`indicators.py:24-48`)是手设+Optuna 冻结到「上月 regime」。每周对真实成交做 logistic/GBM 重拟合(label=命中 TP1 vs SL)让因子重要性随市场漂移——这是 session 说旧 ML 缺的「真新边际」(新 label=真实成交结果、在线/每周)。suggest-mode | med | high | **next** |
| 7 | **reconcile 改 suggest-mode + ATR 派生止损** | 现 `auto_fix=True` 静默改仓、用 `cost×0.965` 魔法常数(违反铁律 + 自相矛盾 docstring)。默认 detect-only,提案推审批,止损走 risk_manager 的 ATR | low | med | **next** |
| 8 | **regime/VIX 连续风险乘子** | 把 regime 从二元 BEAR 闸变成连续 risk-budget 标量(SPY 趋势+低 VIX 对齐时加注),给自省循环一个干净旋钮。analyze→notify→approve | med | med | **next** |
| 9 | **clamp Optuna 的 9.99 sentinel** | `sortino`/`calmar` 在 <2 亏损日返 9.99,会让低成交少亏损的参数集靠 sentinel 假赢——自省循环里的过拟合通道 | low | med | **next** |
| 10 | **宇宙选择改 PnL 驱动 + 合并黑名单** | `watchlist_updater` 跑了选股但输出被丢弃(TARGET_SIZE 钉死)。重写成按真实 per-symbol expectancy 排序、提 add/drop 建议;与 blacklist 合并成一张「每周宇宙复盘」审批卡(受现金墙约束,边际有限但诚实) | med | med | **later** |
| 11 | **(投机)ML 复活 — 仅在换标签后** | **明确投机**:仅当 label 重定义为日内尺度(几根 bar 内 +X bps before −Y bps,对齐 live TP/SL 8.0/3.5 而非死的 1.5/2.0)+ 在线/增量学习 + 击败 AUC 0.55 OOS + owner 审批候选模型,才值得碰。否则 ML 仍是死重 | high | 不确定 | **later** |

# 6. AI 化 + 自我提升 重设计愿景

立足现有 + 可救活的部分,不空想。

**(a) 动态本金 — params 从 live balance 派生。** 现状根因:`config.py:45` `account_usd: float = _float("ACCOUNT_USD", 4500)`,且 `@dataclass(frozen=True)` 在 import 时读一次永不刷新;所有 sizing/cap/DD/heat(`risk_manager.py:102,183,190,368`、`portfolio.py:59`、`adaptive_sizing.py:78` 的 `/4500.0`、`backtest.py:89,956`)全锚定它。实盘余额 `main.py:84,227 c.get_account_cash()` 抓了却只用于 `risk_manager.py:359` 单笔买得起的检查 + GUI 显示。GUI「Edit Budget」(`gui_qt.py:989-1013`)只是把 `ACCOUNT_USD=` 写回 `.env`,要重启才生效——这正是要废掉的反模式。**改法**:加单一 `equity()` helper = 实盘 `get_account_cash()` + 持仓 MTM(broker 已返 `nominal_price`×qty,注意 `get_account_cash` 只返 cash 字段不含净清算,必须自己加持仓市值),每次扫描注入 sizing/cap/heat/DD;owner 改本金或盈利复利时全部自动重算,notify-on-change。`.env ACCOUNT_USD` 只保留为可选硬天花板。

**(b) 每周真实成交自省 loop。** 底料已存在:`portfolio.record_close` 每次平仓写 R-multiple/MFE/MAE/strategy 进 SQLite;`audit.gate_summary` 已量化「哪个闸杀了最多 setup」(若 `insufficient_cash`/`budget_cap` 居首即量化了现金墙);`ml/calibration.py` 已按分数桶真实成交(把它从「ML proba 校准」泛化成自省引擎)。新增周度 job:对 broker 真实 closed_trades 算 `metrics.py` 同一套指标,diff 回测预期 vs 实盘 $/day,按 strategy/symbol/regime/hour 拆「哪些 setup 在赔钱」,产**建议**(调阈值/TP/SL/换票),经 `cron_state` 保证笔记本关机的周末也能补跑。这复用全部现有 infra(audit/blacklist/notifier/calibration/cron_state),不需要 ML 就能立刻产出价值。

**(c) Anthropic API 自主优化接口(预留,默认 suggest-mode)。** 挂载点已就绪:`optimizer.run_study → suggest-to-Telegram 不写 .env` 已是正确的 analyze→notify 骨架。预留方式:在 `db.kv_state` 开一个 `pending_suggestions` (JSON) 键,用 `db.atomic_state(fn)` 作为 AI 循环 staging 写入的安全路径;`ml/predict.py` 保留单一 `predict_proba` + 加一个薄 `suggest()` 接口让后台 AI 把候选 param/模型写入 `pending_approval` 槽——**永不 live 直到 owner 批**。`adaptive_sizing` 重建处同样预留风险参数提案接口。

**(d) 全程 feedback 铁律 — analyze→notify→approve→execute,永不静默。** 现状:notifier 覆盖了几乎所有自动动作的*通知*(买/跳过/止损/TP/max-hold/retrain/Optuna/blacklist/catchup),但缺*审批*半边,且有几处**静默改 live 行为**违规必须修:`reconcile(auto_fix=True)` 静默改仓(#7)、`blacklist.evaluate_all` 静默 + executor 静默 force-close 黑名单仓、ML 月度重训静默覆盖模型(已在 KILL B 禁)、watchlist 周刷可静默换宇宙(已禁)、DD-halt auto-release 只 `log.warning` 不通知、`adaptive_sizing` 静默改 qty、AI veto 预算/配额耗尽后静默放行无通知。修法:全部走「检测→通知→批准→执行」,默认 suggest-mode;notifier 加 inbound 审批(#4)。

**ML 能否现实地复活?** 诚实回答:**当前形态不能**——AUC 0.516 等于随机,proba 聚在 ~0.6 永不过 0.35 veto 阈,且标签是 65-bar/~10 天 swing(对齐死的 1.5/2.0,与 live 8.0/3.5+scale-out 不符),训的是 bot 不做的交易。复活**只在**同时满足:重定义为日内 label + 在线学习 + 真实成交结果作 label + OOS 击败 AUC 0.55 + 候选模型经 owner 审批。在那之前 ML 是死重,`predict.py` 只保留为 stub。真正能动 $/day 的「AI」近期是 (b)/(5)/(6) 的本地学习 + LLM 做事后结构化周复盘,而非事前噪声否决。

# 7. 最小 GUI 控件集

hands-off owner 实际需要的最小集合(其余 ~900-1000 行诊断面板 + matplotlib 依赖删掉):

1. **Start / Stop scheduler**(已有,`gui_qt.py:908`)。
2. **Kill-switch / Halt-reset**(已有 `gui_qt.py:1015` `db.atomic_state`)——一键停开新仓 + 复位。
3. **账户金额输入**(驱动动态 sizing)——**改造**:不再写静态 `.env ACCOUNT_USD`,而是显示 live 余额并让 owner 设「风险策略」(可选硬天花板),sizing 从 `equity()` 自动派生。
4. **少量风险旋钮**:`risk_per_trade`、`ENTRY_SCORE_THRESHOLD`、一个 aggressiveness/risk-budget 滑块。其余(权重/周期/heat/streak/VIX 内部)机器调参,只读展示 + 每周 diff。
5. **状态/反馈面板**:read-only 持仓 + PnL + regime/halt 状态 + 最近自省摘要;**审批卡**(approve/reject 建议——风险参数/黑名单/宇宙变更/Optuna best_params),即 inbound 审批通道的 GUI 端。
6. **API Keys** + **单个 Watchlist 编辑器**(合并掉第二份 signal_watchlist)+ **Help**(修掉硬编码 $5000、删除描述已禁子系统的条目)。

**删除/隐藏**:Sector heat-map、ML dialog + Train 按钮(子系统已禁,Train 是 footgun)、Equity matplotlib 图(可经 Telegram 推)、Audit dialog(2-3 个关键数折进主条或 Telegram)、Sync Clock 按钮(hour-bar 不需要 sub-5s 校时)、Refresh WL 可保留为手动按钮。

# 8. 风险 & 待用户决策的开放问题

1. **保真缺口是最大风险**:$40/$23 是 SIMULATE + trend-only + 无 AI 否决的世界。REAL bracket 无 scale-out/trailing、live 多跑 momentum_break、AI veto 只在 live 拦单且诚实引擎看不见——**翻 REAL 前必须先修 #2,否则 $50 目标建在错的 baseline 上**。
2. **现金墙是硬约束**:~2 仓上限已证;路线图 #5/#6/#10 的边际都被它压顶。**待决策**:owner 是否接受 $50/day 在 $5k 现金账户上可能根本不可达,需要等本金增长(动态本金正是为此铺路)?
3. **AI 否决去留**:在 10 只钉死 mega-cap 上几乎不开火,却给每候选加 Tavily+Gemini 往返(延迟/配额风险)。**待决策**:做一次诚实开/关 ablation 后,降级为 suggest-mode 还是直接 disable?
4. **Anthropic key 何时供给**:接口可现在预留(suggest-mode 写 `kv_state`),但自主优化循环上线前需 owner 明确「哪些动作允许 AI 自动执行 vs 必须审批」的边界。
5. **`.env` vs 默认值漂移**:`DAILY_DRAWDOWN_STOP`(config 默认 0.03 vs live .env 0.06)、`MAX_POSITION_PCT`(config 默认 0.20 vs brief/.env 0.40)。**待决策**:确认 source of truth,否则任何自动派生会被陈旧默认值误导。
6. **多层减仓乘子可能过度刹车**:loss-streak × account-DD × Sortino 乘性叠加,3 连亏+10%DD 时 qty 砍到 0.125×,可能在已被现金墙锁死的引擎上进一步饿死它够不到 $50 所需的 size。**待决策**:是否收敛为单一减仓乘子?
7. **OCO 部分成交 bug**:`is_order_filled` 仅认 `FILLED_ALL`,`FILLED_PART` 当未成交 → 不取消对侧腿,有双重成交风险;soft-stop「市价」实为 `last×0.995` 限价,快速 gap-down 可能挂不上。翻 REAL 前需修。

—

所有引用路径(绝对):`/Users/junxian/Documents/GitHub/moomoo-trader/src/main.py`、`/src/config.py`(:45)、`/src/risk_manager.py`、`/src/adaptive_sizing.py`、`/src/portfolio.py`、`/src/executor.py`、`/src/reconcile.py`、`/src/backtest.py`(:89,330,956,1141-1171,1248-1254)、`/src/backtest_v3.py`、`/src/optimizer.py`(:36)、`/src/cron_state.py`(:36-42)、`/src/notifier.py`(:42)、`/src/glossary.py`(:1042-1047)、`/src/blacklist.py`(:249-260)、`/src/indicators.py`(:273)、`/src/sec_edgar.py`、`/src/ml/features.py`(:80)、`/src/ml/predict.py`、`/src/ml/train.py`、`/src/ml/dataset.py`、`/src/ml/calibration.py`、`/src/finbert_sentiment.py`、`/src/watchlist_updater.py`(:266)、`/src/sector.py`、`/src/signal_reporter.py`、`/gui_qt.py`(:908,989-1013,2037)。

---

# 附:对抗式 KILL 验证原文

Important nuance for Report 4's secondary kills: `relative_strength` and `sector_regime_bullish` are STILL imported and called in `backtest_v3.py:200,373,382` (flag-gated by `cfg.apply_rs_gate`/`apply_sector_regime`, default off but the code path is live-wired). `min30_trend_bullish` has zero callers (only the docstring at indicators.py:8). The MIN_10/MIN_30 timeframe presets are referenced by `backtest.py:1011,1014` (the `_TF_BY_NAME` map) and `test_min30.py` script — so they're not orphaned at import level.

These Report-4 secondary items are "prune inside a kept file" not standalone module kills, and they have live-wired (flag-gated) callers, so they're KEEP/downgrade. I have everything needed. Let me do one final confirmation of the `finbert_sentiment` GUI reachability (it IS launchable via GUI subprocess) to settle disable-vs-delete.

The signal_reporter (which imports finbert) is launchable from GUI at gui_qt.py:2169/2198 as a subprocess. So finbert is reachable, but only via the manually-triggered signal_reporter, and its output is a decorative card line. "Disable" (default no-load via `is_available()` returning False) is correct and less destructive than delete. I now have full evidence for every entry.

---

# Adversarial Kill-List Verification — moomoo-trader

Every module/function marked `delete` or `disable` across the 10 reports, grep-verified against `src/**`, `gui_qt.py`, `scripts/**`, `*.command`, `*.json`, including lazy/inline imports, dynamic refs (`getattr`/`eval`/`importlib`), and scheduler/cron registration in `src/main.py` + `src/cron_state.py`.

## Verification table

| # | module / target | proposed action (report) | importers / refs found (verbatim grep) | GUI? | cron? | CONFIRMED action | caveat / what breaks first |
|---|---|---|---|---|---|---|---|
| 1 | `src/strategy_mr.py` (whole module) | delete/archive (R4) | **top-level import** `main.py:22`; live call `main.py:396` (guarded by `settings.mr_enabled`, default false); lazy `backtest_v3.py:224`, `backtest.py:353/583`; i18n label `i18n.py:192,343`; `strategy_momentum.py:3,6` docstring | no | no | **disable (KEEP file)** | Deleting the file breaks the unconditional top-level import at `main.py:22` → bot won't start. To delete you must first remove the `main.py:22` import + `main.py:395-398` block + the 3 backtest lazy-imports. Safe path: leave module, keep `MR_ENABLED=false` (already off). Downgrade delete→disable. |
| 2 | `src/sec_edgar.py` (whole module) | delete (R6) | lazy `backtest.py:1145`, `ml/predict.py:145`, `scripts/train_lgbm_compare.py:132`, `scripts/insider_ablation.py` (purpose-built); feature listed in `ml/features.py:80` FEATURE_NAMES | no | no | **delete (with rewire)** | All 3 live/backtest import sites are `try/except → 0.0`, so deletion won't crash live or backtest. BUT `ml/features.py:80` lists `insider_30d_net_log` in `FEATURE_NAMES` (model input schema) and 2 scripts import it directly. Must first: drop the feature from `features.py`, delete the injection blocks (`backtest.py:1141-1171`, `predict.py:138-152,177-178`), and delete/neuter `scripts/insider_ablation.py` + `train_lgbm_compare.py:128-160`. Proven zero-importance → safe to delete once rewired. |
| 3 | `backtest.py::backtest_ticker` (function, ~220 lines) | delete (R7) | def `backtest.py:330`; **zero callers** — only self-refs in comments (`:571,627,1222`) | no | no | **DELETE — safe** | No callers in src/, scripts/, GUI, or strings. `simulate_with_cache` routes to `simulate_time_stepped`, not this. Pure dead code. |
| 4 | `src/finbert_sentiment.py` (default load) | disable (R6/R10) | only importer: `signal_reporter.py:47` (used at `:891,918,920`) | yes (indirect — signal_reporter launched via `gui_qt.py:2169/2198`) | no | **disable (KEEP file)** | Reachable only via manually-triggered signal_reporter subprocess; output is one decorative premarket card line, zero $/day impact. `signal_reporter.py:918` already guards on `is_available()`, so forcing it False omits the line gracefully. Disable load (don't delete) — less destructive and signal_reporter still imports it. |
| 5 | `src/ml/predict.py` (live veto/conviction wiring) | disable, keep stub (R1/R5) | **top-level import** `main.py:27`; live calls `main.py:170,172,332,334,361,364,400,530,531,534`; lazy `backtest_v3.py:229`, `backtest.py:357,587`; `scripts/*` | status only (`gui_qt.py` via `account.json`) | no | **disable (KEEP module)** | Module is inert (ML_ENABLED=false ⇒ `ml_active` False ⇒ predict_proba never runs) but heavily top-level-wired in main. Do NOT delete — strip the veto/conviction call sites (`main.py:399-402,524-538`), keep `predict_proba/is_available/model_meta` as the future-learning stub. Deleting breaks `main.py:27` import + GUI status + backtest gates. |
| 6 | `src/ml/train.py` (monthly retrain **cron**) | disable cron, keep code (R5) | cron registered `main.py:925` (`day=1 02:00`); body `main.py:674-696` lazy-imports `train.py`; catch-up `main.py:858`; cron_state key `"ml_retrain"` (`cron_state.py:37`); GUI Train button `gui_qt.py:2037` | yes (Train button) | **yes** (`main.py:925`) | **disable cron (KEEP code)** | Cron silently `save_model()`-overwrites live model with no quality/approval gate (铁律 violation) even with ML off. Safe to unregister `add_job` at `main.py:925`, remove catch-up entry `main.py:858`, prune `"ml_retrain"` from `cron_state.KNOWN_JOBS:37`. Keep the trainer module + hide GUI Train button. Code itself stays (reference trainer). |
| 7 | `_weekly_watchlist_refresh_job` (**cron**) | disable cron (R1/R8) | cron `main.py:930` (Sun 22:00); body `main.py:810-826`; catch-up `main.py:852`; cron_state key `"watchlist_refresh"` (`cron_state.py:38`); calls `watchlist_updater.refresh()`→`write_watchlist()`→**overwrites `config/watchlist.json`** (`watchlist_updater.py:266`) | yes ("Refresh WL" button `gui_qt.py:531,733,950`) | **yes** (`main.py:930`) | **disable cron (KEEP module + GUI button)** | NOT a no-op: `refresh()` writes `config/watchlist.json` and `build_watchlist` filters out blacklisted names — so the cron CAN silently mutate the live pinned universe (铁律 violation). Safe to unregister `add_job` at `main.py:930` + catch-up `main.py:852` + prune cron_state key. Keep the module and the GUI "Refresh WL" button (manual, owner-initiated = acceptable). |
| 8 | `portfolio.py::heat_check` (gate) | disable (R3) | live call `main.py:569`; doc ref `glossary.py:301` | no | no | **disable gate (KEEP function)** | It IS called (`main.py:569`), just never binds ($900 cap vs ~$180 real open-risk at 2-pos cash wall). To "disable," gate the call site (`main.py:569`) behind a flag or remove the call — do NOT delete the function (it's the only heat primitive). Caveat: hardcoded `0.20` violates dynamic-capital. Less-destructive = stop calling it, keep code. |
| 9 | `sector.py::check_sector_exposure` (gate) | disable gate (R8) | import `main.py:30`; live call `main.py:504`; def `sector.py:137` | no | no | **disable gate (KEEP function + SECTOR_MAP/SECTOR_TO_ETF)** | Gate is called but can never bind (7 semis+3 tech vs 2-pos wall, MAX_PER_SECTOR=5). Remove the call at `main.py:504` (+ import `main.py:30`). MUST KEEP `SECTOR_MAP`/`SECTOR_TO_ETF`/`get_sector`/`get_sector_etf` — used by `signal_reporter.py:63,143`, `ml/predict.py:108`, `backtest.py:1178`, `gui_qt.py:1908`. Disable the gate only, not the module. |
| 10 | `notifier.py::send_html` (function) | delete (R10) | def `notifier.py:42`; only other ref is its own internal log `:70`; **zero external callers** (signal_reporter uses `notifier.send`) | no | no | **DELETE — safe** | No callers anywhere (its docstring's "used by signal_reporter" claim is false). Pure dead code. |
| 11 | `glossary.py::all_categories` / `all_entries` | delete (R10) | defs `:1042/:1047`; **zero callers** (HelpDialog uses `GLOSSARY` + `get_entry` only) | no (defined for GUI, unused) | no | **DELETE — safe** | No callers in GUI or anywhere. Confirmed dead. |
| 12 | `blacklist.py::force_review` / `manual_remove` | delete or wire (R8) | defs `:249/:260`; only ref is docstring `:17`; **zero callers** | no (intended "for GUI", never wired) | no | **DELETE — safe** (or wire to GUI) | No callers; grep confirms zero. Safe to delete as dead code. (Alternatively wire into a GUI approve card — owner's call. Default-less-destructive = either is fine since removing changes no live behavior.) |

## Items raised in reports that are NOT safe kills (downgraded to KEEP)

- **`indicators.relative_strength` / `sector_regime_bullish`** (R4 "delete dead helpers") — still imported + called in `backtest_v3.py:200,373,382` (flag-gated `apply_rs_gate`/`apply_sector_regime`). Not orphaned. **KEEP** (prune only if you also remove the backtest_v3 gate code).
- **`indicators.min30_trend_bullish`** (R4) — zero callers (only docstring `indicators.py:8`). This one IS dead, but it's a function inside a core kept file, not a module. **Safe to delete the function** (treat like #10/#11).
- **`timeframe.MIN_10` / `MIN_30` presets** (R4) — referenced by `backtest.py:1011,1014` (`_TF_BY_NAME`), `main.py:35` (`_INTRADAY_TFS`), `indicators.py:324`, `scripts/test_min30.py`. Not orphaned. **KEEP** (deleting needs rewiring `_TF_BY_NAME` + `_INTRADAY_TFS`).
- **clock drift-correction** (R9) — "simplify/demote," not a delete; `ny_now()` is live everywhere. **KEEP**.

---

## CONFIRMED-SAFE KILL LIST (provably unused or non-crashing today)

**A. Delete now — zero callers, zero dynamic refs, deletion changes no live behavior:**
1. `backtest.py::backtest_ticker` — dead function (~220 lines), only self-references in comments.
2. `notifier.py::send_html` — dead function, no external callers.
3. `glossary.py::all_categories` and `glossary.py::all_entries` — dead functions, no callers.
4. `blacklist.py::force_review` and `blacklist.py::manual_remove` — dead functions, no callers (also remove the stale docstring ref at `blacklist.py:17`).
5. `indicators.py::min30_trend_bullish` — dead function, no callers (only a docstring mention).

**B. Disable now — safe to unregister/stop-calling without touching imports (reversible, less destructive than delete):**
6. Unregister cron `_monthly_retrain_job` — `main.py:925` `add_job` + catch-up `main.py:858` + prune `"ml_retrain"` from `cron_state.KNOWN_JOBS`. (Keeps `ml/train.py` code; hide GUI Train button `gui_qt.py:2037`.)
7. Unregister cron `_weekly_watchlist_refresh_job` — `main.py:930` `add_job` + catch-up `main.py:852` + prune `"watchlist_refresh"` from `KNOWN_JOBS`. (Keeps module + manual GUI "Refresh WL" button.)
8. Disable `portfolio.heat_check` gate — gate/remove the call at `main.py:569` (keep the function).
9. Disable `sector.check_sector_exposure` gate — remove the call `main.py:504` + import `main.py:30` (keep `SECTOR_MAP`/`SECTOR_TO_ETF`/getters — 5 other live consumers).
10. Disable `finbert_sentiment` default load — force `is_available()` False (keep file; signal_reporter already degrades gracefully).
11. Strip `ml/predict.py` live veto/conviction call sites — `main.py:399-402,524-538` (keep `predict_proba/is_available/model_meta` as the future-learning stub; keep the `main.py:27` import).

**NOT safe to delete (downgraded delete→disable/keep): `strategy_mr.py` (live top-level import at main.py:22), `sec_edgar.py` (delete only after rewiring FEATURE_NAMES + 3 injection blocks + 2 scripts — defensible but multi-file), `ml/predict.py` module (top-level-wired), `sector.py` module, `finbert_sentiment.py` module, `ml/train.py` module, `watchlist_updater.py` module.**

Key safety note: **#6 and #7 are not no-ops to leave running** — both cron jobs silently overwrite live state with no approval (`save_model()` for retrain; `config/watchlist.json` rewrite for watchlist refresh), so disabling their cron registration is the priority 铁律 fix even before any deletion.

Relevant files: `/Users/junxian/Documents/GitHub/moomoo-trader/src/main.py` (cron: 925/930/852/858; gates: 504/569; ML wiring: 399-402,524-538), `/Users/junxian/Documents/GitHub/moomoo-trader/src/cron_state.py` (KNOWN_JOBS:36-42), `/Users/junxian/Documents/GitHub/moomoo-trader/src/backtest.py:330`, `/Users/junxian/Documents/GitHub/moomoo-trader/src/notifier.py:42`, `/Users/junxian/Documents/GitHub/moomoo-trader/src/glossary.py:1042-1047`, `/Users/junxian/Documents/GitHub/moomoo-trader/src/blacklist.py:249-260`, `/Users/junxian/Documents/GitHub/moomoo-trader/src/indicators.py:273`, `/Users/junxian/Documents/GitHub/moomoo-trader/src/ml/features.py:80`, `/Users/junxian/Documents/GitHub/moomoo-trader/src/watchlist_updater.py:266`.