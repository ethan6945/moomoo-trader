"""
超短线信号回测 — 验证 30-min scalp 信号在你指定时点的方向准确度
=================================================================

回测的是 signal_reporter 里 *确定性技术骨架*（多周期评分 → 方向 + ATR点位），
用的是和实盘信号 **完全相同** 的指标函数（直接 import signal_reporter 的 _rsi/
_macd/_score…）。AI 那层（新闻/情绪/Gemini 措辞）无法历史重放（需要历史新闻 +
每个时点一次 LLM 调用），所以这里不含 AI——但实盘里 AI 绝大多数时候跟随技术方向
（如 MU 技术 买1/卖6 → AI 也判卖），故技术方向的准确度是信号边际的好代理。

诚实边界:
  • 价格 = MooMoo 券商历史 K 线（已修复细周期取数 bug，准确）。
  • 盘中 6 个时点完全可回测（盘中 5M/30M 数据齐全）。
  • 盘前信号: 5M K 线无盘前数据（最早 09:35），所以盘前读数 = 用「上一交易日收盘
    技术面」预测当日开盘方向。同一小时内 2 次盘前信号数据相同 → 实际只算 1 个读数。

口径:
  • 方向准确度 = 非「观望」信号中，未来 N 分钟收益方向与预测一致的比例。
  • 命中率(target-before-stop) = 以 ATR(5M) 为目标/止损，先到目标记胜、先到止损记负;
    单根 5M 内同时触及目标和止损 → 保守记「负」。
  • R = 实现收益 / ATR(5M)。

用法:
  python -m src.scalp_backtest MU            # MU, 3 个月
  python -m src.scalp_backtest MU 4          # MU, 4 个月
  python -m src.scalp_backtest MU,SNDK 3     # 多只
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd
from moomoo import KLType, RET_OK

from . import signal_reporter as sr
from .moomoo_client import _BARS_PER_TRADING_DAY, client as _moomoo_client

log = logging.getLogger(__name__)

# 盘中 6 个信号时点（开盘后 ~3 小时，每 30min）。09:30 用首根 5M(09:35)+上一日 30M。
INTRADAY_SLOTS = [dtime(10, 0), dtime(10, 30), dtime(11, 0),
                  dtime(11, 30), dtime(12, 0), dtime(12, 30)]
PREMARKET_SLOTS = [dtime(8, 30), dtime(9, 0)]   # 数据相同 → 视作 1 个盘前读数

TP_MULT = 1.5    # 目标 = 1.5 × ATR(5M)
SL_MULT = 1.0    # 止损 = 1.0 × ATR(5M)
HOLD_MIN = 30    # 持仓窗口（分钟）—— 超短线「30分钟」


def _fetch_history(q, code: str, ktype: KLType, start: date, end: date) -> pd.DataFrame | None:
    """分块抓取，绕过单次 ~1000 行上限。返回按 time_key 索引的 df。"""
    bpd = _BARS_PER_TRADING_DAY.get(ktype, 1)
    chunk_days = max(2, int(900 / bpd))    # 每个窗口 < ~900 根
    frames, cur = [], start
    while cur <= end:
        win_end = min(cur + timedelta(days=chunk_days), end)
        try:
            ret, df, _ = q.request_history_kline(
                code, start=cur.isoformat(), end=win_end.isoformat(),
                ktype=ktype, max_count=1000, autype="qfq")
            if ret == RET_OK and len(df):
                frames.append(df[["time_key", "open", "high", "low", "close", "volume"]])
        except Exception as e:
            log.warning("history chunk %s %s..%s failed: %s", code, cur, win_end, e)
        cur = win_end + timedelta(days=1)
        time.sleep(0.6)   # 友好对待 60/30s 限流
    if not frames:
        return None
    out = pd.concat(frames).drop_duplicates("time_key").copy()
    out["time_key"] = pd.to_datetime(out["time_key"])
    return out.set_index("time_key").sort_index()


def _signal_at(d30: pd.DataFrame, d5: pd.DataFrame) -> dict | None:
    """用截止某时点(已切片, 无未来数据)的 30M+5M 重建技术信号。"""
    if len(d30) < 40 or len(d5) < 20:
        return None
    price = float(d5["close"].iloc[-1])
    c, h, l, v = d30["close"], d30["high"], d30["low"], d30["volume"]
    buy, sell, _ = sr._score(
        price, sr._rsi(c), sr._macd(c), sr._ema(c, 9), sr._ema(c, 21),
        sr._bollinger(c), sr._volume_surge(v), sr._stochastic(h, l, c),
        sr._vwap_intraday(d30), sr._momentum(c))
    action = sr._action_from_scores(buy, sell)
    direction = 1 if "买" in action else (-1 if "卖" in action else 0)
    return {"price": price, "action": action, "dir": direction,
            "buy": buy, "sell": sell, "atr5": sr._atr(d5["high"], d5["low"], d5["close"])}


def _evaluate(d5: pd.DataFrame, entry_ts: datetime, price: float, direction: int,
              atr5: float, hold_min: int, tp_mult: float = TP_MULT,
              sl_mult: float = SL_MULT) -> dict | None:
    """前瞻评估: 未来 hold_min 内 target-before-stop + 方向收益。无前瞻偏差。
    r 单位 = ATR(5M): win=+tp_mult, loss=-sl_mult, timeout=方向化收益(ATR)。"""
    if direction == 0 or atr5 <= 0:
        return None
    long = direction > 0
    target = price + (tp_mult * atr5 if long else -tp_mult * atr5)
    stop = price - (sl_mult * atr5 if long else -sl_mult * atr5)
    end_ts = entry_ts + timedelta(minutes=hold_min)
    fwd = d5[(d5.index > entry_ts) & (d5.index <= end_ts)]
    if not len(fwd):
        return None
    outcome, r = "timeout", None
    for _, row in fwd.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        hit_stop = (lo <= stop) if long else (hi >= stop)
        hit_tgt = (hi >= target) if long else (lo <= target)
        if hit_stop:                       # 同根同时触及 → 保守记负
            outcome, r = "loss", -sl_mult
            break
        if hit_tgt:
            outcome, r = "win", tp_mult
            break
    last = float(fwd["close"].iloc[-1])
    fwd_ret_r = (last - price) / atr5 * (1 if long else -1)   # 方向化收益(ATR)
    if outcome == "timeout":
        r = round(fwd_ret_r, 2)
    return {"outcome": outcome, "r": r,
            "dir_correct": (last - price) * direction > 0,
            "fwd_ret_pct": round((last / price - 1) * 100 * (1 if long else -1), 3)}


def _agg(rows: list[dict], min_net: int = 0) -> dict:
    trades = [r for r in rows if r["dir"] != 0 and r["eval"]
              and abs(r["buy"] - r["sell"]) >= min_net]
    n_sig = len(rows)
    n_tr = len(trades)
    if not n_tr:
        return {"n_sig": n_sig, "n_trades": 0}
    wins = sum(1 for r in trades if r["eval"]["outcome"] == "win")
    losses = sum(1 for r in trades if r["eval"]["outcome"] == "loss")
    dir_ok = sum(1 for r in trades if r["eval"]["dir_correct"])
    rs = [r["eval"]["r"] for r in trades]
    decided = wins + losses
    return {
        "n_sig": n_sig, "n_trades": n_tr,
        "hold_pct": round((n_sig - n_tr) / n_sig * 100, 1) if n_sig else 0,
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "dir_acc": round(dir_ok / n_tr * 100, 1),
        "avg_r": round(sum(rs) / len(rs), 3),
        "wins": wins, "losses": losses, "timeouts": n_tr - decided,
    }


TF_CANDIDATES = [("10M", KLType.K_10M), ("15M", KLType.K_15M), ("30M", KLType.K_30M)]


def _scan_strong(df_primary: pd.DataFrame, df5: pd.DataFrame, hold_min: int,
                 start_t: dtime = dtime(10, 0), end_t: dtime = dtime(15, 30),
                 min_net: int = 5) -> list[dict]:
    """扫描整段交易时段的每一根主周期 K 线，对*强信号*(|买-卖|≥min_net)前瞻评估。
    口径是「信号准确性」而非可交易净值（强信号会重叠，不能同时持那么多仓）。"""
    rows = []
    idx = df_primary.index
    for i in range(len(idx)):
        ts = idx[i]
        if not (start_t <= ts.time() <= end_t):
            continue
        d_p = df_primary.iloc[max(0, i - 79):i + 1]      # 末 80 根足够所有指标
        if len(d_p) < 40:
            continue
        d5 = df5[df5.index <= ts].tail(40)
        sig = _signal_at(d_p, d5)
        if not sig or sig["dir"] == 0 or abs(sig["buy"] - sig["sell"]) < min_net:
            continue
        ev = _evaluate(df5, ts, sig["price"], sig["dir"], sig["atr5"], hold_min)
        rows.append({**sig, "eval": ev, "entry_ts": ts})
    return rows


def run_exit_sweep(symbols: list[str], months: int = 3,
                   primary: KLType = KLType.K_30M) -> dict:
    """对 30M *强信号*（合并多只票扩大样本）扫一组「目标/止损/持仓」组合，找哪种
    退出几何能把『方向常对、但被止损刮掉』的边际转成正期望。探索性，小样本易过拟合。"""
    end = date.today()
    start = end - timedelta(days=int(months * 31) + 7)
    sigs = []
    with _moomoo_client() as c:
        q = c.quote
        for sym in symbols:
            code = f"US.{sym.upper()}"
            df5 = _fetch_history(q, code, KLType.K_5M, start, end)
            dfp = _fetch_history(q, code, primary, start, end)
            if df5 is None or dfp is None:
                continue
            for r in _scan_strong(dfp, df5, 30):     # 只取强信号(direction/price/atr5/entry_ts)
                sigs.append({**r, "df5": df5})
    if not sigs:
        return {"error": "无强信号样本"}
    grid_tp = [0.7, 1.0, 1.5, 2.0]
    grid_sl = [0.7, 1.0, 1.5]
    grid_hold = [30, 60, 90]
    out = []
    for hold in grid_hold:
        for tp in grid_tp:
            for sl in grid_sl:
                rows = []
                for s in sigs:
                    ev = _evaluate(s["df5"], s["entry_ts"], s["price"], s["dir"],
                                   s["atr5"], hold, tp, sl)
                    rows.append({"dir": s["dir"], "buy": s["buy"], "sell": s["sell"], "eval": ev})
                a = _agg(rows)
                if a.get("n_trades"):
                    out.append({"hold": hold, "tp": tp, "sl": sl, **a})
    out.sort(key=lambda x: -x["avg_r"])
    return {"symbols": [s.upper() for s in symbols], "n_sig": len(sigs), "grid": out}


def _fmt_sweep(res: dict) -> str:
    if res.get("error"):
        return "❌ " + res["error"]
    L = [f"━━━ 退出几何寻优 · 30M强信号 · {'+'.join(res['symbols'])} ━━━",
         f"强信号样本 {res['n_sig']} 个 (合并多票). 单位=ATR(5M). 未计滑点/佣金.",
         "探索性·小样本易过拟合——只看是否有稳健正期望的方向, 别照单 all-in.",
         "",
         f"{'持仓':>5}{'目标×':>7}{'止损×':>7}{'方向准':>8}{'胜率':>7}{'均R(ATR)':>10}"]
    for row in res["grid"][:12]:      # top 12 by avg_r
        wr = f"{row['win_rate']}%" if row["win_rate"] is not None else "—"
        flag = "  ✅" if row["avg_r"] > 0.05 else ("  ·" if row["avg_r"] > 0 else "  ✗")
        L.append(f"{row['hold']:>4}m{row['tp']:>7}{row['sl']:>7}{row['dir_acc']:>7}%{wr:>7}{row['avg_r']:>10}{flag}")
    L += ["", "(按均R降序取前12; ✅=均R>0.05ATR/信号, 才算有意义的正期望)"]
    return "\n".join(L)


def run_tf_compare(symbol: str, months: int = 3) -> dict:
    """比较 10M/15M/30M 主周期下*强信号*的方向准确度与期望(30min & 60min 持仓)。"""
    end = date.today()
    start = end - timedelta(days=int(months * 31) + 7)
    code = f"US.{symbol.upper()}"
    log.info("tf-compare %s | %s..%s", symbol, start, end)
    with _moomoo_client() as c:
        q = c.quote
        df5 = _fetch_history(q, code, KLType.K_5M, start, end)
        prim = {name: _fetch_history(q, code, kt, start, end) for name, kt in TF_CANDIDATES}
    if df5 is None:
        return {"error": "5M 数据获取失败"}
    n_days = len({ts.date() for ts in df5.index})
    out = {}
    for name, _ in TF_CANDIDATES:
        dfp = prim.get(name)
        if dfp is None:
            out[name] = None
            continue
        out[name] = {30: _agg(_scan_strong(dfp, df5, 30)),
                     60: _agg(_scan_strong(dfp, df5, 60))}
    rng = f"{min(ts.date() for ts in df5.index)} .. {max(ts.date() for ts in df5.index)}"
    return {"symbol": symbol.upper(), "months": months, "n_days": n_days,
            "range": rng, "tf": out}


def _fmt_tf(res: dict) -> str:
    if res.get("error"):
        return "❌ " + res["error"]
    L = [f"━━━ 主周期对比 · 强信号(net≥5, 全时段扫描 10:00-15:30) · {res['symbol']} ━━━",
         f"区间 {res['range']}  ({res['n_days']} 交易日)  目标1.5×/止损1.0×ATR(5M)",
         "",
         f"{'主周期':<6}{'强信号':>6}{'方向准@30':>10}{'胜率@30':>9}{'均R@30':>8}{'方向准@60':>11}{'胜率@60':>9}{'均R@60':>8}"]
    for name, _ in TF_CANDIDATES:
        d = res["tf"].get(name)
        if not d:
            L.append(f"{name:<6}  (无数据)")
            continue
        a30, a60 = d[30], d[60]
        if not a30.get("n_trades"):
            L.append(f"{name:<6}{0:>6}   (无强信号样本)")
            continue
        wr30 = f"{a30['win_rate']}%" if a30["win_rate"] is not None else "—"
        wr60 = f"{a60['win_rate']}%" if a60["win_rate"] is not None else "—"
        L.append(f"{name:<6}{a30['n_trades']:>6}{a30['dir_acc']:>9}%{wr30:>9}{a30['avg_r']:>8}"
                 f"{a60['dir_acc']:>10}%{wr60:>9}{a60['avg_r']:>8}")
    L += ["",
          "方向准 = 强信号未来N分钟收益方向正确比例 (>50% 才有边际)",
          "均R = 平均实现 R (>0 才正期望; 含同根同触保守记负, 未计滑点/佣金)",
          "注: 强信号会重叠 → 这是「信号准确性」非可交易净值。"]
    return "\n".join(L)


# 跨板块/跨涨跌的验证宇宙（不只半导体）— out-of-sample 检验信号边际是否泛化
VALIDATE_UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "GOOGL",
                     "AMZN", "PLTR", "COIN", "JPM", "AVGO", "MU", "SNDK", "INTC"]
OLD_CFG = (1.5, 1.0, 30)    # 旧: 目标1.5× / 止损1.0× / 30min
NEW_CFG = (1.5, 1.5, 60)    # 新: 止损放宽 1.5× / 60min（稳健原则, 非每只挑最优）


def run_validate(symbols: list[str] | None = None, months: int = 3,
                 back_months: int = 0) -> dict:
    """跨多票验证 30M 强信号边际是否泛化, 并用 *固定* 新旧配置对比退出几何。
    用固定配置(不每只调参)= 诚实的 out-of-sample 检验。
    back_months>0 → 把整个窗口往前推(时间 out-of-sample, 测是否只是本期行情)。"""
    syms = symbols or VALIDATE_UNIVERSE
    end = date.today() - timedelta(days=int(back_months * 31))
    start = end - timedelta(days=int(months * 31) + 7)
    per, all_old, all_new = [], [], []
    n_days = 0
    with _moomoo_client() as c:
        q = c.quote
        for sym in syms:
            code = f"US.{sym.upper()}"
            df5 = _fetch_history(q, code, KLType.K_5M, start, end)
            dfp = _fetch_history(q, code, KLType.K_30M, start, end)
            if df5 is None or dfp is None:
                per.append({"sym": sym.upper(), "n": 0})
                continue
            n_days = max(n_days, len({t.date() for t in df5.index}))
            sigs = _scan_strong(dfp, df5, 30)

            def _rows(tp, sl, hold):
                return [{"dir": s["dir"], "buy": s["buy"], "sell": s["sell"],
                         "eval": _evaluate(df5, s["entry_ts"], s["price"], s["dir"],
                                           s["atr5"], hold, tp, sl)} for s in sigs]
            old_rows = _rows(*OLD_CFG)
            new_rows = _rows(*NEW_CFG)
            all_old += old_rows
            all_new += new_rows
            o, n = _agg(old_rows), _agg(new_rows)
            per.append({"sym": sym.upper(), "n": n.get("n_trades", 0),
                        "dir": n.get("dir_acc"), "old_r": o.get("avg_r"),
                        "new_r": n.get("avg_r"), "new_wr": n.get("win_rate")})
            log.info("validate %s: %s strong signals", sym, n.get("n_trades", 0))
    return {"n_days": n_days, "per": per,
            "pooled_old": _agg(all_old), "pooled_new": _agg(all_new)}


def _fmt_validate(res: dict) -> str:
    po, pn = res["pooled_old"], res["pooled_new"]
    L = [f"━━━ 扩张验证 · 30M强信号跨票泛化 ({res['n_days']}交易日) ━━━",
         "固定配置对比: 旧=目标1.5×/止损1.0×/30min   新=目标1.5×/止损1.5×/60min",
         "(用固定配置=out-of-sample; 单位=ATR(5M); 未计滑点/佣金)",
         "",
         f"{'票':<7}{'强信号':>6}{'方向准':>8}{'旧均R':>8}{'新均R':>8}{'新胜率':>8}  趋势"]
    traded = [p for p in res["per"] if p.get("n")]
    for p in sorted(traded, key=lambda x: -(x.get("new_r") or -9)):
        wr = f"{p['new_wr']}%" if p.get("new_wr") is not None else "—"
        better = "↑" if (p.get("new_r") or -9) > (p.get("old_r") or -9) else " "
        L.append(f"{p['sym']:<7}{p['n']:>6}{p['dir']:>7}%{p['old_r']:>8}{p['new_r']:>8}{wr:>8}  {better}")
    skipped = [p["sym"] for p in res["per"] if not p.get("n")]
    if skipped:
        L.append(f"(无强信号/无数据: {', '.join(skipped)})")
    n_pos_new = sum(1 for p in traded if (p.get("new_r") or -9) > 0)
    n_pos_old = sum(1 for p in traded if (p.get("old_r") or -9) > 0)
    n_better = sum(1 for p in traded if (p.get("new_r") or -9) > (p.get("old_r") or -9))
    n_dir = sum(1 for p in traded if (p.get("dir") or 0) > 50)
    L += ["",
          f"汇总({len(traded)}只有强信号):",
          f"  方向准 >50% 的票: {n_dir}/{len(traded)}",
          f"  正期望(均R>0): 旧 {n_pos_old}/{len(traded)} → 新 {n_pos_new}/{len(traded)}",
          f"  放宽止损后变好(新>旧): {n_better}/{len(traded)}",
          "",
          f"  全样本合并: 旧 方向准{po.get('dir_acc')}% 均R{po.get('avg_r')}  →  "
          f"新 方向准{pn.get('dir_acc')}% 均R{pn.get('avg_r')} (胜率{pn.get('win_rate')}%, n={pn.get('n_trades')})",
          "",
          ">50% 方向准 + 合并均R>0 + 多数票变好 = 边际稳健; 否则就是单一行情/小样本噪音。"]
    return "\n".join(L)


def run(symbol: str, months: int = 3, hold_min: int = HOLD_MIN) -> dict:
    end = date.today()
    start = end - timedelta(days=int(months * 31) + 7)
    code = f"US.{symbol.upper()}"
    log.info("scalp backtest %s | %s..%s | hold=%dmin", symbol, start, end, hold_min)

    with _moomoo_client() as c:
        q = c.quote
        df30 = _fetch_history(q, code, KLType.K_30M, start, end)
        df5 = _fetch_history(q, code, KLType.K_5M, start, end)
    if df30 is None or df5 is None:
        return {"error": "数据获取失败"}

    trading_days = sorted({ts.date() for ts in df5.index})
    by_slot: dict[str, list] = {s.strftime("%H:%M"): [] for s in INTRADAY_SLOTS}
    premkt_rows: list = []

    for i, day in enumerate(trading_days):
        # —— 盘中 6 时点 ——
        for slot in INTRADAY_SLOTS:
            ts = datetime.combine(day, slot)
            d30 = df30[df30.index <= ts]
            d5 = df5[df5.index <= ts]
            sig = _signal_at(d30, d5)
            if not sig:
                continue
            # forward-test against the FULL df5 (sliced d5 has no future bars!)
            ev = _evaluate(df5, d5.index[-1], sig["price"], sig["dir"], sig["atr5"], hold_min)
            by_slot[slot.strftime("%H:%M")].append({**sig, "eval": ev, "day": day})

        # —— 盘前读数(用上一交易日收盘技术面预测当日开盘后 60min 方向)——
        if i == 0:
            continue
        prior_end = datetime.combine(trading_days[i - 1], dtime(16, 0))
        d30p = df30[df30.index <= prior_end]
        d5p = df5[df5.index <= prior_end]
        sig = _signal_at(d30p, d5p)
        if not sig:
            continue
        # entry = 当日开盘首根 5M (~09:35) 收盘; 前瞻 60min
        day_bars = df5[(df5.index >= datetime.combine(day, dtime(9, 30))) &
                       (df5.index < datetime.combine(day, dtime(16, 0)))]
        if not len(day_bars):
            continue
        open_ts = day_bars.index[0]
        open_px = float(day_bars["close"].iloc[0])
        ev = _evaluate(df5, open_ts, open_px, sig["dir"], sig["atr5"], 60)
        premkt_rows.append({**sig, "price": open_px, "eval": ev, "day": day})

    return {
        "symbol": symbol.upper(), "months": months, "hold_min": hold_min,
        "n_days": len(trading_days),
        "range": f"{trading_days[0]} .. {trading_days[-1]}" if trading_days else "n/a",
        "intraday": {slot: _agg(rows) for slot, rows in by_slot.items()},
        "intraday_all": _agg([r for rows in by_slot.values() for r in rows]),
        "intraday_strong": _agg([r for rows in by_slot.values() for r in rows], min_net=5),
        "premarket": _agg(premkt_rows),
        "premarket_strong": _agg(premkt_rows, min_net=5),
    }


def _fmt(res: dict) -> str:
    if res.get("error"):
        return "❌ " + res["error"]
    L = [f"━━━ 超短线信号回测 · {res['symbol']} ━━━",
         f"区间 {res['range']}  ({res['n_days']} 交易日)  持仓窗口 {res['hold_min']}min",
         f"目标 {TP_MULT}×ATR / 止损 {SL_MULT}×ATR(5M)",
         "",
         f"{'时点':<7}{'信号':>5}{'交易':>5}{'方向准':>8}{'胜率':>7}{'均R':>7}{'观望%':>7}"]
    for slot, a in res["intraday"].items():
        if not a.get("n_trades"):
            L.append(f"{slot:<7}{a.get('n_sig',0):>5}{0:>5}{'—':>8}{'—':>7}{'—':>7}{a.get('hold_pct',0):>6}%")
            continue
        wr = f"{a['win_rate']}%" if a["win_rate"] is not None else "—"
        L.append(f"{slot:<7}{a['n_sig']:>5}{a['n_trades']:>5}{a['dir_acc']:>7}%{wr:>7}{a['avg_r']:>7}{a['hold_pct']:>6}%")
    al = res["intraday_all"]
    if al.get("n_trades"):
        wr = f"{al['win_rate']}%" if al["win_rate"] is not None else "—"
        L += ["", f"{'盘中合计':<7}{al['n_sig']:>5}{al['n_trades']:>5}{al['dir_acc']:>7}%{wr:>7}{al['avg_r']:>7}{al['hold_pct']:>6}%"]
    pm = res["premarket"]
    if pm.get("n_trades"):
        wr = f"{pm['win_rate']}%" if pm["win_rate"] is not None else "—"
        L += [f"{'盘前读数':<7}{pm['n_sig']:>5}{pm['n_trades']:>5}{pm['dir_acc']:>7}%{wr:>7}{pm['avg_r']:>7}{pm['hold_pct']:>6}%",
              "(盘前=上一日收盘技术面预测开盘后60min方向)"]
    # 只看高信心(net≥5, 即「强烈买入/强烈卖出」)— 你实际会动手的那批
    L.append("")
    for tag, key in [("盘中·强", "intraday_strong"), ("盘前·强", "premarket_strong")]:
        a = res.get(key, {})
        if a.get("n_trades"):
            wr = f"{a['win_rate']}%" if a["win_rate"] is not None else "—"
            L.append(f"{tag:<7}{a['n_sig']:>5}{a['n_trades']:>5}{a['dir_acc']:>7}%{wr:>7}{a['avg_r']:>7}{a['hold_pct']:>6}%")
        else:
            L.append(f"{tag:<7}  (强信号样本不足/无)")
    L += ["",
          "方向准 = 未来N分钟收益方向与信号一致的比例 (>50% 才有边际)",
          "胜率 = 先到目标/先到止损 (同根同触保守记负)",
          "均R = 平均实现 R 倍数 (>0 才正期望)"]
    return "\n".join(L)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s",
                        handlers=[logging.StreamHandler()])
    args = sys.argv[1:]
    if args and args[0] == "tf":      # 主周期对比模式
        syms = (args[1] if len(args) > 1 else "MU").split(",")
        months = int(args[2]) if len(args) > 2 else 3
        for s in syms:
            print("\n" + _fmt_tf(run_tf_compare(s.strip(), months)))
        return
    if args and args[0] == "exit":    # 退出几何寻优(合并多票)
        syms = (args[1] if len(args) > 1 else "MU,SNDK").split(",")
        months = int(args[2]) if len(args) > 2 else 3
        print("\n" + _fmt_sweep(run_exit_sweep([s.strip() for s in syms], months)))
        return
    if args and args[0] == "validate":   # 跨票泛化验证(固定新旧配置)
        rest = args[1:]
        # 数字参数=月数; 非数字=自定义票列表(逗号分隔); 都没有=默认宇宙+3个月
        syms = next(([s.strip() for s in a.split(",")] for a in rest if not a.isdigit()), None)
        months = next((int(a) for a in rest if a.isdigit()), 3)
        print("\n" + _fmt_validate(run_validate(syms, months)))
        return
    syms = (args[0] if args else "MU").split(",")
    months = int(args[1]) if len(args) > 1 else 3
    for s in syms:
        print("\n" + _fmt(run(s.strip(), months)))


if __name__ == "__main__":
    main()
