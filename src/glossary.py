"""In-app glossary / help content.

Structured as ordered dict-of-dicts:
    GLOSSARY = {
        "Category": {
            "Term": {
                "name":    "Full English name",
                "summary": "One-line tl;dr",
                "explain": "Full body",
                "where":   "Where the system uses it",
                "value":   "Current setting (optional)",
            },
            ...
        },
        ...
    }

Python 3.7+ preserves dict insertion order, so the order you write entries
here is the order they appear in the Help window TOC.
"""
from __future__ import annotations


GLOSSARY: dict[str, dict[str, dict[str, str]]] = {
    # ===================================================================
    "📘 概述 Overview": {
        "moomoo-trader": {
            "name": "What is moomoo-trader",
            "summary": "AI 驱动的美股自动化短线交易系统",
            "explain": (
                "一个完整的自动化交易闭环:\n\n"
                "  • 每 30 分钟扫描 watchlist (30 只 S&P 500 强势股)\n"
                "  • 用 6 因子打分 + ML 历史胜率 + Gemini AI 上下文审核\n"
                "  • 8 道过滤漏斗筛掉烂信号\n"
                "  • 通过 MooMoo OpenAPI 下限价单 + OCO 止损/止盈\n"
                "  • Telegram 推送所有信号 + 持仓变化\n"
                "  • DD 断路器 + 自适应仓位 + 黑名单 = 三层风控\n"
                "  • 每月 ML 重训 + Optuna 重新调参 (建议不强加)"
            ),
            "where": "整个 src/ 目录都是它的实现",
            "value": "180 天回测: Sortino 3.48 / Sharpe 2.53 / Win Rate 60.9% / Max DD 9.3%",
        },
        "8 道过滤漏斗 (Filter Funnel)": {
            "name": "8-layer Decision Funnel",
            "summary": "信号要通过 8 道关卡才能下单",
            "explain": (
                "30 只股 × 每 30 分钟 ≈ 1500 个候选/天，最后下单 2-5 笔。\n"
                "信号必须连续通过所有这些关:\n\n"
                "  1. trade_phase   美东 09:45-15:30 才开新仓\n"
                "  2. regime        SPY 200MA 在 BEAR 时禁开\n"
                "  3. blacklist     黑名单股直接跳过\n"
                "  4. gap           隔夜跳空 > 2.5% 拒入\n"
                "  5. mtf           daily EMA20>EMA50 才算趋势对\n"
                "  6. sector        同板块最多 4 仓\n"
                "  7. earnings      ≤ 2 天有财报的跳过\n"
                "  8. ml + ai       ML 概率 ≥ 0.35 + Gemini 不否决"
            ),
            "where": "src/main.py 的 scan_once() 是这个漏斗的实现",
        },
        "时间框架 (Timeframe)": {
            "name": "Timeframe",
            "summary": "用哪个 K 线周期判断信号",
            "explain": (
                "可用 (.env 里 TIMEFRAME=):\n\n"
                "  • HOUR_1   — 1 小时 K，持仓 1-5 天 (✓ 当前, 实盘交易框)\n"
                "  • MIN_10   — 10 分钟，日内 (回测发现在当前市场亏钱)\n\n"
                "HOUR_1 是 Optuna 验证后最稳的，180 天 Sortino +3.48。\n"
                "(DAILY 日线交易模式已于 2026-06-07 删除 — 同窗口回测 HOUR_1 完胜\n"
                " $36.5 vs $22.9/day 且回撤更低; 日线数据仍保留给 HOUR_1 的 MTF 确认用。)"
            ),
            "where": ".env: TIMEFRAME=HOUR_1",
            "value": "HOUR_1",
        },
    },

    # ===================================================================
    "🎯 评分系统 Scoring": {
        "6 因子打分 (6-Factor Score)": {
            "name": "Multi-factor Composite Score",
            "summary": "0-100 综合分数，决定是否入场",
            "explain": (
                "每个因子单独算 0-100，按权重加权:\n\n"
                "  趋势 (trend)      18%   EMA9>EMA21 + 斜率向上\n"
                "  动量 (momentum)   18%   MACD 金叉 + RSI 40-70 + Stoch\n"
                "  量能 (volume)     14%   当根量 > 20 期均 × 1.5\n"
                "  形态 (pattern)    14%   突破 20 期新高 / BB 下轨反弹\n"
                "  ADX               13%   ADX ≥ 25 强趋势\n"
                "  VWAP              13%   价格 ≥ rolling VWAP(20)\n"
                "  AI veto           10%   Gemini + Tavily 新闻审核"
            ),
            "where": "src/indicators.py 的 HOUR_1_WEIGHTS",
        },
        "入场阈值 (Entry Threshold)": {
            "name": "ENTRY_SCORE_THRESHOLD",
            "summary": "分数 ≥ 这个才下单",
            "explain": (
                "Optuna 在 60-80 区间搜过 (step 2)，当前 .env 用 70。\n"
                "入场用一个'门槛 − 10'的缓冲带 (threshold_floor=60):\n\n"
                "  • < 60   → 完全拒 (连漏斗都不进)\n"
                "  • 60-69  → marginal (进漏斗,但减半仓位 half-conviction)\n"
                "  • 70+    → full conviction (满仓信心)\n\n"
                "为什么要这条缓冲带:多数美股大盘股常年只打到 ~60 分,若硬卡 70\n"
                "几乎永远 top_5=[] → 一笔都不开。所以 60-69 也放进来,但仓位减半,\n"
                "和 ML '中性区'减半是同一个思路 — 试,但小试。\n\n"
                "若叠加 ML 中性区 (proba 0.35-0.55 再 ×0.5) → 最小可到 1/4 仓。\n"
                "分数 80+ 在回测里胜率最高,但样本太少。"
            ),
            "where": ".env: ENTRY_SCORE_THRESHOLD;src/main.py: threshold_floor / marginal_setup",
            "value": "70 (缓冲带下沿 60)",
        },
    },

    # ===================================================================
    "📊 技术指标 Indicators": {
        "ATR — Average True Range": {
            "name": "Average True Range",
            "summary": "股票平均每日波动幅度",
            "explain": (
                "14 期 True Range 的均值。True Range = max 三者:\n"
                "  • 今日 High - 今日 Low\n"
                "  • |今日 High - 昨日 Close|\n"
                "  • |今日 Low  - 昨日 Close|\n\n"
                "ATR 大 = 波动大，止损要拉宽\n"
                "ATR 小 = 安静，止损可以紧"
            ),
            "where": "止损/止盈/仓位计算全靠它:\n"
                     "  stop = entry - 3.5 × ATR  (SL_ATR_MULT)\n"
                     "  tp   = entry + 8.0 × ATR  (TP_ATR_MULT)\n"
                     "  qty  = $250 风险 / 止损距离",
        },
        "EMA — Exponential Moving Average": {
            "name": "Exponential Moving Average",
            "summary": "指数移动均线，比 SMA 更注重近期价格",
            "explain": (
                "权重指数衰减:今天价格 > 昨天 > 前天 > ...\n"
                "比简单 MA 反应快约 50%，趋势判断主用。\n\n"
                "本系统:\n"
                "  EMA9 / EMA21  (HOUR_1 趋势判断)\n"
                "  EMA20 / EMA50 (Daily 主趋势)"
            ),
            "where": "factor 'trend' 评分 + MTF 过滤 + 移动止损",
        },
        "MACD": {
            "name": "Moving Average Convergence Divergence",
            "summary": "两条 EMA 的差，判断动量转折",
            "explain": (
                "三条线:\n"
                "  MACD Line  = EMA12 - EMA26\n"
                "  Signal     = MACD 的 9 期 EMA\n"
                "  Histogram  = MACD - Signal\n\n"
                "金叉 (MACD 上穿 Signal) = 买信号\n"
                "死叉 (MACD 下穿 Signal) = 卖信号"
            ),
            "where": "factor 'momentum' 评分核心",
        },
        "RSI — Relative Strength Index": {
            "name": "Relative Strength Index",
            "summary": "0-100 超买超卖指标",
            "explain": (
                "RSI = 100 - 100/(1 + RS)  RS = 平均上涨/平均下跌 14 期\n\n"
                "  RSI > 70  超买，反转风险\n"
                "  RSI 40-70 健康上涨区\n"
                "  RSI < 30  超卖，反弹机会\n\n"
                "本系统要求 RSI 在 40-70 才给动量满分 — 既不冷也不过热。"
            ),
            "where": "factor 'momentum' 评分",
        },
        "ADX — Average Directional Index": {
            "name": "Average Directional Index",
            "summary": "趋势强度 0-100",
            "explain": (
                "不管方向，只衡量趋势“强不强”:\n\n"
                "  ADX < 20    震荡市 (趋势策略失败)\n"
                "  ADX 20-25   弱趋势\n"
                "  ADX ≥ 25    强趋势 (本系统给满分)\n"
                "  ADX ≥ 50    极强 (但可能要反转)\n\n"
                "震荡市识别这个最重要 — ADX 低就别开。"
            ),
            "where": "factor 'adx' 评分 + 30min MTF 过滤",
        },
        "BB — Bollinger Bands": {
            "name": "Bollinger Bands",
            "summary": "20 期均线 ± 2 标准差",
            "explain": (
                "三条线:\n"
                "  上轨 = SMA(20) + 2σ\n"
                "  中轨 = SMA(20)\n"
                "  下轨 = SMA(20) - 2σ\n\n"
                "价格 95% 时间在轨道内。系统用:\n"
                "  • 触下轨反弹 → pattern 因子加分\n"
                "  • Squeeze (轨道收窄) → 即将爆发"
            ),
            "where": "factor 'pattern' 评分",
        },
        "VWAP — Volume Weighted Average Price": {
            "name": "Volume Weighted Average Price",
            "summary": "成交量加权均价 — 机构基准",
            "explain": (
                "VWAP = Σ(价 × 量) / Σ(量)\n\n"
                "意义:大资金的“成本”。\n"
                "  • 价格 > VWAP → 多头主导\n"
                "  • 价格 < VWAP → 空头主导\n\n"
                "本系统要求价格在 rolling VWAP(20) 上方才能开多。"
            ),
            "where": "factor 'vwap' 评分 + 30min MTF",
        },
        "Stochastic": {
            "name": "Stochastic Oscillator (随机指标)",
            "summary": "%K 当前价相对 N 期高低区间的位置",
            "explain": (
                "%K = 100 × (Close - Low_N) / (High_N - Low_N)\n"
                "%D = %K 的 3 期 MA\n\n"
                "  %K > 80 超买\n"
                "  %K < 20 超卖\n\n"
                "比 RSI 更敏感，本系统要求 20 ≤ %K ≤ 80 时给动量加成。"
            ),
            "where": "HOUR_1 的 momentum 因子辅助",
        },
        "VIX — Volatility Index": {
            "name": "CBOE Volatility Index",
            "summary": "未来 30 天 S&P 500 预期波动率",
            "explain": (
                "也叫“恐慌指数”:\n\n"
                "  VIX < 15    平静牛市\n"
                "  VIX 15-25   正常\n"
                "  VIX 25-35   高波动 → 本系统仓位减半\n"
                "  VIX > 35    危机 → 仓位减 75%\n\n"
                "VIX 暴涨 = 市场恐慌 = 趋势策略易失败 → 自动减仓避险。"
            ),
            "where": "risk_manager.calc_position_size 里的 VIX 乘数",
        },
    },

    # ===================================================================
    "🛡 风控规则 Risk Management": {
        "RISK_PER_TRADE": {
            "name": "Per-Trade Risk Cap",
            "summary": "单笔最多亏账户的多少 %",
            "explain": (
                "当前 5%，账户 $5000 → 单笔风险 = $250。\n"
                "如果止损被打满最多亏 $250。\n\n"
                "  qty = ($5000 × 5%) / 止损距离 = $250 / (entry - stop)\n\n"
                "自适应仓位会基于近 30 笔 Sortino 自动调:\n"
                "  Sortino > 5  → 风险 × 1.25 (= 6.25%)\n"
                "  Sortino < 0  → 风险 × 0.5  (= 2.5%)"
            ),
            "where": ".env: RISK_PER_TRADE (ACCOUNT_USD=$5000)",
            "value": "0.05 (5%) → $250/笔",
        },
        "MAX_POSITIONS / MAX_POSITION_PCT": {
            "name": "Concurrent Position Caps",
            "summary": "同时最多几仓 + 每仓最大多少 %",
            "explain": (
                "  MAX_POSITIONS=5         最多 5 仓\n"
                "  MAX_POSITION_PCT=0.50   每仓名义市值上限 50% 账户 ($2500)\n\n"
                "50% 是'封顶',不是目标 — 实际每仓大小由风险决定\n"
                "(= $250 / 止损距离),MAX_POSITION_PCT 只防止单仓过大。\n\n"
                "注意 5 × 50% = 250%:Oracle 不查现金,多仓的名义市值确实可能叠到\n"
                ">100% (= 隐性杠杆,见'双引擎回测'章)。真实 $5000 现金账户在\n"
                "enforce_cash 镜头下会被现金墙挡住,买不起就不买。"
            ),
            "where": ".env: MAX_POSITIONS, MAX_POSITION_PCT",
            "value": "5 仓 / 每仓上限 50%",
        },
        "DD 断路器 (Drawdown Circuit Breaker)": {
            "name": "Account Drawdown Circuit Breaker",
            "summary": "账户回撤太大时自动减仓 / 停盘",
            "explain": (
                "实时跟踪账户级 DD (= peak_equity - current_equity):\n\n"
                "  DD ≥ 10% (DD_SIZE_CUT_PCT)  → 新仓位 × 0.5\n"
                "  DD ≥ 18% (DD_HALT_PCT)      → 完全停开新仓\n\n"
                "现有仓位不受影响 (止损会自然触发)。\n"
                "DD 回到 < 10% 后自动恢复全仓。\n\n"
                "180 天回测显示这能把 11 月那种灾难月 -17% 压到 -8%。"
            ),
            "where": "src/risk_manager.py: _dd_size_multiplier",
            "value": "减半 10% / 停盘 18%",
        },
        "3 连亏熔断": {
            "name": "3-Day Loss Streak Halt",
            "summary": "连续 3 个交易日亏损就强制暂停",
            "explain": (
                "防止“越亏越激进”的心理偏差。\n\n"
                "每次平仓后:\n"
                "  实现亏损 + 今天 last_close_day 不同 → 连亏 +1\n"
                "  实现盈利 → 连亏清 0\n\n"
                "达到 3 自动 halted=True，需要人工或 daily 重置才恢复。"
            ),
            "where": "src/risk_manager.py: record_trade_close",
        },
        "组合热度 (Portfolio Heat)": {
            "name": "Portfolio Heat Cap",
            "summary": "所有持仓的总风险敞口不超账户 8%",
            "explain": (
                "Heat = Σ (entry - stop) × qty   (所有未平仓)\n\n"
                "上限 8% × ACCOUNT_USD = $360\n\n"
                "即使每仓 2% 风险 × 10 仓 = 20%，但部分仓位\n"
                "已经盈利、移动止损上提后实际风险变小了 — heat 才是\n"
                "真正的“全员被打止损损失多少”的衡量。\n\n"
                "Heat 超过 8% 时新仓被拒。"
            ),
            "where": "src/portfolio.py: heat_check",
            "value": "8% × account",
        },
        "MAX_GAP_PCT": {
            "name": "Overnight Gap Filter",
            "summary": "隔夜跳空太大就不入场",
            "explain": (
                "今日开盘 vs 昨日收盘:\n"
                "  跳空 > 2.5% → 拒入\n\n"
                "原因:\n"
                "  • 跳空向上 → 追高风险，可能立刻均值回归\n"
                "  • 跳空向下 → 接飞刀，趋势可能继续向下"
            ),
            "where": ".env: MAX_GAP_PCT (= indicators.check_gap)",
            "value": "2.5",
        },
        "板块集中度 (Sector Cap)": {
            "name": "Sector Concentration Limit",
            "summary": "同一板块最多几仓",
            "explain": (
                "MAX_PER_SECTOR = 4\n\n"
                "防止全仓 tech 一起跌的尾部风险。同板块 4 仓 × 10% = 40% 最多。\n\n"
                "板块归属看 src/sector.py 的 SECTOR_MAP — 已对齐当前 watchlist。"
            ),
            "where": "src/sector.py",
            "value": "4 per sector",
        },
        "财报屏蔽 (Earnings Block)": {
            "name": "Earnings Window Block",
            "summary": "未来 2 天有财报的股不开新仓",
            "explain": (
                "财报前后的隐含波动率极高，趋势/技术指标完全失效。\n"
                "从 Yahoo Finance 拉每只股的下次财报日，未来 ≤ 2 天的全部拦截。\n\n"
                "现有仓位不受影响 — 这是入场过滤，不是强平规则。"
            ),
            "where": "src/earnings.py",
        },
    },

    # ===================================================================
    "🎚 出场与加仓 Exits & Scaling": {
        "R — 风险单位 (R-Unit)": {
            "name": "R-Unit (Initial Per-Share Risk)",
            "summary": "一切出场/加仓的度量尺:1R = 入场价 − 初始止损",
            "explain": (
                "R = entry − initial_stop = SL_ATR_MULT × ATR(入场时)\n\n"
                "几乎所有出场和加仓规则都用 R 描述距离,而不是用 %:\n"
                "  • scale-out 在 +3R / +6R 分批止盈\n"
                "  • 保本止损在 +1R 触发\n"
                "  • 移动止损在 +1R 之后才启动\n"
                "  • 金字塔加仓要求浮盈 ≥ +0.5R\n\n"
                "好处:不管股价高低、波动大小,'1R' 永远等于'当初愿意亏的那一份',\n"
                "规则在不同股票之间可比 (R-Multiple 同理)。"
            ),
            "where": "src/backtest_v3.py: r_unit = cfg.sl_atr_mult × atr0;实盘 executor 同公式",
            "value": "1R = 3.5 × ATR (SL_ATR_MULT=3.5)",
        },
        "SL_ATR_MULT / TP_ATR_MULT": {
            "name": "Stop / Take-Profit ATR Multipliers",
            "summary": "止损、止盈各离入场多少个 ATR",
            "explain": (
                "  止损 stop = entry − SL_ATR_MULT × ATR\n"
                "  止盈 tp   = entry + TP_ATR_MULT × ATR\n\n"
                "当前 .env:\n"
                "  SL_ATR_MULT = 3.5  → 止损约 3.5 个 ATR (宽止损,少被噪音洗下车)\n"
                "  TP_ATR_MULT = 8.0  → 止盈约 8 个 ATR (很宽,博肥尾大行情)\n\n"
                "宽 SL + 很宽 TP = 用'低胜率'换'大盈亏比'的趋势打法。\n"
                "2026-05 sweep:更宽的 SL/TP 反而 +$5/天,因为减少了假止损。"
            ),
            "where": ".env: SL_ATR_MULT, TP_ATR_MULT;src/backtest_v3.py 出场解析",
            "value": "SL 3.5 / TP 8.0 ATR",
        },
        "Scale-out — 分批止盈 (3 档)": {
            "name": "Scale-Out (3-Tranche Partial Exit)",
            "summary": "把仓位拆 3 份,边涨边分批落袋",
            "explain": (
                "入场时把仓位分成 3 份 (各 1/3):\n"
                "  第 1 档  +TP1_R × R = +3R          卖 1/3 → 标 'TP1'\n"
                "  第 2 档  +TP2_R × R = +6R          卖 1/3 → 标 'TP2'\n"
                "  runner   +TP_ATR_MULT × ATR = +8 ATR  最后 1/3,固定止盈、不移动\n\n"
                "用意:'落袋为安' 与 '让赢家跑' 的折中 — 先锁一部分利润,\n"
                "再留一部分博更大行情,降低到手利润被回吐的风险。\n\n"
                "注意当前参数:TP1(+10.5 ATR)/TP2(+21 ATR)其实高于 runner 的\n"
                "止盈(+8 ATR),所以普通慢涨行情整仓会先在 +8 ATR 全平,两档只在\n"
                "单根 K 线暴涨/跳空时才触发。回测:180d +$2/天、360d 持平、回撤不变\n"
                "— 唯一一个'从不输基线'的无杠杆改动,所以才打开它。"
            ),
            "where": ".env: USE_SCALE_OUT, TP1_R, TP2_R;实盘 src/executor.py 软管理路径",
            "value": "ON · TP1_R=3.0 / TP2_R=6.0",
        },
        "Pyramiding / Stacking — 金字塔加仓": {
            "name": "Pyramiding (Add-On Stacking)",
            "summary": "给已经在赚的持仓加仓 (实盘 open_position 的合并)",
            "explain": (
                "一只已持有的票若再次触发买入信号,且当前浮盈 ≥ +0.5R、\n"
                "未超过每标的 5 层上限 → 把加仓单合并进原仓:\n"
                "  • 成本价 = 原仓与加仓的加权平均\n"
                "  • 止损/止盈 = 只升不降\n"
                "  • 加仓不占 MAX_POSITIONS 名额 (是同一个持仓);但要扣现金\n\n"
                "回测结论 (真实 $5k 现金账户):反而亏 —\n"
                "  180d +55→+28/天,  360d +11→+6.5/天,  回撤更大\n"
                "  '现金受限入场' 暴增 → 加仓把新机会需要的现金吃光了。\n"
                "所以 use_pyramiding 默认 OFF,实盘没开 (只是建模出来供回测对比)。"
            ),
            "where": ".env: USE_PYRAMIDING (默认 false);src/backtest_v3.py: _stack_gate / "
                     "_merge_addon;实盘 src/executor.py: open_position",
            "value": "OFF · 上限 5 层/票 · 门槛 +0.5R",
        },
        "保本止损 (Breakeven Stop)": {
            "name": "Breakeven Stop",
            "summary": "浮盈到 +1R 就把止损提到入场价 (从此最坏平手)",
            "explain": (
                "价格触及 entry + breakeven_trigger_r × R (= +1R) 后,\n"
                "把止损上提到入场价 — 之后最坏也是平手出场,标 'BREAKEVEN'。\n\n"
                "回测引擎里有这个开关,但当前 .env 没开 (USE_BREAKEVEN_STOP 默认 false)。\n"
                "可以在 sweep 里试;默认关是为了和实盘行为保持一致。"
            ),
            "where": "src/backtest_v3.py / backtest.py: use_breakeven_stop, breakeven_trigger_r",
            "value": "OFF (触发 = +1R)",
        },
        "吊灯移动止损 (Chandelier Trail)": {
            "name": "Chandelier Trailing Stop",
            "summary": "止损跟着最高价上移:peak − N×ATR,只升不降",
            "explain": (
                "启动后:止损 = 持仓期间最高价 − trail_atr_mult × ATR,只升不降。\n"
                "只有浮盈过 +trail_activate_r × R (= +1R) 才启动,早期给行情留空间。\n\n"
                "  trail_atr_mult = 3.0  (跟踪 3 个 ATR)\n\n"
                "当前 .env 没开 (USE_TRAILING_STOP 默认 false) — 这也是为什么 scale-out\n"
                "的 runner 是'固定止盈不移动'。实盘打开 scale-out 后,正是用固定止盈\n"
                "取代了移动止损,避免被正常回调噪音甩下车。"
            ),
            "where": "src/backtest_v3.py: trail_active, trail_atr_mult, trail_activate_r",
            "value": "OFF (trail 3×ATR, 启动 +1R)",
        },
        "MAX_HOLD_DAYS — 最长持有": {
            "name": "Max Hold (Time Stop)",
            "summary": "持仓超过 N 个交易日还没了结就强平",
            "explain": (
                "持仓达到 MAX_HOLD_DAYS 个'交易日'(business days,跳过周末/假日)\n"
                "还没碰到止损/止盈 → 按收盘市价强平,标 'MAX_HOLD'。\n\n"
                "用意:这是短线 swing 策略,不让资金被一只走平的票长期占住名额。\n"
                "executor 现在按 business days 计,和回测口径一致。"
            ),
            "where": ".env: MAX_HOLD_DAYS;src/backtest_v3.py 出场解析",
            "value": "7 交易日",
        },
    },

    # ===================================================================
    "📈 性能指标 Performance Metrics": {
        "Sharpe Ratio": {
            "name": "Sharpe Ratio",
            "summary": "风险调整后收益 (越高越好)",
            "explain": (
                "Sharpe = (年化收益 - 无风险利率) / 年化波动率\n\n"
                "解读:\n"
                "  < 1   策略一般\n"
                "  1-2   合格 (大多对冲基金)\n"
                "  2-3   优秀\n"
                "  3+    可能过拟合，要警惕\n\n"
                "本系统用日权益级 Sharpe (不是逐笔级，那个虚高)。"
            ),
            "where": "src/metrics.py: sharpe_ratio",
            "value": "180d 实测: 2.53",
        },
        "Sortino Ratio": {
            "name": "Sortino Ratio",
            "summary": "只罚下行波动的 Sharpe (更诚实)",
            "explain": (
                "Sortino = 年化收益 / 下行波动率\n\n"
                "区别 Sharpe: 上涨波动不算“风险”，\n"
                "策略大涨日不会拉低分数。\n\n"
                "通常比 Sharpe 高 1.5-2x。本系统 180d Sortino 3.48 = 极佳。"
            ),
            "where": "src/metrics.py: sortino_ratio",
            "value": "180d 实测: 3.48",
        },
        "Calmar Ratio": {
            "name": "Calmar Ratio",
            "summary": "年化收益 / 最大回撤",
            "explain": (
                "Calmar = CAGR / |Max DD|\n\n"
                "衡量\"承担多少痛苦换来多少收益\"。\n"
                "  > 1   合格\n"
                "  > 3   优秀\n"
                "  > 5   excellent\n\n"
                "实测: 180 天 CAGR 49.7% / Max DD 9.31% = Calmar 5.34"
            ),
            "where": "src/metrics.py: calmar_ratio",
        },
        "Profit Factor": {
            "name": "Profit Factor",
            "summary": "总赢钱 / 总亏钱",
            "explain": (
                "PF = Σ(盈利交易) / |Σ(亏损交易)|\n\n"
                "  < 1.0   亏钱策略\n"
                "  1.0-1.3 边际\n"
                "  1.3-2.0 健康\n"
                "  > 2.0   出色\n\n"
                "180d 实测 1.28 = 边际偏健康，胜率支撑利润。"
            ),
            "where": "src/metrics.py: profit_factor",
        },
        "Win Rate": {
            "name": "Win Rate",
            "summary": "盈利交易占比",
            "explain": (
                "= 盈利笔数 / 总笔数 × 100%\n\n"
                "  > 40%   只要盈亏比好就能赚钱\n"
                "  > 50%   健康\n"
                "  > 60%   优秀\n\n"
                "本系统 180d: 60.9% — 比绝大多数零售策略高。"
            ),
            "where": "src/metrics.py: compute_full_metrics",
        },
        "Expectancy": {
            "name": "Per-Trade Expectancy",
            "summary": "平均每笔交易期望收益 ($)",
            "explain": (
                "Expectancy = Σ pnl / N\n\n"
                "正数 = 长期赚钱。\n\n"
                "实测 +$1.70/笔 (每笔小赚)，乘 584 笔 = +$990 总收益。"
            ),
            "where": "src/metrics.py: expectancy",
        },
        "Kelly Fraction": {
            "name": "Kelly Criterion",
            "summary": "理论最优下注比例",
            "explain": (
                "f* = W - (1-W)/R\n"
                "  W = 胜率, R = 平均赢/平均亏\n\n"
                "本系统实测约 +8-25%，意思是\"理论可以投资本的 8-25% 单笔\"。\n"
                "但 Full Kelly 在散户身上极易破产 — 实际只用 Half Kelly。\n"
                "本系统 RISK_PER_TRADE = 2% 远低于 Kelly 建议。"
            ),
            "where": "src/metrics.py: kelly_fraction",
        },
        "Max Drawdown (MDD)": {
            "name": "Maximum Drawdown",
            "summary": "账户从顶点跌到谷底最深的百分比",
            "explain": (
                "= max(peak - current) / peak × 100%\n\n"
                "这是策略\"最痛苦的时刻\"。心理承受:\n"
                "  < 5%   不痛\n"
                "  5-10%  正常\n"
                "  10-20% 心理压力大 (90% 散户在这阶段放弃)\n"
                "  > 20%  灾难\n\n"
                "180d 实测 9.31% — 完全在可承受区间。"
            ),
            "where": "src/metrics.py: max_drawdown",
        },
        "Ulcer Index": {
            "name": "Ulcer Index",
            "summary": "回撤深度的均方根 (回撤痛苦程度)",
            "explain": (
                "UI = √(Σ DD%² / N)\n\n"
                "和 Max DD 不同 — UI 反映\"长期处于回撤\"的痛苦。\n"
                "短暂大回撤 vs 长期小回撤，UI 可能一样。\n\n"
                "  UI < 5    平滑\n"
                "  UI 5-10   正常\n"
                "  UI > 10   颠簸"
            ),
            "where": "src/metrics.py: ulcer_index",
        },
        "Monte Carlo P(profitable)": {
            "name": "Monte Carlo Bootstrap",
            "summary": "重排交易顺序 1000 次后赚钱的概率",
            "explain": (
                "把所有交易的 pnl 列表随机重排 1000 次，每次重算最终权益。\n"
                "P(profitable) = 1000 次里最终 > starting_capital 的比例。\n\n"
                "  > 95%   策略稳健\n"
                "  85-95%  合格\n"
                "  < 85%   边际，可能运气成分大\n\n"
                "实测 99.5% = 强稳健性证据。"
            ),
            "where": "src/metrics.py: monte_carlo_bootstrap",
        },
        "R-Multiple": {
            "name": "R-Multiple",
            "summary": "盈利 / 初始风险",
            "explain": (
                "R = (exit - entry) / (entry - stop)\n\n"
                "每笔交易换成\"赢/亏几个 R\":\n"
                "  +1R  按原止损距离赚 1 倍\n"
                "  -1R  打到止损\n"
                "  +3R  大胜\n\n"
                "比 % 收益更可比，因为消除了仓位大小影响。\n"
                "avg_R > 0.3 算合格，>0.5 优秀。"
            ),
            "where": "src/portfolio.py: record_close (r_multiple)",
        },
        "MFE / MAE": {
            "name": "Max Favourable / Adverse Excursion",
            "summary": "持仓期间最高浮盈/最低浮亏",
            "explain": (
                "  MFE = 持仓期间最大未实现盈利\n"
                "  MAE = 持仓期间最大未实现亏损\n\n"
                "用途:\n"
                "  • 平均 MFE 大但 pnl 小 → TP 设得太近，错过利润\n"
                "  • 平均 MAE 接近 SL → 止损够紧，没被噪音洗\n\n"
                "本系统每笔记录 — 长期数据可改进 TP/SL 设置。"
            ),
            "where": "src/portfolio.py: record_close",
        },
    },

    # ===================================================================
    "🤖 AI 与自动化 AI & Automation": {
        "ML XGBoost 验证层": {
            "name": "ML Validation Layer (XGBoost)",
            "summary": "用历史数据预测信号的成功概率",
            "explain": (
                "训练集:过去 365 天所有 6 因子打分 + 实际\n"
                "“TP-before-SL within 65 bars”的标签。\n\n"
                "每次扫描:输入当前因子分数 → ML 模型输出概率\n"
                "  proba < 0.35  → 否决，跳过\n"
                "  0.35-0.55     → 半仓\n"
                "  > 0.55        → 全仓\n\n"
                "每月 1 号 02:00 ET 自动用最新数据重训。"
            ),
            "where": "src/ml/predict.py + train.py",
            "value": "ML_BLEND_WEIGHT=0.30 (每月重训)",
        },
        "Gemini AI 验证": {
            "name": "Gemini AI Context Veto",
            "summary": "LLM 检查新闻/财报/宏观环境",
            "explain": (
                "技术指标看不到\"今天 NVDA 被 SEC 调查\"这种事件。\n"
                "AI 拉 Tavily 最近 3 天的 ticker 新闻 + 宏观新闻，"
                "问 Gemini 这些信息会不会让信号失效。\n\n"
                "返回 pass / veto。veto 直接跳过这只股。\n\n"
                "每次扫描限 5 次调用 — 不会爆 Gemini 1500 req/day 免费额度。"
            ),
            "where": "src/ai_validator.py",
            "value": "gemini-3.5-flash",
        },
        "自适应仓位 (Adaptive Sizing)": {
            "name": "Adaptive Position Sizing",
            "summary": "近 30 笔表现自动调风险大小",
            "explain": (
                "每次开仓前查近 30 笔已平仓的 Sortino:\n\n"
                "  Sortino > 5     → risk × 1.25 (热手期加仓)\n"
                "  2-5             → 1.00 基线\n"
                "  0-2             → 0.75 (降温)\n"
                "  < 0             → 0.50 (保护模式)\n"
                "  < 10 笔         → 1.00 (热身)\n\n"
                "这是 DD 断路器之前的“柔性”防御 — DD 还没到 10% \n"
                "就先减仓，比断路器更早起作用。"
            ),
            "where": "src/adaptive_sizing.py",
        },
        "自适应黑名单 (Adaptive Blacklist)": {
            "name": "Adaptive Symbol Blacklist",
            "summary": "持续亏钱的股自动观察，恢复了自动剔出",
            "explain": (
                "每天 23:00 ET 查每只股近 60 笔累计 PnL。\n\n"
                "加入条件 (动态):\n"
                "  组合 Sortino > 3   → PnL < -$25 就拉黑 (严)\n"
                "  Sortino 0-3       → PnL < -$50 才拉黑\n"
                "  Sortino < 0        → PnL < -$200 才拉黑 (宽，怪市场不怪股)\n\n"
                "拉黑后 30-90 天复评一次。改善了立刻剔除；还差就延长。\n\n"
                "这是\"靠 AI 进行变通\" — 不是硬规则。"
            ),
            "where": "src/blacklist.py",
        },
        "yfinance 自动选股": {
            "name": "Automatic Watchlist Refresh",
            "summary": "每周日从 S&P 500 自动选最强 30 只",
            "explain": (
                "每周日 22:00 ET 自动跑:\n"
                "  1. 拉 S&P 500 全部 503 只\n"
                "  2. 过滤:日均成交量 ≥ 500 万、价格 ≥ $15、ATR% ≥ 1.5%\n"
                "  3. 要求:5d 和 20d 收益都正 (避免下跌股)\n"
                "  4. 排序: 5d 相对 SPY 强度 + 20d 动量 + ATR%\n"
                "  5. 锚定 10 只大盘股 + 排名前 20 = 30 只\n"
                "  6. 自动跳过黑名单股\n\n"
                "你的 watchlist 永远跟着市场轮动走。"
            ),
            "where": "src/watchlist_updater.py",
        },
        "错过任务自动补 (Catchup)": {
            "name": "Missed Task Catchup",
            "summary": "电脑关机错过的定时任务，开机自动补",
            "explain": (
                "scheduler 启动时检查 data/cron_state.json 上次运行时间:\n"
                "  • Weekly 任务  错过 ≥ 7 天 → 补一次\n"
                "  • Monthly 任务 错过 ≥ 28 天 → 补一次\n"
                "  • Daily 任务   错过 ≥ 1 天 → 补一次\n\n"
                "多次错过只补一次 (coalesce)，不会一次跑十次。"
            ),
            "where": "src/cron_state.py",
        },
    },

    # ===================================================================
    "🔬 算法 Algorithms": {
        "Optuna 贝叶斯优化": {
            "name": "Optuna TPE Hyperparameter Search",
            "summary": "用 TPE 算法自动找最优参数",
            "explain": (
                "TPE = Tree-structured Parzen Estimator\n\n"
                "比网格搜索快 10 倍 — 每次根据历史 trial 结果选下一组参数。\n\n"
                "搜索空间:\n"
                "  threshold       60-80   (step 2)\n"
                "  tp_atr_mult     1.0-3.0 (step 0.25)\n"
                "  sl_atr_mult     1.5-3.5 (step 0.25)\n"
                "  max_gap_pct     2.0-5.0 (step 0.5)\n"
                "  base_slip_bp    1.0-5.0 (step 0.5)\n\n"
                "目标函数:0.7 × 平均 OOS Sortino + 0.3 × worst-fold\n"
                "每月 1 号 03:00 ET 自动跑，结果推 Telegram (不自动应用)。"
            ),
            "where": "src/optimizer.py",
        },
        "时间步进 Portfolio 模拟": {
            "name": "Time-Stepped Portfolio Simulator",
            "summary": "回测按时间戳交错处理所有股 (live-parity)",
            "explain": (
                "之前回测是每只股独立跑，结果忽略 MAX_POSITIONS 约束。\n"
                "现在按时间戳交错:\n\n"
                "  1. 收集所有股 × 所有 bar 成事件流\n"
                "  2. 按时间戳排序\n"
                "  3. 一个个事件处理\n"
                "  4. 共享 portfolio state + open_trades dict\n"
                "  5. MAX_POSITIONS / DD 断路器都按账户级生效\n\n"
                "结果:回测 PnL 跟实盘行为 100% 一致。"
            ),
            "where": "src/backtest.py: simulate_time_stepped",
        },
        "Walk-Forward 验证": {
            "name": "Walk-Forward Out-of-Sample Validation",
            "summary": "把回测期切 3 折，每折独立看表现",
            "explain": (
                "180 天数据切 3 折:\n"
                "  Fold 1: Day 0-59\n"
                "  Fold 2: Day 60-119\n"
                "  Fold 3: Day 120-179\n\n"
                "每折独立算 Sortino。最优参数 = mean × 0.7 + worst × 0.3\n\n"
                "防止过拟合到某个特定时期。\n"
                "如果 worst-fold Sortino 是负的 → 策略可能不稳。"
            ),
            "where": "src/optimizer.py: _evaluate_params",
        },
        "真实成交模拟 (Realistic Limit Fills)": {
            "name": "Realistic Limit Order Fill Simulation",
            "summary": "限价单可能成交不了",
            "explain": (
                "之前回测假定限价单永远成交在 next_bar.open — 太乐观。\n\n"
                "真实逻辑:\n"
                "  if next_bar.open ≤ limit_price:\n"
                "      成交于 open (好运)\n"
                "  elif next_bar.low ≤ limit_price:\n"
                "      成交于 limit_price (本来就要)\n"
                "  else:\n"
                "      跳空过去了，没成交 → 信号丢弃\n\n"
                "暴露真实情况:约 15-20% 的信号其实抓不到。"
            ),
            "where": "src/backtest.py: simulate_time_stepped",
        },
        "ATR 缩放滑点": {
            "name": "ATR-Scaled Slippage",
            "summary": "波动大的股滑点也大",
            "explain": (
                "之前固定 5 bp 滑点 — 跟现实差距大。\n\n"
                "新模型: slip_bp = base + k × (ATR% × 100)\n"
                "  base = 1.5 bp\n"
                "  k    = 0.5\n\n"
                "示例:\n"
                "  ATR 2% 的稳定股 → 1.5 + 0.5×2 = 2.5 bp\n"
                "  ATR 5% 的波动股 → 1.5 + 0.5×5 = 4.0 bp\n\n"
                "止损穿透时滑点 × 2 (sl_breakaway_mult)。"
            ),
            "where": "src/backtest.py: _slip_for",
        },
    },

    # ===================================================================
    "🔬 双引擎回测 Dual-Engine & 现金诚实度": {
        "两个引擎 (Oracle vs V3)": {
            "name": "Two Backtest Engines (Differential Testing)",
            "summary": "两套独立写的回测引擎互相对账,防止单一实现里的隐藏 bug",
            "explain": (
                "回测结论要敢拿来调实盘参数,就不能只信一套代码。所以有两个引擎:\n\n"
                "  • Oracle  simulate_time_stepped (src/backtest.py)\n"
                "            — 冻结的'标准答案',GUI 跑的就是它\n"
                "  • V3      simulate_v3 (src/backtest_v3.py)\n"
                "            — 完全独立重写的第二实现\n\n"
                "差分测试 (differential testing):让 V3 在'机制验证'模式下逐笔、\n"
                "逐美元复现 Oracle。两套独立代码同时算错同一个数的概率极低,\n"
                "所以一旦对得上,就对结果有信心。\n\n"
                "scripts/engine_compare.py 是常驻对账脚本:净值差 / 笔数差只要不为 0\n"
                "就 exit 1 报警。当前:净差 $0.00、笔数差 0 (完全一致)。"
            ),
            "where": "src/backtest.py (oracle) vs src/backtest_v3.py (v3);scripts/engine_compare.py",
            "value": "PARITY OK · Δ净值 $0.00 · Δ笔数 0",
        },
        "parity_mode — 机制验证": {
            "name": "parity_mode (Mechanism Validation Lens)",
            "summary": "V3 的'对账模式':关现金墙、关加仓,只验证机制和 Oracle 一致",
            "explain": (
                "同一份 simulate_v3 代码,两个镜头看回测。parity_mode=True 是第一个:\n\n"
                "  • 不检查现金 (和 Oracle 一样按静态 $5000 算仓位)\n"
                "  • 关闭金字塔加仓\n"
                "  • 目标:逐笔、逐美元复现 Oracle\n\n"
                "用意:把'机制对不对'和'策略赚不赚'两件事拆开验证。先证明 V3 的\n"
                "出场/排序/风控机制和冻结的 Oracle 完全一样 (机制验证),再打开现金\n"
                "约束去看真实账户表现 (策略验证)。机制这层不过,策略数字就没意义。"
            ),
            "where": "src/backtest_v3.py: simulate_v3(parity_mode=True)",
            "value": "对账时 ON;真实账户评估时 OFF",
        },
        "enforce_cash — 策略验证 / 真实现金账户": {
            "name": "enforce_cash (Real $5k Cash-Balance Lens)",
            "summary": "V3 的'真实现金模式':真的扣现金、买不起就不买",
            "explain": (
                "simulate_v3 的第二个镜头 enforce_cash=True,模拟真实的 $5000 现金账户:\n\n"
                "  • 每次开仓 / 加仓真的从现金余额里扣钱\n"
                "  • 现金不够 → 这个信号买不起,直接丢弃 (现金墙)\n"
                "  • 跟踪逐 bar 的浮动盈亏 (MTM-DD)\n\n"
                "为什么需要它:Oracle 按静态 $5000 算每仓,从不查'账上还剩多少现金',\n"
                "于是会同时持有远超 $5000 的名义市值 (= 隐性杠杆)。真实 SIMULATE\n"
                "账户只有 $5000 现金,买不起就是买不起。这个镜头把那层免费杠杆\n"
                "拿掉,给出'真账户能复制'的诚实读数。"
            ),
            "where": "src/backtest_v3.py: simulate_v3(enforce_cash=True)",
            "value": "真实账户评估时 ON",
        },
        "MTM-DD — 浮动回撤 (Mark-to-Market Drawdown)": {
            "name": "Mark-to-Market Drawdown",
            "summary": "把未平仓也按每根 K 线收盘价计价后的回撤 (比已实现回撤诚实)",
            "explain": (
                "普通'已实现回撤'只在平仓那一刻才把亏损计入权益曲线 — 持仓浮亏被\n"
                "藏起来,回撤看着很漂亮。MTM-DD (mark-to-market) 不一样:\n\n"
                "  • 每根 bar 都把所有未平仓按当根收盘价重新计价\n"
                "  • 浮盈浮亏实时进权益曲线\n"
                "  • 回撤 = 这条'含浮动'曲线的峰谷差\n\n"
                "意义:这才是你账户屏幕上真实看到的数字,也是你真正要承受的痛。\n"
                "报告里凡是带 MTM 的回撤都比已实现回撤更大 / 更诚实。"
            ),
            "where": "src/backtest_v3.py: _RealizedDD / 逐 bar 计价",
            "value": "报告里标 MTM-DD",
        },
        "隐性杠杆 / 峰值毛敞口 (pkGr%)": {
            "name": "Implicit Leverage / Peak Gross Exposure",
            "summary": "Oracle 按静态 $5000 算每仓、从不查现金,于是悄悄上了杠杆",
            "explain": (
                "Oracle 每笔都按固定 $5000 × 风险% 算仓位,但从不检查'账上现金\n"
                "够不够'。于是它能同时持有好几个满仓位,名义市值远超 $5000 —\n"
                "等于免费用了杠杆,自己却不知道。\n\n"
                "峰值毛敞口 pkGr% = 同时持仓的名义市值峰值 / $5000:\n"
                "  • 100% = 刚好满仓,无杠杆\n"
                "  • 实测见过 252% – 534% → 相当于 2.5×–5× 杠杆\n\n"
                "真实 SIMULATE 现金账户没有这层杠杆,所以 Oracle 的漂亮收益有一部分\n"
                "是'账户其实复制不出来'的。enforce_cash 镜头就是用来戳破这一点。"
            ),
            "where": "src/backtest_v3.py: 峰值毛敞口统计 (pkGr%)",
            "value": "实测峰值 252% – 534% (≈2.5×–5×)",
        },
        "现金受限入场 (cashLim)": {
            "name": "Cash-Limited Entries",
            "summary": "因为现金不够而被丢弃的合格信号数 — '饿肚子'指标",
            "explain": (
                "在 enforce_cash 模式下,一个信号通过了全部 8 道漏斗、本该开仓,\n"
                "却因为账上现金不足而买不起 → 记一次'现金受限入场' (cashLim)。\n\n"
                "它是一个'饥饿'信号:数字越大,说明现金越常被占满、错过的好机会越多。\n\n"
                "典型用法:评估金字塔加仓时就是看它 —\n"
                "  加仓把现金吃光 → cashLim 暴增 (180d ×3.6 / 360d ×1.8)\n"
                "  → 新名字的机会被饿死 → 这正是加仓在 $5k 账户上反而亏钱的原因。"
            ),
            "where": "src/backtest_v3.py: enforce_cash 路径的 cash-limited 计数",
            "value": "报告里标 cashLim (越低越好)",
        },
        "诚实读数 ($/day @ MTM-DD)": {
            "name": "The Honest Read",
            "summary": "用真实现金 + 浮动回撤口径看策略,而不是被隐性杠杆美化的版本",
            "explain": (
                "把上面几件事合起来,就是评判一个'无杠杆改动'到底好不好的统一口径:\n\n"
                "  1. enforce_cash  — 真扣现金、买不起就不买 (去掉隐性杠杆)\n"
                "  2. MTM-DD        — 含浮动盈亏的诚实回撤\n"
                "  3. cashLim       — 有没有把现金占满、饿死新机会\n"
                "  4. $/day         — 每日净收益 (跨不同回测长度可比)\n\n"
                "一个改动只有在这个诚实口径下'不输基线'才会被打开。\n"
                "  • scale-out 过了 → 实盘打开 (USE_SCALE_OUT=true)\n"
                "  • pyramiding 没过 ($/day 掉、回撤升、cashLim 暴增) → 保持 OFF"
            ),
            "where": "scripts/engine_compare.py / pyramiding_compare.py 的对比口径",
            "value": "决策口径:真实现金 + MTM-DD + $/day",
        },
    },

    # ===================================================================
    "💱 订单与执行 Order Execution": {
        "限价单 (Limit Order)": {
            "name": "Limit Order",
            "summary": "以指定价格或更好的价格买/卖",
            "explain": (
                "市价单 (Market) = 立刻成交，但价格不确定\n"
                "限价单 (Limit)  = 价格保证，但可能不成交\n\n"
                "本系统所有交易都用限价单 — 控制成本。\n"
                "买单 limit = 信号触发时的 close price。"
            ),
            "where": "src/executor.py: place_limit_order",
        },
        "OCO Bracket (止损 + 止盈)": {
            "name": "One-Cancels-Other Bracket Order",
            "summary": "买入同时挂止损 + 止盈，任一触发则取消另一个",
            "explain": (
                "REAL 账户支持:\n"
                "  1. 买入限价单触发\n"
                "  2. 自动挂止损单 + 止盈限价单\n"
                "  3. 任意一个被触发 → 另一个自动取消\n\n"
                "好处:不需要程序一直监控价格也能止损。\n"
                "缺点:Paper Trading 不支持 → 用软止损代替。"
            ),
            "where": "src/executor.py: place_bracket",
        },
        "软止损 (Soft Stop)": {
            "name": "Soft Stop Loss (Polling)",
            "summary": "程序轮询价格，触发就市价平",
            "explain": (
                "用于 SIMULATE 账户或 REAL 但 bracket 失败的情况。\n\n"
                "每次扫描:\n"
                "  for each open_trade:\n"
                "      if last_price ≤ stop_loss:\n"
                "          市价平仓\n"
                "      elif last_price ≥ take_profit:\n"
                "          市价平一半 (TP 半仓策略)\n\n"
                "缺点:依赖程序运行 + 30 分钟才扫一次。"
            ),
            "where": "src/executor.py: manage_open_trades",
        },
        "Stale Order Cancel": {
            "name": "Stale Order Auto-Cancel",
            "summary": "5 分钟没成交的限价单自动撤",
            "explain": (
                "限价单挂出去 5 分钟没成交 → 自动撤销。\n\n"
                "原因:\n"
                "  • 市场动了，原信号已失效\n"
                "  • 占用预算额度，挡住新机会\n"
                "  • 撤销后下次扫描会重新评估"
            ),
            "where": "src/executor.py: cancel_stale_orders",
        },
        "Bid-Ask Spread Filter": {
            "name": "Bid-Ask Spread Filter",
            "summary": "价差太大就不入",
            "explain": (
                "spread% = (ask - bid) / mid × 100\n\n"
                "上限:0.5%。超过就拒入 — 流动性差，进出都吃亏。\n\n"
                "盘前盘后流动性差，spread 大，所以本系统 09:45 才开始扫。"
            ),
            "where": "src/main.py: SPREAD_MAX_PCT",
            "value": "0.5%",
        },
    },

    # ===================================================================
    "⏰ 定时任务 Scheduled Tasks": {
        "扫描循环 (Scan Loop)": {
            "name": "Main Scan Loop",
            "summary": "每 30 分钟扫描 watchlist 找信号",
            "explain": (
                "运行时段:美东 09:45 - 15:30 (避开开盘/收盘 30 分钟噪音)\n\n"
                "  1. 拉每只股最新 K 线\n"
                "  2. 6 因子打分\n"
                "  3. 8 道过滤漏斗\n"
                "  4. 风控 + 仓位计算\n"
                "  5. 限价单 + OCO 止损\n"
                "  6. Telegram 推送"
            ),
            "where": "src/main.py: run_loop + scan_once",
            "value": "30 min 间隔",
        },
        "每日黑名单复评 23:00": {
            "name": "Daily Blacklist Review",
            "summary": "重新审视黑名单股是否该剔出",
            "explain": (
                "每天美东 23:00 自动跑:\n"
                "  1. 查每个黑名单股近期 PnL\n"
                "  2. 改善了 → 移除\n"
                "  3. 仍差 → 延长复评期 (+15 天)\n"
                "  4. 再扫所有股，发现新输家就拉黑\n\n"
                "门槛随整体 Sortino 自动调整。"
            ),
            "where": "src/main.py: _daily_blacklist_review_job",
        },
        "周日 22:00 watchlist 刷新": {
            "name": "Weekly Watchlist Refresh",
            "summary": "每周自动选 30 只最强的 S&P 500 股",
            "explain": (
                "周日晚 22:00 (周一开盘前 11.5 小时):\n"
                "  1. 拉 S&P 500\n"
                "  2. 过滤 + 排序\n"
                "  3. 写到 config/watchlist.json\n"
                "  4. Telegram 推送前 15 只\n"
                "  5. 周一开市就用新名单"
            ),
            "where": "src/main.py: _weekly_watchlist_refresh_job",
        },
        "周日 22:30 回测健康检查": {
            "name": "Weekly Backtest Health Check",
            "summary": "用过去 90 天数据跑一次回测",
            "explain": (
                "每周日比 watchlist 刷新晚 30 分钟跑:\n"
                "  • 用新刷的 watchlist + 当前 .env 参数\n"
                "  • 跑 90 天 backtest\n"
                "  • Telegram 推 Sortino + WR + Max DD + P(profitable)\n\n"
                "策略劣化时这是第一个发现的信号。"
            ),
            "where": "src/main.py: _weekly_backtest_validation_job",
        },
        "月 1 号 02:00 ML 重训": {
            "name": "Monthly ML Retrain",
            "summary": "每月用最新数据重训 XGBoost 模型",
            "explain": (
                "每月 1 号 02:00 美东:\n"
                "  1. 拉最近 365 天数据\n"
                "  2. 训练 XGBoost\n"
                "  3. 保存新模型 + meta\n"
                "  4. Telegram 推训练集大小 + AUC + accuracy\n\n"
                "市场风格变化时这能让 ML 模型跟上。"
            ),
            "where": "src/main.py: _monthly_retrain_job",
        },
        "月 1 号 03:00 Optuna 调参": {
            "name": "Monthly Optuna Re-Optimization",
            "summary": "每月跑一次 Optuna 找最优参数",
            "explain": (
                "每月 1 号 03:00 (ML 重训后 1 小时):\n"
                "  1. 跑 30 个 Optuna trials\n"
                "  2. 用 60 天数据 × 3 折 OOS 验证\n"
                "  3. 保存最优参数到 data/optimizer/\n"
                "  4. Telegram 推 best_params + 跟当前 .env 对比\n\n"
                "重点:不自动写 .env — 你看 Telegram 决定改不改。\n"
                "连续 2-3 个月推同样方向才考虑应用。"
            ),
            "where": "src/main.py: _monthly_optuna_job",
        },
    },
}


def get_entry(category: str, term: str) -> dict | None:
    """Return the entry dict, or None if not found."""
    return GLOSSARY.get(category, {}).get(term)
