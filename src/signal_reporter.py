"""
Signal Reporter — 短线信号推送模块
====================================

两种推送模式:

  run_premarket()   08:30 ET，盘前一次
                    15-min K线 + 完整技术指标 + Gemini AI
                    每只股票一张完整信号卡片

  run_intraday()    09:30 / 10:00 / 10:30 / 11:00 / 11:30 ET（每 30 min）
                    5-min K线 + 快速技术指标，无 AI（频率太高会超配额）
                    全 watchlist 合并成一条压缩消息，快速扫读

Watchlist 管理（独立于交易 watchlist）:
  add_ticker(sym)      加入信号 watchlist
  remove_ticker(sym)   移除
  load_watchlist()     查看当前列表

CLI（在项目根目录运行）:
  python -m src.main signal list
  python -m src.main signal add NVDA
  python -m src.main signal remove NVDA

为什么不用 moomoo-trader 的评分模型:
  indicators.py 的 EMA20/50、ADX14 是为 daily/1H swing 校准的，
  在 5-min 上指标严重滞后。这里改用短线参数:
    RSI(7)         →  7×5min = 35 min 响应
    MACD(5/13/5)   →  最慢 EMA 65 min
    EMA9/21        →  45 / 105 min
    VWAP(日内)      →  当日成交量加权均价，日内基准线
    Stoch(9,3)     →  45 min K 线
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from moomoo import KLType

from .config import settings
from . import news_fetcher, notifier
from .moomoo_client import client as _moomoo_client

log = logging.getLogger(__name__)

# ─── WATCHLIST FILE ───────────────────────────────────────────────────────────

_WL_FILE: Path = settings.root / "config" / "signal_watchlist.json"

COMPANY_NAMES: dict[str, str] = {
    "AAPL": "Apple",      "MSFT": "Microsoft",  "NVDA": "NVIDIA",
    "TSLA": "Tesla",      "AMZN": "Amazon",     "GOOGL": "Alphabet",
    "META": "Meta",       "AMD":  "AMD",         "INTC": "Intel",
    "SPY":  "S&P500",     "QQQ":  "纳指ETF",    "MU":   "Micron",
    "SNDK": "SanDisk",    "ARM":  "Arm",         "PLTR": "Palantir",
    "AVGO": "Broadcom",   "TSM":  "台积电",      "NFLX": "Netflix",
    "JPM":  "摩根大通",   "COIN": "Coinbase",    "UBER": "Uber",
    "SMH":  "半导体ETF",  "QCOM": "Qualcomm",   "DDOG": "Datadog",
    "DELL": "Dell",       "SWKS": "Skyworks",    "SOFI": "SoFi",
}


def load_watchlist() -> list[str]:
    """Load from JSON file; fall back to SIGNAL_WATCHLIST env var."""
    if _WL_FILE.exists():
        try:
            data = json.loads(_WL_FILE.read_text())
            tickers = [s.strip().upper() for s in data.get("tickers", []) if s.strip()]
            if tickers:
                return tickers
        except Exception as e:
            log.warning("signal_watchlist.json parse error: %s", e)
    # Fallback: env var
    return list(settings.signal_watchlist)


def _save_watchlist(tickers: list[str]) -> None:
    data = {
        "_comment": "Managed via: python -m src.main signal add/remove/list",
        "tickers": tickers,
    }
    _WL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def add_ticker(sym: str) -> str:
    sym = sym.strip().upper()
    tickers = load_watchlist()
    if sym in tickers:
        return f"{sym} 已在信号 watchlist 中"
    tickers.append(sym)
    _save_watchlist(tickers)
    return f"✅ {sym} 已加入信号 watchlist（共 {len(tickers)} 支）"


def remove_ticker(sym: str) -> str:
    sym = sym.strip().upper()
    tickers = load_watchlist()
    if sym not in tickers:
        return f"{sym} 不在信号 watchlist 中"
    tickers.remove(sym)
    _save_watchlist(tickers)
    return f"🗑 {sym} 已从信号 watchlist 移除（剩余 {len(tickers)} 支）"


def list_tickers() -> str:
    tickers = load_watchlist()
    if not tickers:
        return "信号 watchlist 为空"
    names = [f"{t}({COMPANY_NAMES.get(t, t)})" for t in tickers]
    return "📋 信号 watchlist:\n" + "\n".join(f"  {i+1}. {n}" for i, n in enumerate(names))


# ─── SHORT-TERM INDICATORS (5-min / 15-min optimized) ────────────────────────

def _rsi(close: pd.Series, period: int = 7) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    val   = (100 - 100 / (1 + rs)).iloc[-1]
    return round(float(val), 1)


def _macd(close: pd.Series, fast=5, slow=13, signal=5) -> dict:
    ema_f = close.ewm(span=fast,   adjust=False).mean()
    ema_s = close.ewm(span=slow,   adjust=False).mean()
    m     = ema_f - ema_s
    sig   = m.ewm(span=signal, adjust=False).mean()
    hist  = m - sig
    return {
        "hist":       round(float(hist.iloc[-1]), 4),
        "cross_up":   bool(len(hist) >= 2 and hist.iloc[-2] < 0 and hist.iloc[-1] > 0),
        "cross_down": bool(len(hist) >= 2 and hist.iloc[-2] > 0 and hist.iloc[-1] < 0),
        "above":      bool(hist.iloc[-1] > 0),
    }


def _ema(close: pd.Series, period: int) -> float:
    return round(float(close.ewm(span=period, adjust=False).mean().iloc[-1]), 4)


def _bollinger(close: pd.Series, period=20) -> dict:
    sma   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    price = float(close.iloc[-1])
    cu, cl = float(upper.iloc[-1]), float(lower.iloc[-1])
    pct_b  = (price - cl) / (cu - cl) if (cu - cl) != 0 else 0.5
    return {
        "pct_b":    round(pct_b, 3),
        "breakout": "up" if price > cu else ("down" if price < cl else "inside"),
    }


def _stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k=9, d=3) -> dict:
    ll  = low.rolling(k).min()
    hh  = high.rolling(k).max()
    kv  = 100 * (close - ll) / (hh - ll + 1e-9)
    dv  = kv.rolling(d).mean()
    k_n = round(float(kv.iloc[-1]), 1)
    d_n = round(float(dv.iloc[-1]), 1)
    kp  = float(kv.iloc[-2]) if len(kv) >= 2 else k_n
    dp  = float(dv.iloc[-2]) if len(dv) >= 2 else d_n
    return {
        "k":          k_n,
        "d":          d_n,
        "cross_up":   bool(kp < dp and k_n > d_n and k_n < 35),
        "cross_down": bool(kp > dp and k_n < d_n and k_n > 65),
        "oversold":   k_n < 20,
        "overbought": k_n > 80,
    }


def _vwap_intraday(df: pd.DataFrame) -> float:
    """Rolling 20-bar VWAP — works on any timeframe."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    return round(float(
        pv.rolling(20).sum().iloc[-1] / df["volume"].rolling(20).sum().iloc[-1]
    ), 2)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period=14) -> float:
    prev = close.shift(1)
    tr   = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return round(float(tr.rolling(period).mean().iloc[-1]), 4)


def _volume_surge(volume: pd.Series, lookback=20) -> dict:
    avg   = float(volume.rolling(lookback).mean().iloc[-1])
    cur   = float(volume.iloc[-1])
    ratio = round(cur / avg, 2) if avg > 0 else 1.0
    return {"ratio": ratio, "surge": ratio >= 1.8}


def _momentum(close: pd.Series, period=6) -> float:
    if len(close) < period + 1:
        return 0.0
    return round((float(close.iloc[-1]) / float(close.iloc[-1 - period]) - 1) * 100, 2)


def _sr(df_higher: pd.DataFrame, lookback=10) -> dict:
    """Support / resistance from higher timeframe (daily) data."""
    r = df_higher.tail(lookback)
    return {
        "support":    round(float(r["low"].min()), 2),
        "resistance": round(float(r["high"].max()), 2),
    }


# ─── SCORING (0-10 buy / 0-10 sell) ──────────────────────────────────────────

ACTION_EMOJI = {
    "强烈买入": "🚀", "买入": "🟢", "观望": "🟡",
    "卖出": "🔴",    "强烈卖出": "❌",
}

RISK_EMOJI = {"低": "🟢", "中": "🟡", "高": "🔴"}
CONF_EMOJI = {"高": "💪", "中": "✋", "低": "⚠️"}


def _score(price, rsi, macd, ema9, ema21, boll, vol, stoch, vwap, mom
           ) -> tuple[int, int, list[str]]:
    buy, sell, alerts = 0, 0, []

    # RSI(7) — 快速超买超卖
    if rsi <= 22:     buy += 2; alerts.append(f"RSI超卖({rsi})")
    elif rsi <= 33:   buy += 1
    elif rsi >= 78:   sell += 2; alerts.append(f"RSI超买({rsi})")
    elif rsi >= 67:   sell += 1

    # MACD(5/13/5)
    if macd["cross_up"]:      buy += 2;  alerts.append("MACD金叉")
    elif macd["cross_down"]:  sell += 2; alerts.append("MACD死叉")
    elif macd["above"]:       buy += 1
    else:                     sell += 1

    # EMA9/21 短期趋势
    if ema9 > ema21 and price > ema9:    buy  += 1
    elif ema9 < ema21 and price < ema9:  sell += 1

    # VWAP — 日内机构基准
    if price > vwap * 1.003:    buy  += 1
    elif price < vwap * 0.997:  sell += 1

    # Bollinger Bands
    if boll["breakout"] == "up":    buy += 2;  alerts.append("BB突破上轨")
    elif boll["breakout"] == "down": sell += 2; alerts.append("BB跌破下轨")
    elif boll["pct_b"] < 0.08:  buy  += 1
    elif boll["pct_b"] > 0.92:  sell += 1

    # Volume surge
    if vol["surge"]:
        if mom > 0:   buy += 1;  alerts.append(f"放量{vol['ratio']}x↑")
        else:         sell += 1; alerts.append(f"放量{vol['ratio']}x↓")

    # Stochastic(9,3)
    if stoch["cross_up"]:     buy  += 1; alerts.append("Stoch底部金叉")
    elif stoch["cross_down"]: sell += 1; alerts.append("Stoch顶部死叉")
    elif stoch["oversold"]:   buy  += 1
    elif stoch["overbought"]: sell += 1

    # 6-bar 动能
    if mom >= 1.5:    buy  += 1
    elif mom <= -1.5: sell += 1

    return min(buy, 10), min(sell, 10), alerts


def _action_from_scores(buy: int, sell: int) -> str:
    net = buy - sell
    if net >= 5:    return "强烈买入"
    if net >= 3:    return "买入"
    if net <= -5:   return "强烈卖出"
    if net <= -3:   return "卖出"
    return "观望"


def _score_bar(score: int) -> str:
    s     = max(1, min(10, int(score)))
    emoji = "🟩" if s >= 7 else ("🟥" if s <= 4 else "🟨")
    return emoji * s + "⬜" * (10 - s) + f" *{s}/10*"


# ─── GEMINI AI (盘前 only) ────────────────────────────────────────────────────

def _call_gemini(prompt: str) -> Optional[dict]:
    try:
        from google import genai
    except ImportError:
        return None

    keys = list(settings.gemini_keys)
    if not keys:
        return None

    primary = settings.gemini_model or "gemini-2.5-flash"
    seen: set[str] = set()
    cascade = [m for m in [primary, "gemini-2.5-flash", "gemini-2.5-flash-lite"]
               if not (m in seen or seen.add(m))]  # type: ignore[func-returns-value]

    for model_name in cascade:
        for key in keys:
            for attempt in range(2):
                try:
                    client = genai.Client(api_key=key)
                    resp = client.models.generate_content(model=model_name, contents=prompt)
                    raw  = re.sub(r"```json|```", "", resp.text.strip()).strip()
                    result = json.loads(raw)
                    result["_model"] = model_name
                    return result
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        break
                    if attempt == 0 and any(x in err for x in ("503", "500", "UNAVAILABLE")):
                        time.sleep(4)
                        continue
                    break
    return None


def _gemini_signal(tech: dict, ticker_news: list, macro_news: list) -> dict:
    news_text  = "\n".join(
        f"- {n['title'][:80]}: {n['content'][:100]}" for n in ticker_news[:4]
    ) or "无近期新闻"
    macro_text = "\n".join(f"- {n['title'][:70]}" for n in macro_news[:3]) or "无宏观新闻"

    prompt = f"""你是专业短线交易员（持仓 日内~2天）。基于 15min K线技术指标 + 近期新闻，给出今日/明日交易信号。

【{tech['ticker']} ({tech['company']})】 现价 ${tech['price']} | 日涨跌 {tech['chg1']:+.2f}%

技术指标 (15min K线):
- RSI(7)={tech['rsi']} | MACD(5/13/5) hist={tech['macd']['hist']:+.4f} (金叉={tech['macd']['cross_up']}, 死叉={tech['macd']['cross_down']})
- EMA9={tech['ema9']:.2f} / EMA21={tech['ema21']:.2f} | BB%B={tech['boll']['pct_b']} ({tech['boll']['breakout']})
- Stoch K={tech['stoch']['k']} D={tech['stoch']['d']} (超买={tech['stoch']['overbought']}, 超卖={tech['stoch']['oversold']})
- VWAP=${tech['vwap']} | 量比={tech['vol']['ratio']}x | 6-bar动能={tech['mom']:+.2f}%
- ATR={tech['atr']} | 支撑=${tech['sr']['support']} | 压力=${tech['sr']['resistance']}
- 技术评分: 买 {tech['buy_score']}/10 | 卖 {tech['sell_score']}/10
- 警报: {', '.join(tech['alerts']) or '无'}

近3天个股新闻（优先权高于技术面）:
{news_text}

近3天宏观新闻:
{macro_text}

评分 1-10: 9-10=极强买 | 7-8=强买 | 6=买 | 5=观望 | 4=卖 | 2-3=强卖 | 1=极强卖
财报前3天必须观望/日内。新闻强正催化→7+；强负催化→3以下。

严格返回 JSON（无代码块）:
{{"score":整数1-10,"action":"强烈买入/买入/观望/卖出/强烈卖出","hold_period":"日内/隔夜/2天",
  "entry_low":数字,"entry_high":数字,"target_1":数字,"target_2":数字,"stop_loss":数字,
  "reason":"35字内核心理由","news_impact":"20字内","catalyst":"20字内",
  "risk_level":"低/中/高","confidence":"高/中/低","summary":"40字内执行建议"}}"""

    result = _call_gemini(prompt)
    if result:
        log.info("premarket AI [%s] %s → %s (%s/10)",
                 result.get("_model", "?"), tech["ticker"],
                 result.get("action"), result.get("score"))
        return result

    # Fallback: pure technical
    net   = tech["buy_score"] - tech["sell_score"]
    score = max(1, min(10, 5 + net // 2))
    p, atr_v = tech["price"], tech["atr"]
    action = _action_from_scores(tech["buy_score"], tech["sell_score"])
    return {
        "score": score, "action": action, "hold_period": "日内",
        "entry_low":  round(p - atr_v * 0.3, 2),
        "entry_high": round(p + atr_v * 0.3, 2),
        "target_1":   round(p + atr_v * 1.5, 2),
        "target_2":   round(p + atr_v * 2.5, 2),
        "stop_loss":  round(p - atr_v * 2.0, 2),
        "reason": "AI 不可用，仅技术面",
        "news_impact": "无", "catalyst": "未知",
        "risk_level": "中", "confidence": "低",
        "summary": "AI 暂不可用，参考技术指标判断。",
        "_model": "fallback",
    }


# ─── 盘前全面分析（完整卡片，带 AI）────────────────────────────────────────

def _build_premarket_card(tech: dict, ai: dict) -> str:
    score  = int(ai.get("score", 5))
    act_e  = ACTION_EMOJI.get(ai.get("action", "观望"), "🟡")
    risk_e = RISK_EMOJI.get(ai.get("risk_level", "中"), "🟡")
    conf_e = CONF_EMOJI.get(ai.get("confidence", "中"), "✋")
    arrow  = "📈" if tech["chg1"] >= 0 else "📉"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{tech['ticker']}* {tech['company']} | "
        f"${tech['price']} {arrow} {tech['chg1']:+.2f}%\n"
        f"\n"
        f"{act_e} *{ai.get('action', '?')}*  {_score_bar(score)}\n"
        f"   {ai.get('reason', '')}\n"
        f"\n"
        f"⏱ 持仓 {ai.get('hold_period', '?')} | "
        f"{risk_e} 风险 {ai.get('risk_level', '?')} | "
        f"{conf_e} 信心 {ai.get('confidence', '?')}\n"
        f"\n"
        f"🎯 入场 ${ai.get('entry_low', 0):.2f} ~ ${ai.get('entry_high', 0):.2f}\n"
        f"📍 目标1 *${ai.get('target_1', 0)}* | 目标2 *${ai.get('target_2', 0)}*\n"
        f"🛑 止损 *${ai.get('stop_loss', 0)}*\n"
        f"\n"
        f"📊 技术 买{tech['buy_score']}/10 | 卖{tech['sell_score']}/10\n"
        f"   RSI={tech['rsi']} · MACD={tech['macd']['hist']:+.3f} · BB%B={tech['boll']['pct_b']}\n"
        f"   VWAP=${tech['vwap']} · Vol {tech['vol']['ratio']}x · 动能{tech['mom']:+.2f}%\n"
        f"   支撑${tech['sr']['support']} · 压力${tech['sr']['resistance']} · ATR={tech['atr']}\n"
        f"   ⚡ {' · '.join(tech['alerts']) if tech['alerts'] else '无警报'}\n"
        f"\n"
        f"📰 {ai.get('news_impact', '-')}\n"
        f"🔮 {ai.get('catalyst', '-')}\n"
        f"💡 {ai.get('summary', '-')}\n"
        f"🤖 {ai.get('_model', 'unknown')}"
    )


def _build_premarket_summary(results: list) -> str:
    scores     = [int(r["ai"].get("score", 5)) for r in results]
    strong_buy = sum(1 for s in scores if s >= 8)
    buy        = sum(1 for s in scores if 6 <= s < 8)
    hold       = sum(1 for s in scores if 4 < s < 6)
    sell       = sum(1 for s in scores if s <= 4)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "*盘前汇总*",
        f"🚀 强烈买 {strong_buy} | 🟢 买 {buy} | 🟡 观望 {hold} | 🔴 卖/强卖 {sell}",
    ]
    top = [r for r in results if int(r["ai"].get("score", 5)) >= 7]
    if top:
        lines.append("⭐ *重点: * " + "  ".join(
            f"{r['tech']['ticker']}({r['ai']['score']})" for r in top
        ))
    fallback = [r["tech"]["ticker"] for r in results if r["ai"].get("_model") == "fallback"]
    if fallback:
        lines.append(f"⚠️ AI 不可用（纯技术）: {', '.join(fallback)}")
    lines += ["", "仅供参考，不构成投资建议，请严格执行止损。"]
    return "\n".join(lines)


# ─── 盘中快讯（压缩格式，无 AI）──────────────────────────────────────────────

def _build_intraday_line(sym: str, price: float, chg1: float,
                         buy: int, sell: int, rsi: float,
                         macd: dict, vol: dict, alerts: list) -> str:
    action = _action_from_scores(buy, sell)
    ae     = ACTION_EMOJI.get(action, "🟡")
    arrow  = "📈" if chg1 >= 0 else "📉"

    macd_sym = "↑" if macd["cross_up"] else ("↓" if macd["cross_down"] else ("▲" if macd["above"] else "▼"))
    vol_str  = f" Vol{vol['ratio']}x" if vol["surge"] else ""
    alert_str = " · ".join(alerts[:2])  # 最多 2 个警报，保持紧凑
    extra    = f"  ⚡{alert_str}" if alert_str else ""

    return (
        f"{ae} *{sym}* ${price} {arrow}{chg1:+.1f}%  "
        f"买{buy}/卖{sell}  RSI={rsi} MACD{macd_sym}{vol_str}{extra}"
    )


def _build_intraday_msg(lines: list[str], ts: str) -> str:
    header = f"📡 *盘中快讯* | {ts}\n5-min K线 · 纯技术 · 无 AI"
    return header + "\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)


# ─── FETCH HELPERS ────────────────────────────────────────────────────────────

def _fetch_tech(c, sym: str, ktype: KLType, bars: int,
                df_daily_cache: dict) -> Optional[dict]:
    """Fetch klines and compute all technical indicators. Returns tech dict or None."""
    try:
        df = c.get_kline(sym, bars=bars, ktype=ktype)
    except Exception as e:
        log.warning("signal_reporter: kline %s %s failed: %s", sym, ktype, e)
        return None

    if df is None or len(df) < 40:
        return None

    # Daily kline for S/R (fetch once and cache per run)
    if sym not in df_daily_cache:
        try:
            df_daily_cache[sym] = c.get_kline(sym, bars=30, ktype=KLType.K_DAY)
        except Exception:
            df_daily_cache[sym] = None
    df_d = df_daily_cache[sym]

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]
    price = round(float(close.iloc[-1]), 2)

    chg1 = round((price / float(df_d["close"].iloc[-2]) - 1) * 100, 2) \
           if (df_d is not None and len(df_d) >= 2) else 0.0
    chg5 = round((price / float(df_d["close"].iloc[-6]) - 1) * 100, 2) \
           if (df_d is not None and len(df_d) >= 6) else 0.0

    rsi_v   = _rsi(close)
    macd_v  = _macd(close)
    ema9    = _ema(close, 9)
    ema21   = _ema(close, 21)
    boll_v  = _bollinger(close)
    stoch_v = _stochastic(high, low, close)
    vwap_v  = _vwap_intraday(df)
    atr_v   = _atr(high, low, close)
    vol_v   = _volume_surge(vol)
    mom_v   = _momentum(close)
    sr_v    = _sr(df_d) if df_d is not None else {"support": 0, "resistance": 0}

    buy_s, sell_s, alerts = _score(price, rsi_v, macd_v, ema9, ema21,
                                   boll_v, vol_v, stoch_v, vwap_v, mom_v)

    return {
        "ticker":     sym,
        "company":    COMPANY_NAMES.get(sym, sym),
        "price":      price,
        "chg1":       chg1,
        "chg5":       chg5,
        "rsi":        rsi_v,
        "macd":       macd_v,
        "ema9":       ema9,
        "ema21":      ema21,
        "boll":       boll_v,
        "stoch":      stoch_v,
        "vwap":       vwap_v,
        "atr":        atr_v,
        "vol":        vol_v,
        "mom":        mom_v,
        "sr":         sr_v,
        "buy_score":  buy_s,
        "sell_score": sell_s,
        "alerts":     alerts,
    }


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def run_premarket() -> None:
    """盘前全面分析：08:30 ET 触发，15-min K线 + Gemini AI + 完整卡片。"""
    watchlist = load_watchlist()
    if not watchlist:
        log.info("signal_reporter: watchlist empty")
        return

    log.info("signal_reporter premarket: %s", watchlist)
    import pytz
    from datetime import datetime
    now = datetime.now(pytz.timezone("America/New_York"))
    ts  = now.strftime("%Y-%m-%d %H:%M ET")

    notifier.send(
        f"🌅 *盘前分析* | {ts}\n"
        f"15-min K线 · Gemini AI · 近3天新闻\n"
        f"共 {len(watchlist)} 支，按 AI 评分高→低"
    )

    macro_news = news_fetcher.fetch_macro_news()
    results    = []
    df_d_cache: dict = {}

    with _moomoo_client() as c:
        for sym in watchlist:
            tech = _fetch_tech(c, sym, KLType.K_15M, 120, df_d_cache)
            if not tech:
                log.warning("signal_reporter: skip %s (data insufficient)", sym)
                continue
            ticker_news = news_fetcher.fetch_ticker_news(sym)
            ai = _gemini_signal(tech, ticker_news, macro_news)
            results.append({"tech": tech, "ai": ai})
            time.sleep(0.3)

    if not results:
        notifier.send("⚠️ 盘前分析：所有股票数据获取失败，请检查 OpenD 连接")
        return

    results.sort(key=lambda r: -int(r["ai"].get("score", 5)))
    for r in results:
        notifier.send(_build_premarket_card(r["tech"], r["ai"]))
    notifier.send(_build_premarket_summary(results))
    log.info("signal_reporter premarket done — %d cards", len(results))


def run_intraday() -> None:
    """盘中快讯：5-min K线，纯技术，无 AI，全 watchlist 合并一条消息。"""
    watchlist = load_watchlist()
    if not watchlist:
        return

    import pytz
    from datetime import datetime
    now = datetime.now(pytz.timezone("America/New_York"))
    ts  = now.strftime("%H:%M ET")

    log.info("signal_reporter intraday: %s", ts)
    lines      = []
    df_d_cache: dict = {}

    with _moomoo_client() as c:
        for sym in watchlist:
            tech = _fetch_tech(c, sym, KLType.K_5M, 80, df_d_cache)
            if not tech:
                continue
            line = _build_intraday_line(
                sym, tech["price"], tech["chg1"],
                tech["buy_score"], tech["sell_score"],
                tech["rsi"], tech["macd"], tech["vol"], tech["alerts"],
            )
            lines.append(line)
            time.sleep(0.2)

    if not lines:
        log.warning("signal_reporter intraday: no data for any ticker")
        return

    notifier.send(_build_intraday_msg(lines, ts))
    log.info("signal_reporter intraday done — %d tickers", len(lines))


# ─── STANDALONE SCHEDULER ────────────────────────────────────────────────────

def _nyse_holidays(year: int) -> set:
    from datetime import date, timedelta

    def observed(d: date) -> date:
        if d.weekday() == 5: return d - timedelta(days=1)
        if d.weekday() == 6: return d + timedelta(days=1)
        return d

    def nth_weekday(y, m, wd, n):
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def last_monday(y, m):
        import calendar
        last = date(y, m, calendar.monthrange(y, m)[1])
        return last - timedelta(days=last.weekday())

    def easter(y):
        a = y % 19; b, c = divmod(y, 100); d, e = divmod(b, 4)
        f = (b + 8) // 25; g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
        ll = (32 + 2 * e + 2 * i - h - k) % 7
        m2 = (a + 11 * h + 22 * ll) // 451
        mo, dy = divmod(114 + h + ll - 7 * m2, 31)
        return date(y, mo, dy + 1)

    return {
        observed(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter(year) - __import__("datetime").timedelta(days=2),
        last_monday(year, 5),
        observed(date(year, 6, 19)),
        observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed(date(year, 12, 25)),
    }


def _ny_now():
    import pytz
    from datetime import datetime
    return datetime.now(pytz.timezone("America/New_York"))


def _is_trading_day() -> bool:
    now = _ny_now()
    if now.weekday() >= 5:
        return False
    return now.date() not in _nyse_holidays(now.year)


def run_loop() -> None:
    """Start the signal reporter as a standalone scheduler.

    Premarket full analysis:  08:30 ET (15-min + AI, once daily)
    Opening peak compact scan: 09:30 / 10:00 / 10:30 / 11:00 / 11:30 ET (5-min, no AI)
    """
    import pytz
    from apscheduler.schedulers.blocking import BlockingScheduler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.root / "logs" / "signal_reporter.log"),
        ],
    )

    NY   = pytz.timezone("America/New_York")
    sched = BlockingScheduler(timezone=NY)

    def _premarket():
        if not _is_trading_day():
            return
        try:
            run_premarket()
        except Exception as e:
            log.exception("premarket job failed: %s", e)

    def _intraday():
        if not _is_trading_day():
            return
        now = _ny_now()
        minutes = now.hour * 60 + now.minute
        if not (9 * 60 + 30 <= minutes <= 16 * 60):
            return
        try:
            run_intraday()
        except Exception as e:
            log.exception("intraday job failed: %s", e)

    sched.add_job(_premarket, "cron",
                  day_of_week="mon-fri", hour=8, minute=30,
                  coalesce=True, misfire_grace_time=300, max_instances=1)

    for _h, _m in [(9, 30), (10, 0), (10, 30), (11, 0), (11, 30)]:
        sched.add_job(_intraday, "cron",
                      day_of_week="mon-fri", hour=_h, minute=_m,
                      coalesce=True, misfire_grace_time=120, max_instances=1)

    wl = load_watchlist()
    log.info("signal_reporter started | watchlist: %s", wl)
    log.info("schedule: premarket@08:30, intraday@09:30/10:00/10:30/11:00/11:30 ET")
    notifier.send(
        f"📡 *Signal Reporter 已启动*\n"
        f"watchlist: {', '.join(wl)}\n"
        f"盘前分析@08:30 · 盘中快讯@09:30-11:30 ET"
    )
    sched.start()


# ─── CLI ENTRY ────────────────────────────────────────────────────────────────

def main() -> None:
    """
    python -m src.signal_reporter run             # 启动独立 scheduler
    python -m src.signal_reporter list            # 查看 watchlist
    python -m src.signal_reporter add TICKER      # 加入 watchlist
    python -m src.signal_reporter remove TICKER   # 移除
    python -m src.signal_reporter premarket       # 手动触发盘前分析
    python -m src.signal_reporter intraday        # 手动触发盘中快讯
    """
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "run":
        run_loop()
    elif cmd == "list":
        print(list_tickers())
    elif cmd == "add" and len(sys.argv) > 2:
        print(add_ticker(sys.argv[2]))
    elif cmd == "remove" and len(sys.argv) > 2:
        print(remove_ticker(sys.argv[2]))
    elif cmd == "premarket":
        run_premarket()
    elif cmd == "intraday":
        run_intraday()
    else:
        print(main.__doc__)


if __name__ == "__main__":
    main()
