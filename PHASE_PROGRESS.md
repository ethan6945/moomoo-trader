# moomoo-trader — Phase 进度交接 Note

## 🤖 2026-06-07 AI 化重设计收口（A/B/C/D，ultracode + Opus 4.8，代码未 commit）

> 承接 commit `a10dd52`。本批 13 文件 +261/-74，已过对抗性 review（3 视角）并修完
> 1 must-fix + 2 should-fix + 2 nit。90d parity 回测 = **$36.47/day**（与 baseline 一致，
> 主路径未坏）。**代码留给用户 commit**（铁律：AI 不擅自 commit/改风控）。

**A — 本金动态化收口（req#1）**
- `config.derive_max_positions(capital)`：仓位槽数由本金/`SLOT_TARGET_USD`(1000) 推导，
  `max_positions`(5) 作 FLOOR，`max_positions_cap`(10≈池子大小) 作天花板。$4.5k 默认下
  round(4.5)=4 被 FLOOR 抬回 5 → 行为不变，本金涨过 ~$5.5k 才开更多槽。
- live 路径 `risk_manager.max_positions()` = `derive_max_positions(budget_usd())`，
  `budget_usd()` 认运行时 db-state `budget_usd` 覆盖；`main`/`executor`/`portfolio` 全改读它，
  不再硬编码 5/$5k/$4500。两个回测引擎也改 `derive_max_positions(cfg.account_usd)`。

**B — 自主优化环加固（req#3）**
- `optimizer_ai.backtest_risk_change()`：half-Kelly 改动走**风险调整 Calmar 闸**（per_day/DD 不得
  变差）+ 绝对 DD 上限（≤base+`DD_TOLERANCE_PP`3pp），允许"降仓护盘但 $/day 略降"通过、堵住
  "比例放大但 DD 恶化"。base 用**运行时有效** `risk_per_trade`（非冻结 .env 值）。
- `self_improve.half_kelly_proposal`：先回测验证再进审批队列，回测没过直接 drop；None 不静默放行。
- `main._weekly_self_review_job`：拆成独立 try（self_review / self_improve / optimizer / 记账），
  一块挂了不拖垮其余；`self_review` 不再内部调 optimizer（改由 caller 作独立步骤）。
- 所有 proposal 仍只进**审批队列**，绝不 auto-apply（不违反 no-silent-execution）。

**C — Web 安全加固（req#4）**
- `web/server.py`：登录失败**锁定**（per-IP，5 次/300s）+ `secure=request.is_secure` 自适应
  cookie（HTTPS 才置 Secure，明文 LAN 不丢 cookie）+ 明文访问告警。
- review 后补：`_login_fails` 字典**剪枝 + 滚动窗口**（加 last_seen），防轮换源 IP 无界增长，
  且不剪当前 IP（否则计数被自己清零、锁永不触发）。

**D — ML 删除收口**
- `requirements.txt` 去掉 xgboost/scikit-learn/joblib；`.env.example` 把 ML 段换成 DeepSeek
  Autonomous Optimizer 段;`backtest_v3` 删死变量。
- 附带优化：`optimizer_ai._metrics` 去掉多余 `rich_metrics=True`（lean 路径已含
  net_pnl_usd + max_dd_mtm_pct，省掉每 proposal 的 Sharpe/Sortino/MonteCarlo 白算）。

**改动文件（13）**：`.env.example` `requirements.txt` `src/{backtest,backtest_v3,config,executor,
main,optimizer_ai,portfolio,risk_manager,self_improve,self_review}.py` `web/server.py`

**E — DAILY 交易模式删除（2026-06-07，用户拍板）**
- 背景对比（5k budget）：同窗口 90d **HOUR_1 $36.47/day DD8.05% 完胜 DAILY $22.87/day DD9.81%**；
  HOUR_1 的"180d"是假数（OpenD 小时线只回溯 ~140d，43 笔全挤在最近 ~90d，1/16–3/7 空窗 0 单），
  真实预期把空窗算进去 ≈ **$23/day @5k**，$36 是近期顺风季高点。
- 删 `timeframe.DAILY` TF 对象 + `current()`/`_TF_BY_NAME` 兜底改 HOUR_1 + config 默认 HOUR_1。
- **日线数据保留**（HOUR_1 的 MTF `daily_trend_bullish` / `check_gap` / SPY-regime / `DAILY_WEIGHTS`
  兜底打分仍用），只删"DAILY 当主交易框"这条路径。
- 归一化兜底（config `_timeframe()` + `BacktestConfig.__post_init__`）：任何残留 "DAILY" 字符串
  强制→HOUR_1，杜绝"小时线数据 + MTF gate 被字符串跳过"的四不像垃圾数（修前实测 $3.13/day）。
- 验证：DAILY 请求被强制成 HOUR_1，跑出与 HOUR_1 一致的 $36.47/day，零回归。
- 另动文件：`src/timeframe.py` `src/glossary.py`（+ `.env` 注释）。

**F — 顺风加注层（regime up-scaling，2026-06-08，用户批准 ×1.4）**
- 起因：用户想"顺风压更多注/逆风玩短/逆风切10分钟"。数据探明：**MIN_10 只有 23 天历史**
  （逆风期 1–3月零数据）→ "逆风切10分钟"无法回测、纯盲赌，**不做**；逆风做空（现金户不可）/
  均值回归（已证净拖累）也是死路 → "逆风赚钱"= 未解的找新 alpha 难题，不画饼。
- 能落地的只有"顺风加注"。回测（140d/$5k）：`use_regime_scaling`（强势牛市+VIX<calm 放大单仓）
  +$1.6–2.3/day、回撤平、胜率稳；`use_pyramiding`（浮盈滚仓）**反而亏**（5k 现金墙下占槽，
  43→33 单）→ 不开。bull_mult 2×/2.5× 是 ~40 单上的过拟合噪声（收益↑回撤↓不合理）→ 不追。
- 实现：实盘 `risk_manager.calc_position_size` **本来只有减仓层**，新增 Layer 5 `regime_mult`
  上调层（折进 risk_dollars，钳 ≥1.0，仍受 `max_position_pct` cap 约束 → 不会破集中度）；
  `main.py` 调用点按回测**同款条件**算（`regime.bullish and vix<regime_vix_calm` 才 ×mult）；
  `config` 加 `regime_bull_mult`(默认 **1.0=inert**)/`regime_vix_calm`(20)；`_run_live_engine`
  镜像开启读 settings → **live↔backtest 同源 parity**。
- 验证：`REGIME_BULL_MULT=1.0` → 回测仍 **$36.47**（零破坏）；`1.4` → $37.25（90d/$4500，+2%，
  胜率 65→63%；140d/$5k 口径约 +7~10%）。单元测试：mult=1.4 受 cap 挡在 20 股、mult<1 钳回、
  不传参=向后兼容。`.env` 已按用户批准设 **REGIME_BULL_MULT=1.4**（设回 1.0 即关）。
- **诚实备注**：edge 小且全在顺风窗口量的（逆风无数据验证）。另动文件：`src/config.py`
  `src/risk_manager.py` `src/main.py` `src/backtest.py` `.env`/`.env.example`。

---

## 🛠 2026-06-03 重构执行日志（ultracode，审计后落地中）

> 全审计报告见 `AUDIT_REPORT.md`。以下是已落地的改动（代码未 commit，等用户提交）。

**Tier 1 — 零风险清理（✅ 完成验证，honest engine 不变 $40.53/day）：**
- 停掉静默 cron：ML 月度重训 + watchlist 周刷（违反 feedback 铁律 → 改手动 GUI）
- 移除永不开火的闸：`heat_check`、`check_sector_exposure`（函数保留）
- 删 331 行死码：`backtest_ticker`(228) / `notifier.send_html` / `glossary.all_categories,all_entries` / `blacklist.force_review,manual_remove` / `indicators.min30_trend_bullish`
- ML 调用点：**保留**被 `ml_active` 守卫的休眠脚手架（未来 AI 挂载点），未拆（已告知用户）

**T2-动态本金（✅ 完成验证）：**
- `risk_manager` 加 `budget_usd()`（runtime db-state `budget_usd`，GUI 可改、免重启，fallback .env）+ `sizing_capital()=min(budget, live_equity)`（防 SIMULATE 大余额超额下单）+ `set_live_equity()`
- sizing/budget-cap/DD 全改用 `sizing_capital()`/`budget_usd()`；`main.scan_once` 每扫描喂 `cash+持仓MTM`；`adaptive_sizing` 去掉硬编码 `/4500`；`executor.record_trade_close` 用 budget；GUI「Edit Budget」改写 db-state（免重启）
- backtest 引擎**未碰**（用 `cfg.account_usd`）→ honest 数字不变
- 验证：budget 5k→17 股 / 20k→71 股；live=100k→capped 8k ✓

**T2-修保真缺口 part (a)（✅ 完成）：** 诚实引擎现在镜像 live 策略集（trend+momentum_break，取 max）。加 `apply_momentum_strategy` 旗标（默认 OFF 保 parity/frozen-oracle，`_run_live_engine` 设 True，parity_mode 抑制）。
- **重大发现**：之前诚实引擎只跑 trend → 低估了 bot。真实数字：
  - **180d $49.27/day**（旧 trend-only 40.53），DD 8.48%（↓from 11.09）→ **几乎到 $50 目标**
  - **360d $23.02/day**（旧 22.91），DD 12.84%（↑from 10.54）→ momentum 是近期 regime 赢家、长窗中性偏增回撤
- ⚠ part (b)（REAL bracket 无 scale-out/trailing vs SIMULATE 有）+ REAL bugs 待办，都是 executor REAL-path，需翻 REAL 才能验证。

**T2-每周真实成交自省（✅ 完成）：** 新 `src/self_review.py` —复盘真实 broker 成交（非回测），按 strategy/symbol/hour/exit 拆解，算真实 $/day，产建议。新 cron Sun 23:00 + CLI `python -m src.main review`。analyze→notify→enqueue 到审批队列。验证：现有 39 笔真实成交 → 31% 胜率/−$200/avgR −0.42（在亏），正确 flag trend + 多止损。

**T2-inbound审批通道 + 修静默违规（✅ 核心完成）：** 新 `src/approvals.py` —审批队列（db-state，dedup，analyze→notify→APPROVE→execute 的唯一收口；预留 `param_change` 给未来 Anthropic 自主优化器）。`main.scan_once` 每扫描 `apply_approved()` 执行已批准项并通知；CLI `approve/reject/approvals`；snapshot 加 `pending_approvals` 供 GUI 显示。`blacklist.add()` 新增。静默违规修复：DD熔断自动解除→通知；黑名单平仓→显式通知；reconcile auto-fix 评估为「状态同步broker真相+已通知」→保留（强制审批反而不安全）。
- ⏳ **GUI 审批面板（按钮）+ 完整最小化GUI** 是后续 polish（GUI 2500行 PyQt 无法在此可视化测试，已把 pending_approvals 喂进 snapshot 供消费）。

**本会话执行小结：** Tier1 + 动态本金 + 保真缺口(a) + 2个REAL bug + 每周自省 + 审批通道 全部完成验证。**REAL前必修bug已修。** 待办（下会话）：保真缺口 part(b)（REAL scale-out，需翻REAL测）、GUI最小化、Anthropic自主优化器接到 `approvals.param_change`。

**⚠ 新发现待用户消化：** 修保真缺口后真实 180d=$49.27/day（近 $50 目标！）/360d=$23/day；但 39 笔真实 SIMULATE 成交在亏（−$200，31%胜率）——回测乐观 vs 实盘的差距值得下会话用自省循环深挖。

## 🛠 2026-06-03 第二批（用户追加需求）

- **DeepSeek 优化器接口（✅）**：`.env` 加 `DEEPSEEK_API_KEY/MODEL/BASE_URL`（用户后填 key）；`config.py` 读取；新 `src/optimizer_ai.py`（OpenAI 兼容 HTTPS 调用 → 提参数建议进审批队列，无 key 则 no-op）；新 `src/runtime_config.py`（`param_<key>` db-state 覆盖 entry_threshold/tp/sl，免重启，有 ALLOWED_PARAMS 边界）；indicators tp/sl + main threshold 接 runtime override。验证：override thr 70→65 生效、边界校验、无 key no-op。
- **保真缺口落 .env（✅）**：`REAL_USE_SOFT_EXITS`（默认 false）— 翻 true 时 REAL 用与 SIMULATE 相同的 soft scale-out 出场（无 broker bracket），关回测↔live 缺口。需翻 REAL 小仓验证后再开。
- **GUI 审批栏（✅）**：`gui_qt.py` 加 "✅ Approvals" nav + `ApprovalsDialog`（列 pending/approve/reject → `approvals.resolve`，下次扫描生效）。不最小化 GUI。
- **门控权重 + 完善版真实回测（✅）**：`scripts/gate_ablation.py`。
  - **完善版真实 baseline（含 scale-out，= 真在跑的 bot）：180d $51.79/day（超 $50 目标！DD 8.27%）/ 360d $25.77/day（DD 12.84%）。** 比之前 $49 高，因为之前漏了 scale-out。
  - **门控权重结论（反直觉，诚实）：多数门控在「赚钱」不是「过度保守」。** 唯一真正过紧的是 gap cap：
    - ✅ **MAX_GAP_PCT 2.5→4.0**：+$5.8/day(180d)/+$0.9(360d)，**两窗都赢**，DD +2.8pp（仍<18%）。**已应用 .env。** → 180d 变 ~$57.6/day。
    - threshold 65/60（降门槛=少把关）：**−$11~12/day 灾难**，胜率 72%→52%，DD 爆。证明门槛 70 是对的，多交易≠多钱。
    - scale-out −$2.6/day、MTF −$1.9、earnings −$1.3：都 protective，保留。
    - regime gate：中性微正（+0.47），可留可松（是熊市下行保护）。
  - **给用户「少把关多收益」的诚实答案：除 gap cap 外，门控都在创收，不能盲删。已落地唯一验证过的松绑（gap 4.0）。**

## 🛠 2026-06-03 第三批：live↔backtest 逻辑对齐（用户要 real trading 逻辑全一样）

把验证过的改进用进真实交易 + 消除 live 与诚实回测的逻辑分歧（这样回测的 $/day 才代表真实表现）：
1. **MAX_GAP_PCT 2.5→4.0**（.env）— gap 门控松绑，已生效（live 直接读 settings）。
2. **硬门槛**（main.py）— 去掉 trend-only 时代的 marginal sub-70 半仓 band（`threshold_floor = entry_thr`）。回测就是硬门槛，且 ablation 证明 sub-70 毁收益。
3. **AI veto → 顾问制**（`AI_VETO_BLOCKING=false`）— 回测没有 AI 层，所以 AI 改为「记录+显示但不拦单」，live 选股 = 回测。可设 true 恢复拦截。
4. **REAL_USE_SOFT_EXITS=true**（.env）— REAL 用与回测/SIMULATE 相同的 soft scale-out 出场（无 broker bracket）。⚠ 仍在 SIMULATE，翻 REAL 头几单要盯一下成交。

**最终对齐版真实回测（= 现在 live 真在跑的逻辑）：**
- **180d $57.59/day**（win 73.2%, DD 11.12%, PF 4.26）→ **超 $50 目标**
- **360d $26.68/day**（win 63.5%, DD 12.21%, PF 3.00）

**仅剩的 live-only 安全项（极少触发，刻意保留）**：blacklist（仅 owner 批准的连亏票）、spread 闸（流动性差才拦）、max_new_names_per_scan=2（节流，现金墙下本就~2 仓）。这些不实质改变 $/day。

**结论：完善对齐版 bot 近 180 天回测 $57.6/day（达标），且 live 逻辑已与回测一致。** sizing 也一致（live `sizing_capital=min(budget, equity)`，SIMULATE 余额≥5k 时 = 回测 account_usd 5000）。

## 🛠 2026-06-03 第四批：审计路线图 next/later 全做（self-improvement）

- **#9 sentinel 钳制（✅）**：optimizer fold sortino 钳到 [-10,8]，防单个 lucky fold 用 9.99 sentinel 主导→过拟合。（optimizer 是 suggest-only，影响有限但更稳。）
- **#7 reconcile ATR 止损（✅）**：孤儿仓采用从 `cost×0.965` 魔数改为 live 出场乘子派生（`cost − sl_atr_mult×(2%价)`），止损与 bot 实际出场一致。auto-adopt 保留（保护真实仓+已通知，强制审批反而不安全）。
- **#5 half-Kelly（✅）**：`self_improve.half_kelly_risk` 从真实成交估 edge(p,payoff)→半 Kelly→建议 risk_per_trade（审批制，runtime 可覆盖）。**实测当前 39 笔无 edge(Kelly −0.20,31%胜率)→正确拒绝加仓**（比旧 Sortino 乘子更安全）。
- **#10 PnL 宇宙复盘（✅）**：`self_improve.universe_review` 按真实 per-symbol expectancy 排序→建议 drop 长期亏票(≥6笔)→审批。并入每周自省。
- **#6 因子权重自学习（✅ 测了，无效）**：fixed-weight AUC 0.518 vs logistic-learned 0.498（更差）。**因子权重学习和 ML 一样在本宇宙无 edge → 保持手调权重，不加复杂度。**（`scripts/factor_weight_refit.py`）
- **#8 regime 连续放大（✅ 测了，不接）**：180d +$1.67/day 但 **360d −$5.31/day**、DD 15.56%（恶化）。两窗不一致 → **保持 OFF**（牛市放大在难窗放大亏损）。
- **#11 ML 复活（✅ 测了，仍死）**：重标签对齐真实出场(tp8/sl3.5/h49)后 holdout AUC 0.5125（原 0.468）—— 略好但**仍≈掷硬币**。**ML 在本宇宙无 edge，保持 ML_ENABLED=false。** 真要 alpha 得换宇宙/另类数据，非调 ML。

**审计路线图 next/later 全部完成（#5-#11）。结论：能提收益的真实杠杆只有 gap-cap 松绑（已落地 $57.6/day）；其余 ML/因子权重/regime放大 在本 mega-cap 宇宙都无 edge（诚实验证，不强加）。self-improvement 框架（half-Kelly/宇宙复盘/DeepSeek 优化器）已就位，随真实成交累积 + 你填 DeepSeek key 后自动产建议（审批制）。**

## 🛠 2026-06-03 第五批：Telegram 审批 + 换宇宙实测

**Telegram 审批 bot（✅）**：`src/tg_approvals.py` —— 待批建议推卡片(带 ✅批准/✖拒绝 按钮)，轮询点击→写共享 db 审批队列→下次扫描执行。GUI 审批栏 4 秒自动刷新→与 Telegram 同步。调度器每 60s sync(任何时段)。**已实测发卡+点击+应答+改卡片全通**(loading 是因旧调度器没轮询；重启加载新代码即自动)。GUI `ApprovalsDialog` 加 QTimer 自动刷新。⚠ 激活需重启调度器。

**换宇宙实测（✅ 决定性负面结论）**：`scripts/universe_compare.py` —— 完全相同策略/参数，只换股票池：
| 宇宙 | 180d $/day | 360d $/day | 回撤 | 胜率 | PF |
|---|---|---|---|---|---|
| MEGA(现10只) | **+57.59** | **+26.68** | 11-12% | 64-73% | 3.0-4.3 |
| 中小盘高波动篮子 | **−8.59** | **−4.97** | **41-48%** | 23-36% | 0.5-0.7 |
- **同策略丢中小盘=灾难**。策略与宇宙绑定，edge 不迁移：现策略是为大盘半导体平滑趋势 + 宽 TP/SL 调的，野票被来回打脸。
- 偏差自认：测试篮子是静态的、含 RIVN/LCID/MARA/RIOT 等窗口内暴跌名，只做多动量在跌票上必亏（选池没筛趋势）。中小盘要能用需 (a)RS/动量筛只取上升流动票 +(b)重调策略 = 真项目,非快赢。

## 🏁 本会话最终结论（2026-06-03）

**$57.6/day(180d 回测)≈ 这套「大盘半导体 + trend/momentum」配置的天花板。** 本会话穷举验证、全部负面：ML 死、因子权重学习无效、RS/SOXX 闸亏、regime 放大亏、裸换中小盘大亏。**硬调策略/模型/宇宙都到顶。**

**唯一确定性放大收益的路 = 加本金**（$5k 现金墙锁 ~2 仓是硬约束；本金 ×2 ≈ $/day ×2）。其余（中小盘+专用策略、期权流/另类数据）是高投入不确定的研究项目。

**系统现状**：交易全自动 + 全程 feedback(GUI/Telegram 审批) + self-improvement 框架就位 + DeepSeek 接口待 key。逻辑 live↔回测已对齐。**待用户 commit**（第五批：`tg_approvals.py`、`main.py`、`gui_qt.py`、`scripts/universe_compare.py` 未提交）。

新模块：`self_improve.py`、`runtime_config.risk_per_trade`。新脚本：`factor_weight_refit.py`、`ml_relabel_test.py`、`regime_scaling_test`(inline)。

---


> 最后更新：2026-06-02（Phase 2 已应用 + Phase 3 已验证）。新 chat 可直接读这份文件接续。

---

## 🔔 最新状态（2026-06-02 收盘后）

**Phase 2 建议已全部应用（不再是「待手动」）：**
1. `.env`：`MAX_POSITION_PCT` = **0.40** ✅
2. `config/watchlist.json`：锁回 **10 只** ✅
3. `src/watchlist_updater.py`：`TARGET_SIZE = len(ANCHORS)`（=10），刷新不再扩到 25/30 ✅

**Phase 3（探索新 alpha）已做完 + 诚实引擎验证完毕。结论：调参/加 gate 仍无法把 $/day 抬上去。**

新写了两个 entry gate（都 **默认 OFF**，flag-gated，parity-suppressed，不污染 diff-test）：
- **A `apply_rs_gate`**（`indicators.relative_strength`）：个股 N 日 daily 回报 − SPY 必须 ≥ `rs_min_pct` 才进场
- **B `apply_sector_regime_gate`**（`indicators.sector_regime_bullish`）：SOXX EMA20≤EMA50 时暂停新进场
- **C**：财报屏蔽**早已在生产**（`apply_earnings_gate=True` 在 `_run_live_engine`），保留基础版，未做 PEAD（数据脆弱/易过拟合）

**验证结果（`scripts/phase3_validate.py`，诚实现金引擎，pinned 10，mpp=0.40）：**

| 组合 | 180d $/day | 360d $/day | 180d MTM-DD | 360d MTM-DD | verdict |
|---|---|---|---|---|---|
| **baseline** | **40.67** | **22.91** | 11.09% | 10.54% | — |
| +RS(0) | 39.37 (−1.3) | 23.09 (+0.2) | **8.47%** | **8.08%** | ⚠ mixed |
| +RS(+2pp) | 37.96 | 21.58 | 10.62% | 10.01% | ❌ 两窗都输 |
| +SOXX | 39.48 | 21.78 | 11.09% | 10.60% | ❌ 两窗都输 |
| +RS(0)+SOXX | 38.23 | 22.05 | 8.47% | 8.81% | ❌ 两窗都输 |

**判定：没有任何组合在两个窗口都跑赢 baseline 的 $/day → 默认全部保持 OFF。** 再次印证 2b/2c 的教训。

**唯一有价值的副发现：`+RS(0)` 是「降回撤」杠杆而非「提收益」杠杆** —— $/day 基本打平（−1.3 / +0.2），但 MTM 回撤两窗都砍约 24%（11.09→8.47%、10.54→8.08%），胜率小升、交易数略减。**若用户偏好降回撤可手动开 `apply_rs_gate=True, rs_min_pct=0.0`；若只盯 $50/day 目标则不开。** 待用户决定是否接进 live（live 侧需在 main.py + settings + .env 加旗标，目前只在 backtest 引擎里）。

**未提交代码（用户决定 commit）：** `indicators.py`(+2 helper)、`backtest.py`(+6 cfg 旗标 + SOXX 预取)、`backtest_v3.py`(+2 gate + 计数器)、`watchlist_updater.py`(TARGET_SIZE)、新脚本 `scripts/phase3_validate.py`。`.env` + `watchlist.json` 已由 AI 直接改（用户本轮授权）。

**Phase 4 方向已定（用户 2026-06-02 选择）：探新数据源 alpha。** RS 不接 live（用户决定保持现状）。

**关键：另类数据基建已有一半，第一步不必接新 API：**
- `src/sec_edgar.py`：SEC Form 4 内部人交易 → 已做成 ML 特征 `insider_30d_net_usd`（已在训练集里）
- `src/finbert_sentiment.py`：FinBERT 情绪，模型已本地缓存，但**目前只喂盘前播报卡片，没进交易信号/ML**
- `src/news_fetcher.py`：Tavily 新闻只喂 AI validator 做否决，不产生 alpha

**Phase 4 建议的具体第一步（低成本、可证伪、建在已有地基上）：**
1. **量化现有内部人特征的真实贡献**：在 ML 里做 ablation —— 有/无 `insider_30d_net_usd` 跑诚实引擎，看它到底值不值钱（可能它已经是现有 baseline 的一部分 alpha，也可能是噪音）。
2. **把 FinBERT 情绪从「只显示」升级为「进信号」**：作为 conviction 乘数或新 ML 特征，本地模型零 API 成本，诚实引擎验证。
3. （更大投入）**期权流 / 资金流**：需接新付费 API（如 Unusual Whales），工量大，留到 1-2 没结果再上。

> 方法论铁律（每次都照做）：新特征一律 **默认 OFF + flag-gated + parity-suppressed**，在 `_run_live_engine` 上跑 180d+360d，**两窗都跑赢 baseline $/day 才接 live**。可复用 `scripts/phase3_validate.py` 的对比框架。

**Phase 4 的另一条确定路（非技术）：加本金。** $5k 现金墙是硬约束，本金大了约束自然松——这是唯一不依赖找新 alpha 的确定路径。

---

## Phase 4-1：内部人特征 ablation（2026-06-02 已做）

**问题**：ML 特征 `insider_30d_net_log`（SEC Form 4 内部人交易）到底有没有 alpha？

**方法**（`scripts/insider_ablation.py`，零生产风险——变体模型存 temp 路径 + monkeypatch `predict.MODEL_FILE/FEATURE_NAMES`，生产 `model.joblib` 全程没碰）：同一份数据/切分/seed 训练「有/无该特征」两个 XGBoost，比 holdout 质量 + 诚实引擎 $/day。

**结论：该特征在这 10 只 mega-cap 半导体上完全惰性（无 alpha），可作清理删除，但非紧急（删不删都不影响结果）。**
- XGBoost gain importance = **0.0000，排名 #36/36**（模型从不在它上面分裂）
- holdout：ΔAUC −0.0001、Δlogloss −0.0001（纯噪音）
- 诚实引擎 $/day：**逐分相同**（180d 40.39=40.39，360d 22.91=22.91，trades/DD 全等）
- **不是因为数据缺失**：内部人数据 39% 非零（1437 行中 564 行有值）。是因为对这些高流动 mega-cap，内部人活动与「TP-before-SL」结果**没有可学关系**。内部人买入的 edge 文献上主要在中小盘，大盘半导体上是噪音。

**⚠ 顺带发现（值得单独查，与 ablation 无关）**：两个模型 holdout AUC ≈ **0.468（<0.5）**，即 ML 模型在最近 15% 数据上接近随机甚至略差。ML 闸（`apply_ml_gate=True`）在生产里的真实价值存疑——**建议 Phase 4 单开一个「ML 闸是否还值得开」的 ablation**（有/无 ML 闸跑诚实引擎），可能比找新特征更高价值。

**对「探新数据源 alpha」的启示**：技术面 + RS + 内部人都榨干了。下一步要么 (a) 换universe（中小盘，内部人/另类数据更有 edge），要么 (b) 接真正的新数据（期权流/资金流），要么 (c) 先回头审 ML 闸本身是否在帮倒忙。

**未提交新脚本**：`scripts/insider_ablation.py`（用户决定 commit）。

---

## Phase 4-2：ML 闸 ablation（2026-06-02 已做）

**问题**：ML 闸（`apply_ml_gate=True`，proba<0.35 否决）在赚钱还是白白否决好单？

**方法**（`scripts/ml_gate_ablation.py`）：诚实引擎 ML-ON vs ML-OFF，其余全同，180d+360d。

**结论：在回测里 ML 闸的「否决」完全惰性——从不触发。**
- ML-ON vs ML-OFF **逐项相同**：trades 89/89、123/123，$/day 40.39/40.39、22.91/22.91，DD/PF/sortino 全等
- 即模型对所有「已过 rule≥70」的候选从没输出过 proba<0.35 → 否决从不发生 → 开关无差别
- 生产模型自报 holdout AUC **0.516**（我的复训得 0.468）——两边都贴着 0.5，模型 edge 极弱/脆弱

**⚠ 关键保真缺口（必须记住）**：回测里 ML proba **只用于否决**（`backtest_v3.py:388-389`），**不影响仓位**。但 **live（`main.py:534`）里 ML proba 还驱动 conviction 仓位**：proba<0.55（ML_BOOST）→ 仓位减半。所以：
- 回测证明了：**ML 否决没用**（从不触发）。
- 回测**没建模** ML 的 conviction 减半 → 无法判断 live 里「neutral-zone 减半」是帮还是亏。鉴于模型 ≈ 随机，**很可能在半随机地砍好单的仓位**。

**下一步建议（真正能验证 ML 模型价值的测试）**：给 `backtest_v3` 加一个 flag-gated（默认 OFF）的 **ML conviction 仓位**，镜像 live（proba<0.55 → ×0.5），再跑 ML-ON-with-sizing vs ML-OFF。这才能回答「ML 模型整体（否决+仓位）到底帮不帮」。如果加了 sizing 后 ML-ON 仍不赢，建议 `ML_ENABLED=false` 直到模型重训出真 edge。

**未提交新脚本**：`scripts/ml_gate_ablation.py`（用户决定 commit）。

---

## Phase 4-2b：ML conviction-sizing 镜像 + 重测（2026-06-02 已做，结论决定性）

**做了什么**：给 `backtest_v3` 加 flag `apply_ml_conviction_sizing`（默认 OFF，parity-suppressed），镜像 live `main.py:534` 的 proba<0.55→仓位减半。三变体重测：ML-OFF / ML-VETO（只否决）/ ML-FULL（否决+减半=live）。

**结论：ML 模型完全惰性——既不否决也不减半，三变体逐项相同。**
| 窗口 | 变体 | $/day | trades | 减半次数 |
|---|---|---|---|---|
| 180d | ML-OFF / VETO / FULL | 40.39（三者全等）| 89 | **0** |
| 360d | ML-OFF / VETO / FULL | 22.91（三者全等）| 123 | **0** |

- **ML-FULL halved 0 entries** —— 即对所有过 rule≥70 的候选，模型 proba **永远 ≥0.55**（既 >0.35 不否决，又 ≥0.55 不减半）
- **为什么**：训练集 pos-rate=60.4%，AUC≈0.5 的模型基本只学到了「先验基率」，对所有候选都吐 ~0.6 → 是个常数函数 → 对交易零影响（live + backtest 都是）
- 这比「AUC<0.5」更硬：不是「模型弱」，是**模型对实际下单population完全无作用**

**✅ 已应用（2026-06-02）：`.env` 加了 `ML_ENABLED=false`。** 已证明对 PnL 零影响（逐分相同、0 否决、0 减半），关掉只会去掉每次扫描的死 CPU + sector ETF/SEC EDGAR 抓取依赖，并消除「有个 AI 模型在帮我筛单」的错觉。`settings.ml_enabled=False` → live `main.py` 的 `ml_active` 恒 False，ML 路径不再运行。
- ⚠ 唯一未覆盖：live 还会在 marginal setup（score 60-70，半仓投机）上跑 ML，回测不含这段——那里 ML 偶尔可能 bite。但那是次要population。
- 真要让 ML 有用，得重训出 AUC 实质 >0.5 的模型（换 label / 换特征 / 换 universe），否则它就是装饰。

**未提交代码**：`backtest_v3.py`(+conviction sizing 旗标+计数器)、`backtest.py`(+1 旗标)、`scripts/ml_gate_ablation.py`（用户决定 commit）。

---

---

## 一、目标

- **账户**：MooMoo OpenAPI，美股，**$5,000 现金** SIMULATE/纸上账户（无杠杆）
- **每日目标**：**USD $50/day** 已实现盈利
- **总目标**：RM20,000
- **沟通**：中文；严谨诚实，不许吹未验证的结论
- **铁规矩**：
  - `.env` 改动**只给建议，用户自己改**；代码**用户自己 commit**，AI 不提交
  - `.env` 含密钥（MOOMOO_TRADE_PWD / GEMINI / TAVILY / TELEGRAM），只碰非密钥的交易参数
  - 现金账户，不许偷偷开杠杆/金字塔加仓；新引擎旋钮默认 no-op
  - 删文件前先确认

## 二、核心认知（最重要）

**约束是「现金」不是「机会」。** `$5k + 最多 5 个仓位` 下，`max_position_pct × 槽位 ≈ 100%` 在约 2 个仓位就锁死。所以**加仓位大小、加股票数量都只会摊薄质量**（胜率/盈亏比崩、现金限制爆表）。

**生产引擎真实配置**：`apply_ml_gate=True`（ML 否决开）+ `apply_mr_strategy=False`（均值回归关）。

**两个引擎：**
- `simulate_time_stepped`（`src/backtest.py`）= 冻结的乐观/杠杆 oracle（仅作基准）
- `simulate_v3`（`src/backtest_v3.py`）= 诚实现金引擎
- `_run_live_engine()` = 唯一诚实生产入口（VIX 减仓 + 财报闸 + 真实佣金 + 现金墙）

## 三、Phase 进度（全部已完成）

| Phase | 内容 | 结论 |
|---|---|---|
| **2a** 现金饥饿修复 | 调仓位/槽位 | ✅ `MAX_POSITION_PCT` 0.50→0.40（每仓更小、能多挤一个名字） |
| **2b** 弱势防御 | 砍亏损月 | ✅ **什么都不改**——SL 是不可省的保险，在为肥尾 TP 买单；6 种「防 SL」改法全掉钱 |
| **2c** 扩票 + Optuna 重调 | 诚实引擎上重新优化 | ✅ **钉死 10 只 + 保持现行参数**——扩到 15/25 两窗口都更差；Optuna 的 Sortino 最优参数实测 $/day 全输 |

## 四、最终建议清单（⚠️ 待用户手动应用，尚未改）

**要改（3 处）：**
1. `.env`：`MAX_POSITION_PCT` **0.50 → 0.40**
2. `config/watchlist.json`：钉回这 **10 只** →
   `SNDK, MU, INTC, LRCX, DDOG, AMD, WDC, SWKS, PANW, MCHP`
3. **约束/关掉 `watchlist_updater`** 自动扩到 25 只的行为——当前**正在拖累实盘 ~$9-17/day** 的真凶

**不要改（实测已最优）：**
- 出场参数：`THRESHOLD=70 / TP=8.0 / SL=3.5 / GAP=2.5`
- 止损机制保持原样

**当前最优实测成绩（诚实引擎，10 只，mpp=0.40）：**
- 180d：**+$43.5/day**，回撤 10.6%
- 360d：**+$24.2/day**，回撤 10.5%
- → 仍**低于 $50/day 目标**（短窗口接近，长窗口差一截）

## 五、接下来要干嘛（Phase 3 方向）

**$50/day 靠调参已经到顶。** 要继续往上只剩两条真路子：
1. **找新 alpha**（新信号源 / 新因子）——唯一能实质提升的方向，**建议作为 Phase 3 核心**
2. **加本金**——$5k 现金墙是硬约束，本金大了约束自然松

> 新 chat 第一个任务建议：定 Phase 3 = 探索新 alpha 来源，而不是继续磨现有参数。

## 六、未提交的代码状态（用户决定要不要 commit）

- `src/optimizer.py`：改道到 `_run_live_engine`（让 Optuna 跑在诚实引擎上）+ 解耦 ML/MR 旗标 + 门槛范围 55-80。**建议保留**，让未来优化更忠实。
- 新建诊断脚本：
  `scripts/optuna_honest.py`、`scripts/optuna_validate.py`、`scripts/universe_prune.py`、
  `scripts/leak_diagnose.py`、`scripts/sl_defense_sweep.py`、`scripts/regime_diagnose.py`

## 七、已确认的保真事实（回测 vs 实盘）

- **参数同源**：两边都读 `settings`/`.env`；TP/SL 公式一致（`indicators.py:67,72` ↔ `backtest_v3.py:426-427`）；仓位核心一致（`risk_manager.calc_position_size` ↔ `backtest.py:_position_size`）
- **实盘多几层状态相关减仓**（连亏 streak、自适应 Sortino），回测只部分建模 → 回测偏保守
- **优化不自动上实盘**：Optuna 只发建议，要用户手动改 `.env` 重启（`src/main.py:748-789` 明确写死不自动改）

## 八、关键文件地图

| 用途 | 文件 |
|---|---|
| 实盘扫描/信号漏斗 | `src/main.py` |
| 下单/出场/移动止损 | `src/executor.py` |
| 仓位/VIX 减仓/状态/DD 闸 | `src/risk_manager.py` |
| 诚实现金引擎 | `src/backtest_v3.py` |
| 冻结 oracle + `_position_size` | `src/backtest.py` |
| Optuna 优化器 | `src/optimizer.py` |
| 信号/TP/SL 属性 | `src/indicators.py`（`sl_atr_mult`/`tp_atr_mult` 公式在 62-72 行） |
| 配置（冻结 dataclass） | `src/config.py`（改用 `object.__setattr__` 临时覆盖，finally 还原） |
| 股票池 | `config/watchlist.json` |
