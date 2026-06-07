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

from .config import settings, gemini_cascade
from . import finbert_sentiment, news_fetcher, notifier
from .moomoo_client import client as _moomoo_client


# ─── Context helpers (macro + sector + insider + pre-market) ──────────────────
# Wiring these into the premarket card lets the trader see at a glance:
#   • Is the broader market risk-on or risk-off (VIX + SPY)?
#   • Is this name leading or lagging its sector?
#   • Is earnings imminent?
#   • Liquidity OK?
#   • Pre-market price action?
#   • Recent insider activity?

# sector → ETF lookup — now shared with ml.features; import the single source
# of truth from src.sector so the signal card and the ML feature use identical
# ETF picks.
from .sector import SECTOR_TO_ETF as _SECTOR_TO_ETF  # noqa: E402


def _vix_label(vix: float) -> str:
    """Color-coded VIX bucket label. Matches the live risk_manager bands:
    25 = halve sizes, 35 = quarter sizes."""
    if vix > 35:
        return "🔴 crisis"
    if vix > 25:
        return "🟡 elevated"
    if vix > 20:
        return "🟡 cautious"
    return "🟢 benign"


def _is_premarket_now() -> bool:
    """True when current NY time is in 04:00-09:30 ET (US pre-market window).

    Why this matters: MooMoo's snapshot `last_price` does NOT refresh during
    pre-market for most subscription tiers — it still holds yesterday's regular
    session close until 9:30 ET. If we display `last_price` as "current price"
    at 08:30 ET, the card shows yesterday's close + yesterday's daily change,
    which doesn't match what the user observes in actual pre-market trading.
    Solution: in this window, prefer `pre_price` / compute chg vs prev_close.
    """
    import pytz
    from datetime import datetime
    now = datetime.now(pytz.timezone("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 4 * 60 <= minutes < 9 * 60 + 30


def _live_snap(c, sym: str) -> dict | None:
    """Return {price, prev, chg_pct, src} where src ∈ {'pre','last'}.

    In pre-market hours: use snapshot.pre_price (today's last pre-market trade)
    and compute chg vs prev_close_price. Otherwise: use snapshot.last_price.
    Returns None when prev_close is missing or chosen price is unusable.
    """
    try:
        snap = c.get_snapshot(sym)
        prev = float(snap.get("prev_close_price") or 0)
        if prev <= 0:
            return None
        if _is_premarket_now():
            pre = float(snap.get("pre_price") or 0)
            if pre > 0:
                return {"price": pre, "prev": prev,
                        "chg_pct": round((pre / prev - 1) * 100, 2),
                        "src": "pre"}
        last = float(snap.get("last_price") or 0)
        if last <= 0:
            return None
        return {"price": last, "prev": prev,
                "chg_pct": round((last / prev - 1) * 100, 2),
                "src": "last"}
    except Exception as e:
        log.debug("snapshot %s failed: %s", sym, e)
        return None


def _snap_chg(c, sym: str) -> dict | None:
    """Return a snapshot dict {last, prev, chg_pct} for `sym` or None.

    `last` is the *user-visible* price — pre_price during pre-market window,
    last_price otherwise. Key name kept as 'last' for legacy callers.
    """
    s = _live_snap(c, sym)
    if s is None:
        return None
    return {"last": s["price"], "prev": s["prev"], "chg_pct": s["chg_pct"]}


def _sector_snap(c, ticker: str) -> dict | None:
    """Look up ticker's sector and return that sector ETF's snapshot.

    None when ticker has no sector mapping or ETF data unavailable.
    """
    from .sector import get_sector
    sector = get_sector(ticker)
    etf = _SECTOR_TO_ETF.get(sector)
    if not etf:
        return None
    snap = _snap_chg(c, etf)
    if snap is None:
        return None
    snap.update({"etf": etf, "sector": sector})
    return snap


def _earnings_info(symbol: str) -> dict | None:
    """Days-until-next-earnings + ISO date. Uses the cache in earnings.py."""
    from datetime import date
    from .earnings import get_next_earnings
    next_date = get_next_earnings(symbol)
    if not next_date:
        return None
    try:
        d = date.fromisoformat(next_date)
    except ValueError:
        return None
    return {"date": next_date, "days_until": (d - date.today()).days}


def _premarket(symbol: str) -> dict | None:
    """yfinance pre-market quote, available 04:00–09:30 ET only.

    yfinance keys: preMarketPrice / preMarketChangePercent. Returns None when
    outside the pre-market window or when the field isn't populated.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        pre_price = info.get("preMarketPrice")
        pre_change = info.get("preMarketChangePercent")
        if pre_price and pre_change is not None:
            return {"price": float(pre_price),
                    "change_pct": float(pre_change)}
    except Exception as e:
        log.debug("pre-market fetch %s failed: %s", symbol, e)
    return None


def _insider_30d(symbol: str) -> dict | None:
    """Last-30-day insider activity from yfinance.

    Returns {buys, sells, buy_value, sell_value, net_value}.
    None if no data (ETFs, mutual funds, missing).
    """
    try:
        import yfinance as yf
        from datetime import date, timedelta
        trans = yf.Ticker(symbol).insider_transactions
        if trans is None or trans.empty:
            return None
        cutoff = pd.to_datetime(date.today() - timedelta(days=30))
        # yfinance's "Start Date" can be datetime or string — be tolerant.
        trans = trans.copy()
        trans["Start Date"] = pd.to_datetime(trans["Start Date"], errors="coerce")
        recent = trans[trans["Start Date"] >= cutoff]
        if recent.empty:
            return {"buys": 0, "sells": 0, "buy_value": 0.0,
                    "sell_value": 0.0, "net_value": 0.0}
        buys_mask = recent["Transaction"].astype(str).str.contains(
            "Purchase", case=False, na=False)
        sells_mask = recent["Transaction"].astype(str).str.contains(
            "Sale", case=False, na=False)
        buy_val = float(recent.loc[buys_mask, "Value"].sum() or 0)
        sell_val = float(recent.loc[sells_mask, "Value"].sum() or 0)
        return {
            "buys": int(buys_mask.sum()),
            "sells": int(sells_mask.sum()),
            "buy_value": buy_val,
            "sell_value": sell_val,
            "net_value": buy_val - sell_val,
        }
    except Exception as e:
        log.debug("insider fetch %s failed: %s", symbol, e)
        return None


def _build_context_block(ticker_chg_pct: float, vix: float | None,
                         spy: dict | None, sector: dict | None,
                         earnings: dict | None, premkt: dict | None,
                         insider: dict | None, spread_pct: float | None
                         ) -> str:
    """Assemble the 'Macro & Context' multi-line block. Lines with no data
    are omitted so the card stays compact for low-info names."""
    lines: list[str] = []

    # Line 1 — VIX state + SPY chg + ticker vs SPY relative strength.
    vix_part = f"VIX {vix:.1f} {_vix_label(vix)}" if vix is not None else "VIX n/a"
    if spy is not None:
        rs = ticker_chg_pct - spy["chg_pct"]
        rs_tag = "强势" if rs > 0.5 else ("弱势" if rs < -0.5 else "持平")
        spy_part = f"SPY {spy['chg_pct']:+.2f}%"
        rs_part = f"vs SPY {rs:+.2f}% {rs_tag}"
        lines.append(f"📊 {vix_part} | {spy_part} | {rs_part}")
    else:
        lines.append(f"📊 {vix_part}")

    # Line 2 — Earnings countdown (always show if data exists).
    if earnings is not None:
        d = earnings["days_until"]
        if 0 <= d <= 5:
            emoji = "⚠️"   # tight window — caller may want to skip the trade
        elif d < 0:
            emoji = "🕓"   # past — should be refreshed by cache TTL
        else:
            emoji = "📅"
        lines.append(f"{emoji} Earnings: {d}d ({earnings['date']})")

    # Line 3 — Sector ETF strength + relative.
    if sector is not None:
        diff = ticker_chg_pct - sector["chg_pct"]
        if diff > 0.3:
            tag = "强势"
        elif diff < -0.3:
            tag = "弱势"
        else:
            tag = "同步"
        lines.append(
            f"🏭 Sector {sector['etf']}({sector['sector']}) "
            f"{sector['chg_pct']:+.2f}% — vs sector {diff:+.2f}% {tag}"
        )

    # Line 4 — Liquidity + pre-market action.
    parts: list[str] = []
    if spread_pct is not None and spread_pct > 0:
        if spread_pct < 0.05:
            qual = "优"
        elif spread_pct < 0.15:
            qual = "良"
        else:
            qual = "⚠ 差"
        parts.append(f"spread {spread_pct:.2f}% {qual}")
    if premkt is not None:
        arrow = "📈" if premkt["change_pct"] >= 0 else "📉"
        parts.append(f"Pre-mkt ${premkt['price']:.2f} {arrow}{premkt['change_pct']:+.2f}%")
    if parts:
        lines.append("💧 " + " | ".join(parts))

    # Line 5 — Insider 30d (only show if any activity).
    if insider is not None and (insider["buys"] + insider["sells"] > 0):
        if insider["net_value"] > 0:
            emoji, tag = "🟢", "净买入"
        elif insider["net_value"] < 0:
            emoji, tag = "🔴", "净卖出"
        else:
            emoji, tag = "⚪️", "持平"
        net_m = insider["net_value"] / 1e6
        lines.append(
            f"👔 Insider 30d: {emoji} {insider['buys']} buys / "
            f"{insider['sells']} sells — {tag} ${net_m:+.1f}M"
        )

    return "\n".join(lines)

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
    """Stochastic %K/%D with relaxed cross thresholds.

    Original logic only fired cross_up when k < 35 / cross_down when k > 65.
    Audit (2026-05-28): in trending markets, valid stoch crosses commonly occur
    above 50 — restricting to <35 / >65 silently skipped half the signals.
    Loosened to <50 / >50 so we catch in-trend rotations too.
    """
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
        "cross_up":   bool(kp < dp and k_n > d_n and k_n < 50),
        "cross_down": bool(kp > dp and k_n < d_n and k_n > 50),
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
    # 2026-05-28: relaxed pct_b thresholds from 0.08/0.92 → 0.15/0.85.
    # Original 0.08 needed price almost touching the band — fired so rarely
    # that the score signal was effectively binary. 0.15/0.85 captures the
    # "near band" zone where mean-reversion is statistically meaningful.
    if boll["breakout"] == "up":    buy += 2;  alerts.append("BB突破上轨")
    elif boll["breakout"] == "down": sell += 2; alerts.append("BB跌破下轨")
    elif boll["pct_b"] < 0.15:  buy  += 1
    elif boll["pct_b"] > 0.85:  sell += 1

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

    for model_name in gemini_cascade():
        for key in keys:
            for attempt in range(2):
                try:
                    client = genai.Client(api_key=key)
                    # Low temperature → stable, repeatable decisions (less run-to-run
                    # swing in score/action). Was default (~1.0) which made the same
                    # setup swing e.g. 5/10 ↔ 8/10 between calls.
                    resp = client.models.generate_content(
                        model=model_name, contents=prompt,
                        config={"temperature": 0.15})
                    raw  = re.sub(r"```json|```", "", resp.text.strip()).strip()
                    result = json.loads(raw)
                    result["_model"] = model_name
                    return result
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        # Quota exhausted for this key+model → try next key.
                        break
                    if attempt == 0 and any(x in err for x in ("503", "500", "UNAVAILABLE")):
                        time.sleep(4)
                        continue
                    break
        # All keys failed / quota'd for this model → outer loop tries next (lighter) model.
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

def _ai_num(ai: dict, key: str, default: float = 0.0) -> float:
    """Get a numeric field from AI response, treating None/missing as default."""
    v = ai.get(key)
    return float(v) if v is not None else default


_FINBERT_LABEL_EMOJI = {"positive": "🟢", "neutral": "⚪️", "negative": "🔴"}


def _finbert_line(sentiment: Optional[dict]) -> str:
    """One-liner for the premarket card. Empty string if FinBERT unavailable."""
    if not sentiment or sentiment.get("n_headlines", 0) == 0:
        return ""
    emoji = _FINBERT_LABEL_EMOJI.get(sentiment["label"], "⚪️")
    n = sentiment["n_headlines"]
    return (
        f"🧪 FinBERT {emoji} *{sentiment['label']}* "
        f"net={sentiment['score_net']:+.2f}  "
        f"(P{sentiment['n_pos']}/N{sentiment['n_neu']}/M{sentiment['n_neg']} of {n})\n"
    )


def _build_premarket_card(tech: dict, ai: dict,
                          sentiment: Optional[dict] = None,
                          context_block: str = "") -> str:
    score  = int(ai.get("score") or 5)
    act_e  = ACTION_EMOJI.get(ai.get("action", "观望"), "🟡")
    risk_e = RISK_EMOJI.get(ai.get("risk_level", "中"), "🟡")
    conf_e = CONF_EMOJI.get(ai.get("confidence", "中"), "✋")
    chg1   = tech.get("chg1") or 0.0
    arrow  = "📈" if chg1 >= 0 else "📉"
    # Tag the price source so the user knows whether this is regular-session
    # last or today's pre-market last. Critical at 08:30 ET where MooMoo's
    # last_price is stale and we fall back to pre_price.
    src    = tech.get("price_src", "last")
    src_tag = " 🕓盘前" if src == "pre" else ""
    finbert_line = _finbert_line(sentiment)
    # Context block is optional — empty string when nothing was fetchable.
    context_section = f"\n{context_block}\n" if context_block else ""

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*{tech['ticker']}* {tech['company']} | "
        f"${tech['price']} {arrow} {chg1:+.2f}%{src_tag}\n"
        f"\n"
        f"{act_e} *{ai.get('action', '?')}*  {_score_bar(score)}\n"
        f"   {ai.get('reason', '')}\n"
        f"{context_section}"
        f"\n"
        f"⏱ 持仓 {ai.get('hold_period', '?')} | "
        f"{risk_e} 风险 {ai.get('risk_level', '?')} | "
        f"{conf_e} 信心 {ai.get('confidence', '?')}\n"
        f"\n"
        f"🎯 入场 ${_ai_num(ai, 'entry_low'):.2f} ~ ${_ai_num(ai, 'entry_high'):.2f}\n"
        f"📍 目标1 *${_ai_num(ai, 'target_1'):.2f}* | 目标2 *${_ai_num(ai, 'target_2'):.2f}*\n"
        f"🛑 止损 *${_ai_num(ai, 'stop_loss'):.2f}*\n"
        f"\n"
        f"📊 技术 买{tech['buy_score']}/10 | 卖{tech['sell_score']}/10\n"
        f"   RSI={tech['rsi']} · MACD={tech['macd']['hist']:+.3f} · BB%B={tech['boll']['pct_b']}\n"
        f"   VWAP=${tech['vwap']} · Vol {tech['vol']['ratio']}x · 动能{tech.get('mom', 0):+.2f}%\n"
        f"   支撑${tech['sr']['support']} · 压力${tech['sr']['resistance']} · ATR={tech['atr']}\n"
        f"   ⚡ {' · '.join(tech['alerts']) if tech['alerts'] else '无警报'}\n"
        f"\n"
        f"{finbert_line}"
        f"📰 {ai.get('news_impact') or '-'}\n"
        f"🔮 {ai.get('catalyst') or '-'}\n"
        f"💡 {ai.get('summary') or '-'}\n"
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

    # Use real-time snapshot for current price and daily change.
    # request_history_kline only returns completed bars, so close.iloc[-1] may be
    # 5–15 min stale during trading hours. Snapshot gives the live last price.
    # _live_snap auto-swaps to pre_price during 04:00-09:30 ET — MooMoo's
    # last_price field is stuck on yesterday's close in that window, which
    # used to make the premarket card show yesterday's price + yesterday's chg.
    price = round(float(close.iloc[-1]), 2)  # fallback
    chg1  = 0.0
    price_src = "kline"
    snap_info = _live_snap(c, sym)
    if snap_info is not None:
        price = round(snap_info["price"], 2)
        chg1 = snap_info["chg_pct"]
        price_src = snap_info["src"]
        if price_src == "pre":
            log.info("signal_reporter: %s using pre-market price $%.2f (%+.2f%%)",
                     sym, price, chg1)
    elif df_d is not None and len(df_d) >= 2:
        # snapshot unavailable → fallback to daily kline for chg
        prev_close = float(df_d["close"].iloc[-1])
        if prev_close > 0:
            chg1 = round((price / prev_close - 1) * 100, 2)

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
    # Intraday swing S/R from the bars themselves (last 20) — tight, scalp-relevant
    # levels, unlike the wide daily 30-bar range. Critical for ultra-short entries.
    isr_v   = _sr(df, lookback=20)

    buy_s, sell_s, alerts = _score(price, rsi_v, macd_v, ema9, ema21,
                                   boll_v, vol_v, stoch_v, vwap_v, mom_v)

    return {
        "ticker":     sym,
        "company":    COMPANY_NAMES.get(sym, sym),
        "price":      price,
        "price_src":  price_src,   # 'pre' | 'last' | 'kline' — surfaced in card header
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
        "isr":        isr_v,   # intraday swing S/R (tight) — used by the scalp card
        "buy_score":  buy_s,
        "sell_score": sell_s,
        "alerts":     alerts,
    }


# ─── 超短线 30-MIN SCALP 决策仪表盘（多周期 + AI）─────────────────────────────
# 灵感来自 daily_stock_analysis 的「决策仪表盘」，但为 *超短线*（持仓 15-60min）
# 重新设计：主周期 30M（结构/趋势）+ 触发周期 5M（精确入场），点位贴近现价。
# 价格全部来自 MooMoo 券商实时源（snapshot last_price + 已修复的 kline）。

def _trend_label(tech: dict) -> str:
    """一句话趋势：多头 / 空头 / 震荡 —— 看 EMA9/21 排列 + 价格位置 + MACD。"""
    ema9, ema21, price = tech["ema9"], tech["ema21"], tech["price"]
    above = price > tech["vwap"]
    if ema9 > ema21 and price > ema9:
        return "🟢多头" + ("·强" if (tech["macd"]["above"] and above) else "")
    if ema9 < ema21 and price < ema9:
        return "🔴空头" + ("·强" if (not tech["macd"]["above"] and not above) else "")
    return "🟡震荡"


def _call_deepseek_json(prompt: str) -> Optional[dict]:
    """通用 DeepSeek 调用，返回第一个 {...} JSON 对象 dict，失败返回 None。
    deepseek 是 reasoning 模型 → max_tokens 给足，否则 JSON 被推理内容挤断。"""
    if not settings.deepseek_key:
        return None
    import requests
    try:
        r = requests.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_key}",
                     "Content-Type": "application/json"},
            json={"model": settings.deepseek_model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.1, "stream": False, "max_tokens": 4000},
            timeout=60,
        )
        r.raise_for_status()
        content = (r.json()["choices"][0]["message"].get("content") or "").strip()
        s, e = content.find("{"), content.rfind("}")
        if s == -1 or e <= s:
            return None
        out = json.loads(content[s:e + 1])
        out["_model"] = settings.deepseek_model
        return out
    except Exception as ex:
        log.warning("scalp DeepSeek call failed: %s", ex)
        return None


def _scalp_ai(t30: dict, t5: dict, ticker_news: list, macro_news: list,
              ctx_text: str) -> dict:
    """超短线 AI 决策。Gemini → DeepSeek → 纯技术 fallback。返回富 dict。"""
    news_text = "\n".join(
        f"- {n['title'][:78]}" for n in ticker_news[:4]) or "无近期新闻"
    macro_text = "\n".join(f"- {n['title'][:64]}" for n in macro_news[:2]) or "无"
    atr5 = t5["atr"]

    prompt = f"""你是顶级美股超短线 scalper（持仓 15-60 分钟，分钟级进出）。基于多周期实时数据给出一份*超短线决策仪表盘*。

【{t30['ticker']} {t30['company']}】 实时价 ${t30['price']} | 当日 {t30['chg1']:+.2f}%

主周期 30分钟K线（结构/趋势）:
- RSI(7)={t30['rsi']} | MACD hist={t30['macd']['hist']:+.4f}(金叉={t30['macd']['cross_up']} 死叉={t30['macd']['cross_down']})
- EMA9={t30['ema9']:.2f}/EMA21={t30['ema21']:.2f} | VWAP=${t30['vwap']} | BB%B={t30['boll']['pct_b']}({t30['boll']['breakout']})
- Stoch K={t30['stoch']['k']}/D={t30['stoch']['d']} | ATR={t30['atr']} | 量比={t30['vol']['ratio']}x | 动能={t30['mom']:+.2f}%
- 技术评分 买{t30['buy_score']}/卖{t30['sell_score']} | 警报:{', '.join(t30['alerts']) or '无'}

触发周期 5分钟K线（精确入场）:
- RSI(7)={t5['rsi']} | MACD hist={t5['macd']['hist']:+.4f}(金叉={t5['macd']['cross_up']} 死叉={t5['macd']['cross_down']})
- EMA9={t5['ema9']:.2f}/EMA21={t5['ema21']:.2f} | VWAP=${t5['vwap']} | Stoch K={t5['stoch']['k']} | 量比={t5['vol']['ratio']}x | 动能={t5['mom']:+.2f}% | ATR(5M)={atr5}

日内结构(30M近20根): 支撑 ${t30['isr']['support']} | 压力 ${t30['isr']['resistance']}
日线大区间: 低 ${t30['sr']['support']} | 高 ${t30['sr']['resistance']}
市场背景:
{ctx_text or '无'}
个股新闻:
{news_text}
宏观:
{macro_text}

任务：超短线（分钟级）决策。点位必须*贴近现价*且紧凑——用 5分钟ATR({atr5})、VWAP、日内支撑/压力为锚。
要求 30M 与 5M 多周期共振才给高分；逆势只给观望/低分。财报<2天一律保守。

严格只返回 JSON（无代码块、无多余文字）:
{{"score":1-10整数,"action":"强烈买入/买入/观望/卖出/强烈卖出","verdict":"核心结论一句话(≤30字)",
"trend":"30M与5M多周期趋势是否共振(≤28字)","entry_low":数字,"entry_high":数字,
"target_1":数字,"target_2":数字,"stop_loss":数字,"rr":"盈亏比如1:2.1",
"risks":["风险(≤14字)"],"catalyst":"催化/新闻影响(≤22字)","checklist":["可执行步骤(≤20字)"],
"hold":"预计持仓如15-45min","confidence":"高/中/低"}}"""

    ai = _call_gemini(prompt) or _call_deepseek_json(prompt)
    if ai:
        log.info("scalp AI [%s] %s → %s (%s/10)", ai.get("_model", "?"),
                 t30["ticker"], ai.get("action"), ai.get("score"))
        # 防御：list 字段可能被模型返回成 str
        for k in ("risks", "checklist"):
            if isinstance(ai.get(k), str):
                ai[k] = [ai[k]]
            ai[k] = ai.get(k) or []
        return ai

    # 纯技术 fallback（AI 全挂）— 用 30M 评分 + 5M ATR 给紧凑点位
    net = t30["buy_score"] - t30["sell_score"]
    score = max(1, min(10, 5 + net // 2))
    p, a5 = t30["price"], atr5
    bullish = net >= 0
    return {
        "score": score, "action": _action_from_scores(t30["buy_score"], t30["sell_score"]),
        "verdict": "AI不可用，纯技术信号",
        "trend": f"30M {_trend_label(t30)} / 5M {_trend_label(t5)}",
        "entry_low": round(p - a5 * 0.4, 2), "entry_high": round(p + a5 * 0.4, 2),
        "target_1": round(p + a5 * (1.2 if bullish else -1.2), 2),
        "target_2": round(p + a5 * (2.0 if bullish else -2.0), 2),
        "stop_loss": round(p - a5 * (1.2 if bullish else -1.2), 2),
        "rr": "1:1.7", "risks": ["AI不可用"], "catalyst": "未知",
        "checklist": [f"现价{p}附近触发", f"破止损即离场"],
        "hold": "15-45min", "confidence": "低", "_model": "fallback",
    }


def _build_scalp_card(t30: dict, t5: dict, ai: dict, ctx_text: str,
                      sentiment: Optional[dict], ts: str) -> str:
    score = int(ai.get("score") or 5)
    act_e = ACTION_EMOJI.get(ai.get("action", "观望"), "🟡")
    conf_e = CONF_EMOJI.get(ai.get("confidence", "中"), "✋")
    chg1 = t30.get("chg1") or 0.0
    arrow = "📈" if chg1 >= 0 else "📉"
    src_tag = " 🕓盘前" if t30.get("price_src") == "pre" else ""
    reson = "✅共振" if _trend_label(t30)[:3] == _trend_label(t5)[:3] and "震荡" not in _trend_label(t30) else "⚠️背离/震荡"
    # rr 清洗：模型常把模板占位词("如2.1")混进来 → 只抽 "数字:数字"
    _rr = str(ai.get("rr", "") or "")
    _m = re.search(r"[\d.]+\s*[:：]\s*[\d.]+", _rr)
    rr = _m.group(0).replace("：", ":") if _m else (_rr or "?")
    risks = " · ".join(ai.get("risks", [])[:3]) or "无明显风险"
    checklist = "\n".join(f"   {i+1}. {s}" for i, s in enumerate(ai.get("checklist", [])[:4])) or "   —"
    finbert_line = _finbert_line(sentiment)
    ctx_section = f"\n{ctx_text}\n" if ctx_text else ""

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *超短线* {t30['ticker']} {t30['company']}\n"
        f"${t30['price']} {arrow}{chg1:+.2f}%{src_tag}  ·  {ts}\n"
        f"\n"
        f"{act_e} *{ai.get('action','?')}*  {_score_bar(score)}\n"
        f"🧭 {ai.get('verdict','')}\n"
        f"\n"
        f"📐 趋势 {reson}: {ai.get('trend','')}\n"
        f"   30M {_trend_label(t30)} · 5M {_trend_label(t5)}\n"
        f"\n"
        f"🎯 入场 ${_ai_num(ai,'entry_low'):.2f} ~ ${_ai_num(ai,'entry_high'):.2f}\n"
        f"📍 T1 *${_ai_num(ai,'target_1'):.2f}*  ·  T2 *${_ai_num(ai,'target_2'):.2f}*\n"
        f"🛑 止损 *${_ai_num(ai,'stop_loss'):.2f}*  ·  盈亏比 {rr}  ·  ⏱ {ai.get('hold','?')}\n"
        f"\n"
        f"🚨 风险: {risks}\n"
        f"🔮 催化: {ai.get('catalyst') or '-'}\n"
        f"✅ 操作清单:\n{checklist}\n"
        f"\n"
        f"📊 30M 买{t30['buy_score']}/卖{t30['sell_score']} · RSI{t30['rsi']} · MACD{t30['macd']['hist']:+.3f} · VWAP${t30['vwap']} · Vol{t30['vol']['ratio']}x\n"
        f"   5M  RSI{t5['rsi']} · MACD{t5['macd']['hist']:+.3f} · VWAP${t5['vwap']} · Vol{t5['vol']['ratio']}x · 动能{t5['mom']:+.2f}%\n"
        f"   日内 支撑${t30['isr']['support']} · 压力${t30['isr']['resistance']} · ATR(5M){t5['atr']}\n"
        f"   日线区间 ${t30['sr']['support']}~${t30['sr']['resistance']}\n"
        f"{ctx_section}"
        f"{finbert_line}"
        f"🤖 {ai.get('_model','?')}  ·  {conf_e}{ai.get('confidence','?')}信心\n"
        f"⚠️ 超短线高风险，仅供参考，严格止损。"
    )


def analyze_ultrashort(symbol: str, send: bool = False, min_net: int = 0) -> Optional[str]:
    """单只股票的超短线 30-min 决策卡。返回卡片文本（None=数据不足/信心不足）。
    send=True 时同时推送到 notifier。
    min_net>0 时做 *确定性技术信心* 预筛：|30M 买-卖| < min_net 直接跳过(不调 AI、
    不推送)。回测显示弱信号≈噪音，net≥5(强烈买/卖)才值得推 → 自动扫描传 min_net=5。"""
    symbol = symbol.strip().upper()
    import pytz
    from datetime import datetime
    ts = datetime.now(pytz.timezone("America/New_York")).strftime("%m-%d %H:%M ET")

    df_d_cache: dict = {}
    with _moomoo_client() as c:
        t30 = _fetch_tech(c, symbol, KLType.K_30M, 120, df_d_cache)
        t5 = _fetch_tech(c, symbol, KLType.K_5M, 120, df_d_cache)
        if not t30 or not t5:
            log.warning("scalp %s: data insufficient (30M=%s 5M=%s)",
                        symbol, bool(t30), bool(t5))
            return None
        # 强信号预筛 — 在花 AI 调用前用便宜的技术信心过滤掉弱/观望信号。
        net = t30["buy_score"] - t30["sell_score"]
        if min_net > 0 and abs(net) < min_net:
            log.info("scalp %s: 跳过（技术信心不足 net=%+d, 需 ≥%d）", symbol, net, min_net)
            return None
        # 市场背景（与盘前卡共用同一套 context helpers）
        try:
            vix_value = c.get_vix()
        except Exception:
            vix_value = 15.0
        spy_snap = _snap_chg(c, "SPY")
        sector_data = _sector_snap(c, symbol)
        earnings_data = _earnings_info(symbol)
        spread_pct = None
        try:
            spread_pct = c.get_spread_pct(symbol)
        except Exception:
            pass
        ctx_text = _build_context_block(
            ticker_chg_pct=t30.get("chg1", 0) or 0, vix=vix_value, spy=spy_snap,
            sector=sector_data, earnings=earnings_data, premkt=None,
            insider=None, spread_pct=spread_pct)

    ticker_news = news_fetcher.fetch_ticker_news(symbol)
    macro_news = news_fetcher.fetch_macro_news()
    sentiment = None
    if finbert_sentiment.is_available() and ticker_news:
        sentiment = finbert_sentiment.score_headlines(
            [n.get("title", "") for n in ticker_news[:8]])

    ai = _scalp_ai(t30, t5, ticker_news, macro_news, ctx_text)
    card = _build_scalp_card(t30, t5, ai, ctx_text, sentiment, ts)
    if send:
        notifier.send(card)
    return card


def run_scalp(symbols: Optional[list[str]] = None, min_net: int = 5) -> None:
    """超短线扫描：对 watchlist（或指定 symbols）逐只产决策卡并推送，按评分排序。
    默认 min_net=5 → 只推「强烈买入/强烈卖出」级别的强信号（回测显示弱信号≈噪音）。
    本轮无强信号时静默跳过，不推空消息（启动时已确认在运行）。"""
    wl = symbols or load_watchlist()
    if not wl:
        log.info("scalp: watchlist empty")
        return
    cards = []
    for sym in wl:
        try:
            card = analyze_ultrashort(sym, send=False, min_net=min_net)
            if card:
                # 提取评分用于排序（卡片里嵌的 _score_bar 含 *N/10*）
                m = re.search(r"\*(\d+)/10\*", card)
                cards.append((int(m.group(1)) if m else 5, card))
        except Exception as e:
            log.exception("scalp %s failed: %s", sym, e)
        time.sleep(0.3)
    if not cards:
        log.info("scalp: 本轮无强信号（%d 只全部技术信心 <%d 或数据不足）— 静默跳过",
                 len(wl), min_net)
        return
    cards.sort(key=lambda x: -x[0])
    for _, card in cards:
        notifier.send(card)
    log.info("scalp done — %d 张强信号卡", len(cards))


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

    finbert_on = finbert_sentiment.is_available()
    if finbert_on:
        log.info("signal_reporter premarket: FinBERT enabled — will score news headlines")

    with _moomoo_client() as c:
        # Fetch macro context ONCE per premarket — SPY snapshot + VIX. These
        # are shared across every ticker card so each one shows the same
        # market backdrop.
        spy_snap = _snap_chg(c, "SPY")
        try:
            vix_value = c.get_vix()
            log.info("signal_reporter premarket: VIX=%.1f, SPY %s",
                     vix_value, f"{spy_snap['chg_pct']:+.2f}%" if spy_snap else "n/a")
        except Exception as e:
            log.warning("VIX fetch failed: %s — defaulting to 15.0", e)
            vix_value = 15.0

        for sym in watchlist:
            tech = _fetch_tech(c, sym, KLType.K_15M, 120, df_d_cache)
            if not tech:
                log.warning("signal_reporter: skip %s (data insufficient)", sym)
                continue
            ticker_news = news_fetcher.fetch_ticker_news(sym)
            # FinBERT — score the same headlines Gemini sees. Optional layer:
            # if the import isn't available we silently skip (UI just omits the
            # line). FinBERT is offline-only after first model download.
            sentiment = None
            if finbert_on and ticker_news:
                headlines = [n.get("title", "") for n in ticker_news[:8]]
                sentiment = finbert_sentiment.score_headlines(headlines)
            # Macro & per-ticker context. yfinance fetches (pre-market, insider)
            # add ~1-2s/ticker; acceptable since premarket runs once/day.
            sector_data = _sector_snap(c, sym)
            earnings_data = _earnings_info(sym)
            premkt_data = _premarket(sym)
            insider_data = _insider_30d(sym)
            spread_pct = None
            try:
                spread_pct = c.get_spread_pct(sym)
            except Exception:
                pass
            context_block = _build_context_block(
                ticker_chg_pct=tech.get("chg1", 0) or 0,
                vix=vix_value,
                spy=spy_snap,
                sector=sector_data,
                earnings=earnings_data,
                premkt=premkt_data,
                insider=insider_data,
                spread_pct=spread_pct,
            )
            ai = _gemini_signal(tech, ticker_news, macro_news)
            results.append({
                "tech": tech, "ai": ai, "sentiment": sentiment,
                "context_block": context_block,
            })
            time.sleep(0.3)

    if not results:
        notifier.send("⚠️ 盘前分析：所有股票数据获取失败，请检查 OpenD 连接")
        return

    results.sort(key=lambda r: -int(r["ai"].get("score", 5)))
    for r in results:
        notifier.send(_build_premarket_card(
            r["tech"], r["ai"], r.get("sentiment"),
            context_block=r.get("context_block", "")))
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
    Ultra-short scalp scan:    every 30 min 09:30–16:00 ET (30M+5M multi-TF + AI)
                               — replaced the old 5-min no-AI intraday quick-scan.
    """
    import pytz
    from apscheduler.schedulers.blocking import BlockingScheduler

    # Only FileHandler here — the GUI redirects stdout to signal_reporter.log when
    # launching via subprocess.Popen(stdout=open(SIGNAL_LOG_FILE)), which would
    # cause every line to appear twice if we also had a StreamHandler.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
        handlers=[
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

    def _scalp():
        if not _is_trading_day():
            return
        now = _ny_now()
        minutes = now.hour * 60 + now.minute
        # only inside the regular session 09:30–16:00 ET (filters the 09:00 / 16:30
        # cron edges); AI multi-TF scalp dashboard for the whole watchlist.
        if not (9 * 60 + 30 <= minutes <= 16 * 60):
            return
        try:
            run_scalp()
        except Exception as e:
            log.exception("scalp job failed: %s", e)

    sched.add_job(_premarket, "cron",
                  day_of_week="mon-fri", hour=8, minute=30,
                  coalesce=True, misfire_grace_time=300, max_instances=1)

    # Every 30 min across the whole session. cron fires 09:00…16:30; the _scalp
    # guard drops the 09:00 and 16:30 edges → runs 09:30,10:00,…,16:00 (14×/day).
    sched.add_job(_scalp, "cron",
                  day_of_week="mon-fri", hour="9-16", minute="0,30",
                  coalesce=True, misfire_grace_time=120, max_instances=1)

    wl = load_watchlist()
    log.info("signal_reporter started | watchlist: %s", wl)
    log.info("schedule: premarket@08:30, scalp every 30min 09:30-16:00 ET")
    notifier.send(
        f"📡 *Signal Reporter 已启动*\n"
        f"watchlist: {', '.join(wl)}\n"
        f"盘前分析@08:30 · ⚡超短线@每30min(09:30-16:00 ET)"
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

    # For direct one-shot calls (premarket/intraday) the scheduler's basicConfig
    # is never called, so we set up logging here so warnings have timestamps.
    if cmd in ("premarket", "intraday", "scalp"):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s | %(message)s",
            handlers=[logging.StreamHandler()],
        )

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
    elif cmd == "scalp":
        # python -m src.signal_reporter scalp MU   → 单只测试（打印，不推送）
        # python -m src.signal_reporter scalp       → 全 watchlist 扫描并推送
        if len(sys.argv) > 2:
            card = analyze_ultrashort(sys.argv[2], send=False)
            print(card or "数据不足，无法生成卡片")
        else:
            run_scalp()
    else:
        print(main.__doc__)


if __name__ == "__main__":
    main()
