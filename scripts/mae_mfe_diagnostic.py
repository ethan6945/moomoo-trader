#!/usr/bin/env python3
"""MAE/MFE 诊断 — 把「靠猜的 ATR 倍数」换成「从成交分布里推出来的数」。

替换三组拍脑袋参数:
    SL_ATR_MULT   止损宽度   ← 赢单的 MAE 分布(好单子最多扛多少热)
    TP_ATR_MULT   止盈距离   ← 赢单的 MFE 分布(利润实际活在哪)
    TP1_R / TP2_R 分批档位   ← 赢单的 MFE(以 R 计),并保证 TP1 < TP2 < 整体 TP

名词:
    MAE  Maximum Adverse Excursion   持仓期间最大浮亏(逆向走了多远)
    MFE  Maximum Favorable Excursion 持仓期间最大浮盈(顺向走了多远)

为什么有意义:
    · 赢单的 MAE 告诉你「一笔好单子最多回撤多少」——止损就放在那条边界外一点。
      止损比这窄,会把本来会赢的单子提前打掉;比这宽,只是白白放大单笔风险金额。
    · 赢单的 MFE 告诉你「利润真正活在哪」——止盈 / 分批就放那儿。
      止盈比这远,大多数单子根本够不到,等于没有止盈(全靠 trail / max-hold 退出)。

所有 excursion 同时用两种单位表示:
    · ATR(入场时)单位 → 直接映射到 SL_ATR_MULT / TP_ATR_MULT
    · R(初始风险 = 入场 − 初始止损 = SL_ATR_MULT × ATR)单位 → 直接映射到 TP1_R / TP2_R

⚠ 诚实前提:这脚本给的是「假设」,不是「设定」。它从一段历史(回测样本)里读出
   excursion 分布,告诉你参数大概该落在哪。它不能替代 walk-forward 和前向 paper 验证。
   样本越小、越是单一 regime,结论越脆。脚本会把样本量和来源标在最显眼处。

用法:
    python -m scripts.mae_mfe_diagnostic                 # 回测(实盘 watchlist,180d)+ 实盘成交
    python -m scripts.mae_mfe_diagnostic --source live   # 只看真实成交(目前样本极小)
    python -m scripts.mae_mfe_diagnostic --days 360 --full-pool
    python -m scripts.mae_mfe_diagnostic --json data/mae_mfe_diagnostic.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Allow both `python -m scripts.mae_mfe_diagnostic` and `python scripts/mae_mfe_diagnostic.py`.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import settings  # noqa: E402

DB_PATH = _ROOT / "data" / "trader.db"


# ──────────────────────────────────────────────────────────────────────────
# Per-trade excursion record
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Excursion:
    symbol: str
    source: str            # "backtest" | "live"
    strategy: str
    exit_reason: str
    pnl: float
    is_winner: bool
    # excursions, all positive magnitudes
    mae_atr: float         # adverse, in ATR-at-entry units
    mfe_atr: float         # favorable, in ATR-at-entry units
    mae_r: float           # adverse, in R units (R = initial risk per share)
    mfe_r: float           # favorable, in R units
    bars_held: int = 0     # window length (bars) — for the --debug dump
    mfe_tracked: bool = True   # live: was mfe actually recorded (mfe_pct>0)?


# ──────────────────────────────────────────────────────────────────────────
# Backtest source — run the honest engine over real bars, then recompute
# MAE/MFE from the bar window. We anchor every conversion on `atr_at_entry`
# (NOT the stored `stop_loss`, which gets mutated to the breakeven/trailed
# level mid-trade and would give R=0 on breakeven trades).
# ──────────────────────────────────────────────────────────────────────────
def _bars_high_low(df: pd.DataFrame, entry_idx: int, exit_date, init_stop: float,
                   tp: float) -> tuple[float, float, int] | None:
    """Slice [entry, exit] and return (min_low, max_high, bars_held).

    MFE/MAE are read straight off the bars (NOT the engine's `highest_high`
    field — that only updates inside the trailing branch and is stuck at the
    entry value on the ~vast majority of trades, so it is useless as an MFE
    source). For a TP exit the window ends at the TP touch, so a TP-exit's MFE
    is capped near the take-profit — that's why the TP section leans on the
    *reach-rate* table rather than raw MFE percentiles to judge TP placement.

    Exit bar is reconstructed conservatively: the first bar at/after entry that
    touches the initial stop or the take-profit; otherwise the last bar on the
    exit date (covers MAX_HOLD / TRAIL / EOD where the position rode the whole
    window). Truncating at the first stop touch keeps MAE from counting
    drawdown that happened *after* the position would already be closed.
    """
    n = len(df)
    if entry_idx < 0 or entry_idx >= n:
        return None
    # last bar index whose date <= exit_date
    end_idx = entry_idx
    for k in range(entry_idx, n):
        if df.index[k].date() <= exit_date:
            end_idx = k
        else:
            break
    lows = df["low"].values
    highs = df["high"].values
    exit_idx = end_idx
    for k in range(entry_idx, end_idx + 1):
        if lows[k] <= init_stop or highs[k] >= tp:
            exit_idx = k
            break
    win_low = float(np.min(lows[entry_idx:exit_idx + 1]))
    win_high = float(np.max(highs[entry_idx:exit_idx + 1]))
    return win_low, win_high, exit_idx - entry_idx


def collect_backtest(days: int, full_pool: bool) -> list[Excursion]:
    from src import backtest as bt

    # universe: live watchlist by default (what actually trades), or the full
    # liquidity pool for a bigger (less representative) sample.
    if full_pool:
        pool = json.loads((_ROOT / "config" / "universe_pool.json").read_text())
        tickers = pool.get("tickers") or pool.get("symbols") or []
    else:
        tickers = []   # empty → engine loads config/watchlist.json

    sl_mult = float(settings.sl_atr_mult)
    cfg = bt.BacktestConfig(
        days=days,
        timeframe=str(settings.timeframe),
        threshold=70.0,
        tickers=list(tickers),
        account_usd=float(settings.account_usd),
        risk_per_trade=float(settings.risk_per_trade),
        max_position_pct=float(settings.max_position_pct),
        max_hold_days=int(settings.max_hold_days),
        tp_atr_mult=float(settings.tp_atr_mult),
        sl_atr_mult=sl_mult,
        # Measure the *natural* excursion: disable partials so a +tranche close
        # doesn't truncate MFE before price shows where it really wanted to go.
        use_scale_out=False,
    )

    print(f"  fetching bars (OpenD) for {len(tickers) or 'watchlist'} tickers, "
          f"{days}d {cfg.timeframe} …", flush=True)
    cache = bt.prefetch_data(cfg)
    if not cache.get("per_ticker"):
        raise RuntimeError("prefetch returned 0 tickers — OpenD down? cannot run backtest source")
    print(f"  simulating honest engine …", flush=True)
    result = bt._run_live_engine(cfg, cache)
    trades = result["trades"]
    per_ticker = cache["per_ticker"]
    print(f"  engine produced {len(trades)} closed trades", flush=True)

    out: list[Excursion] = []
    skipped = 0
    for t in trades:
        sym = t["symbol"]
        bundle = per_ticker.get(sym)
        if not bundle:
            skipped += 1
            continue
        df = bundle["intraday"]
        atr0 = float(t.get("atr_at_entry") or 0.0)
        entry = float(t["entry_price"])
        if atr0 <= 0 or entry <= 0:
            skipped += 1
            continue
        r_ps = sl_mult * atr0                      # initial risk per share
        init_stop = entry - r_ps
        tp = float(t.get("take_profit") or (entry + cfg.tp_atr_mult * atr0))
        try:
            exit_date = datetime.fromisoformat(str(t["exit_date"])).date()
        except Exception:
            exit_date = df.index[-1].date()
        hl = _bars_high_low(df, int(t["entry_bar"]), exit_date, init_stop, tp)
        if hl is None:
            skipped += 1
            continue
        win_low, win_high, bars_held = hl
        mae_atr = max(0.0, (entry - win_low) / atr0)
        mfe_atr = max(0.0, (win_high - entry) / atr0)
        out.append(Excursion(
            symbol=sym, source="backtest",
            strategy=str(t.get("strategy") or "n/a"),
            exit_reason=str(t.get("exit_reason") or ""),
            pnl=float(t.get("pnl") or 0.0),
            is_winner=float(t.get("pnl") or 0.0) > 0,
            mae_atr=mae_atr, mfe_atr=mfe_atr,
            mae_r=mae_atr / sl_mult, mfe_r=mfe_atr / sl_mult,
            bars_held=bars_held,
        ))
    if skipped:
        print(f"  ({skipped} trades skipped — missing bars/atr)", flush=True)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Live source — the real fills. mfe_pct / mae_pct are already stored (price %
# vs entry). Convert to R via the stop distance, and to ATR via the configured
# SL_ATR_MULT.  NOTE: tiny sample today; some rows have mfe_pct == 0 (excursion
# was never tracked) — those are excluded from MFE stats.
# ──────────────────────────────────────────────────────────────────────────
def collect_live() -> list[Excursion]:
    if not DB_PATH.exists():
        return []
    sl_mult = float(settings.sl_atr_mult)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT symbol, entry, stop, pnl, mfe_pct, mae_pct, strategy, exit_reason "
        "FROM closed_trades"
    ).fetchall()
    con.close()

    out: list[Excursion] = []
    for r in rows:
        entry = float(r["entry"] or 0)
        stop = float(r["stop"] or 0)
        if entry <= 0 or stop <= 0 or stop >= entry:
            continue
        stop_dist_pct = (entry - stop) / entry * 100.0      # = R in price-%
        mae_pct = abs(float(r["mae_pct"] or 0.0))
        mfe_pct = float(r["mfe_pct"] or 0.0)
        mae_r = mae_pct / stop_dist_pct if stop_dist_pct else 0.0
        mfe_r = mfe_pct / stop_dist_pct if stop_dist_pct else 0.0
        out.append(Excursion(
            symbol=str(r["symbol"]), source="live",
            strategy=str(r["strategy"] or "n/a"),
            exit_reason=str(r["exit_reason"] or ""),
            pnl=float(r["pnl"] or 0.0),
            is_winner=float(r["pnl"] or 0.0) > 0,
            mae_atr=mae_r * sl_mult, mfe_atr=mfe_r * sl_mult,
            mae_r=mae_r, mfe_r=mfe_r,
            mfe_tracked=(mfe_pct > 0),   # 0 == excursion never recorded
        ))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Stats helpers
# ──────────────────────────────────────────────────────────────────────────
_PCTS = [50, 75, 85, 90, 95]


def _pcts(vals: list[float]) -> dict[int, float]:
    if not vals:
        return {p: float("nan") for p in _PCTS}
    arr = np.asarray(vals, dtype=float)
    return {p: float(np.percentile(arr, p)) for p in _PCTS}


def _fmt_pcts(d: dict[int, float], unit: str = "") -> str:
    return "  ".join(f"P{p}={d[p]:.2f}{unit}" for p in _PCTS)


def _reach_rate(vals: list[float], level: float) -> float:
    """Fraction of trades whose excursion reached `level` (>=)."""
    if not vals:
        return float("nan")
    arr = np.asarray(vals, dtype=float)
    return float(np.mean(arr >= level))


# ──────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────
def report(exs: list[Excursion], emit_json: str | None, debug: bool = False) -> None:
    if debug:
        print("\n[debug] first 8 backtest windows (entry→exit reconstruction):")
        print(f"  {'sym':5} {'win?':5} {'bars':>4} {'reason':9} {'MAE_atr':>7} {'MFE_atr':>7} {'MAE_R':>6} {'MFE_R':>6}")
        for e in [x for x in exs if x.source == "backtest"][:8]:
            print(f"  {e.symbol:5} {'W' if e.is_winner else 'L':5} {e.bars_held:>4} "
                  f"{e.exit_reason:9} {e.mae_atr:>7.2f} {e.mfe_atr:>7.2f} {e.mae_r:>6.2f} {e.mfe_r:>6.2f}")
    cur_sl = float(settings.sl_atr_mult)
    cur_tp = float(settings.tp_atr_mult)
    cur_tp1_r = float(settings.tp1_r)
    cur_tp2_r = float(settings.tp2_r)
    full_tp_r = cur_tp / cur_sl if cur_sl else float("nan")

    bt_n = sum(1 for e in exs if e.source == "backtest")
    lv_n = sum(1 for e in exs if e.source == "live")
    winners = [e for e in exs if e.is_winner]
    losers = [e for e in exs if not e.is_winner]

    bar = "═" * 74
    print(f"\n{bar}\n MAE / MFE 诊断报告\n{bar}")
    print(f" 样本: {len(exs)} 笔 (backtest={bt_n}, live={lv_n})  |  "
          f"赢 {len(winners)} / 亏 {len(losers)}")
    print(f" 当前参数: SL_ATR_MULT={cur_sl}  TP_ATR_MULT={cur_tp}  "
          f"TP1_R={cur_tp1_r}  TP2_R={cur_tp2_r}  (整体 TP = {full_tp_r:.2f}R)")
    if len(exs) < 30:
        print(" ⚠ 样本 < 30:以下分位数噪声很大,只能当方向性参考,不能直接当设定。")
    print(" 方法:MAE/MFE 直接从 K 线读(entry→止损/止盈触碰 或 退出日末根);TP 退出的单子 MFE")
    print("       被封在 TP 附近,故止盈用「触达率」表判断、而非 MFE 分位数。")

    # ── 0. 死分批告警 (the +3R/+6R vs +2.29R contradiction) ──
    print(f"\n── 0. 分批止盈一致性检查 ──")
    if cur_tp1_r >= full_tp_r or cur_tp2_r >= full_tp_r:
        print(f" ✗ TP1_R={cur_tp1_r} / TP2_R={cur_tp2_r} 都在整体 TP({full_tp_r:.2f}R)之上 →")
        print(f"   分批档位永远摸不到(整个仓位先在 +{cur_tp} ATR 的 runner 处全平),scale-out 实质失效。")
        print(f"   要么把 TP1_R/TP2_R 压到 < {full_tp_r:.2f}R,要么抬高 TP_ATR_MULT。下面用 MFE 数据给一致的档位。")
    else:
        print(f" ✓ TP1_R={cur_tp1_r} < TP2_R={cur_tp2_r} < 整体 TP({full_tp_r:.2f}R),阶梯一致。")

    # ── 1. MAE → 止损 ──
    w_mae_atr = [e.mae_atr for e in winners]
    l_mae_atr = [e.mae_atr for e in losers]
    wp = _pcts(w_mae_atr)
    print(f"\n── 1. MAE(逆向浮亏)→ 止损宽度 ──")
    print(f" 赢单 MAE(ATR): {_fmt_pcts(wp)}")
    print(f" 亏单 MAE(ATR): {_fmt_pcts(_pcts(l_mae_atr))}")
    print(f"\n 止损宽度权衡(候选 SL_ATR_MULT → 会被打掉的比例):")
    print(f"   {'SL×ATR':>7} | {'砍掉赢单%':>9} | {'砍掉亏单%':>9}  (砍赢单=成本, 砍亏单=早止损=收益)")
    rec_sl = None
    for s in [2.0, 2.5, 3.0, 3.5, 4.0]:
        wk = _reach_rate(w_mae_atr, s) * 100
        lk = _reach_rate(l_mae_atr, s) * 100
        star = " ←现值" if abs(s - cur_sl) < 1e-6 else ""
        print(f"   {s:>7.1f} | {wk:>8.0f}% | {lk:>8.0f}%{star}")
    # recommend: just outside where ~85% of winners pull back to
    if w_mae_atr:
        rec_sl = round(wp[85] + 0.25, 2)
        print(f" → 建议 SL_ATR_MULT ≈ {rec_sl}  (赢单 P85 MAE {wp[85]:.2f} + 0.25 缓冲;"
              f"约 {_reach_rate(w_mae_atr, rec_sl)*100:.0f}% 赢单会被这个止损误杀)")

    # ── 2. MFE → 止盈 ──
    w_mfe_atr = [e.mfe_atr for e in winners]
    all_mfe_atr = [e.mfe_atr for e in exs]
    wpf = _pcts(w_mfe_atr)
    print(f"\n── 2. MFE(顺向浮盈)→ 止盈距离 ──")
    print(f" 赢单 MFE(ATR): {_fmt_pcts(wpf)}")
    print(f" 全样本 MFE(ATR): {_fmt_pcts(_pcts(all_mfe_atr))}")
    print(f"\n 止盈触达率(候选 TP_ATR_MULT → 有多少单子真的摸到):")
    print(f"   {'TP×ATR':>7} | {'赢单触达%':>9} | {'全样本触达%':>11}")
    for tp in [3.0, 4.0, 6.0, 8.0, 10.0]:
        wr = _reach_rate(w_mfe_atr, tp) * 100
        ar = _reach_rate(all_mfe_atr, tp) * 100
        star = " ←现值" if abs(tp - cur_tp) < 1e-6 else ""
        print(f"   {tp:>7.1f} | {wr:>8.0f}% | {ar:>10.0f}%{star}")
    if w_mfe_atr:
        print(f" → 赢单 MFE 中位数 {wpf[50]:.2f} ATR;现 TP={cur_tp} ATR 的赢单触达率 "
              f"{_reach_rate(w_mfe_atr, cur_tp)*100:.0f}%。")
        print(f"   若触达率很低,固定远 TP 形同虚设(多数靠 trail/max-hold 退出)——"
              f"考虑 runner TP≈P75({wpf[75]:.1f} ATR)+ 对尾部用 trailing。")

    # ── 3. MFE(R) → 分批档位 ──
    w_mfe_r = [e.mfe_r for e in winners]
    wpr = _pcts(w_mfe_r)
    print(f"\n── 3. MFE(R 单位)→ 分批档位 TP1_R / TP2_R ──")
    print(f" 赢单 MFE(R): {_fmt_pcts(wpr, 'R')}")
    rec = None
    if w_mfe_r:
        # tranches where a meaningful share of winners actually reach them,
        # forced monotone and strictly under the full-TP ceiling.
        tp1 = round(min(wpr[50], full_tp_r * 0.6), 1)
        tp2 = round(min(wpr[75], full_tp_r * 0.85), 1)
        tp1 = max(0.5, tp1)
        tp2 = max(tp1 + 0.3, tp2)
        rec = (tp1, tp2)
        print(f" → 建议 TP1_R≈{tp1}(赢单 ~{_reach_rate(w_mfe_r, tp1)*100:.0f}% 能摸到), "
              f"TP2_R≈{tp2}(~{_reach_rate(w_mfe_r, tp2)*100:.0f}%),"
              f"均 < 整体 TP {full_tp_r:.2f}R → 阶梯自洽,分批能真正触发。")
        if tp2 >= full_tp_r:
            print(f"   ⚠ 数据想要的 TP2({wpr[75]:.1f}R)≥ 当前整体 TP({full_tp_r:.2f}R):"
                  f"说明 TP_ATR_MULT 太近,先把它抬到 ≳ {wpr[75]*cur_sl:.1f} ATR 才放得下分批。")

    # ── exit-reason mix (context) ──
    print(f"\n── 退出方式分布(上下文)──")
    reasons: dict[str, int] = {}
    for e in exs:
        reasons[e.exit_reason or "?"] = reasons.get(e.exit_reason or "?", 0) + 1
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))

    print(f"\n{bar}")
    print(" 诚实边界:以上是从一段历史样本读出的「假设」。下一步必须 walk-forward / 前向 paper")
    print(" 验证这些数在样本外是否还成立 —— 让数据否决掉每一个值,包括它自己推出来的。")
    print(f"{bar}\n")

    if emit_json:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "n_total": len(exs), "n_backtest": bt_n, "n_live": lv_n,
            "n_winners": len(winners), "n_losers": len(losers),
            "current": {"sl_atr_mult": cur_sl, "tp_atr_mult": cur_tp,
                        "tp1_r": cur_tp1_r, "tp2_r": cur_tp2_r,
                        "full_tp_r": round(full_tp_r, 3)},
            "winner_mae_atr_pcts": _pcts(w_mae_atr),
            "winner_mfe_atr_pcts": _pcts(w_mfe_atr),
            "winner_mfe_r_pcts": _pcts(w_mfe_r),
            "recommend": {
                "sl_atr_mult": rec_sl,
                "tp1_r": rec[0] if rec else None,
                "tp2_r": rec[1] if rec else None,
            },
            "scale_out_dead": (cur_tp1_r >= full_tp_r or cur_tp2_r >= full_tp_r),
        }
        Path(emit_json).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f" wrote {emit_json}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="MAE/MFE diagnostic — derive SL/TP/scale-out from trade excursions")
    ap.add_argument("--source", choices=["backtest", "live", "both"], default="both")
    ap.add_argument("--days", type=int, default=180, help="backtest window (default 180)")
    ap.add_argument("--full-pool", action="store_true", help="use the full universe_pool instead of the live watchlist")
    ap.add_argument("--json", dest="json_out", default=None, help="also write a machine-readable summary to this path")
    ap.add_argument("--debug", action="store_true", help="dump the first few reconstructed trade windows")
    args = ap.parse_args()

    exs: list[Excursion] = []
    if args.source in ("backtest", "both"):
        try:
            print("[backtest source]", flush=True)
            exs += collect_backtest(args.days, args.full_pool)
        except Exception as e:
            print(f"  backtest source failed: {e}", flush=True)
            if args.source == "backtest":
                sys.exit(1)
    if args.source in ("live", "both"):
        print("[live source]", flush=True)
        live = collect_live()
        print(f"  {len(live)} closed trades from DB", flush=True)
        exs += live

    if not exs:
        print("no trades collected — nothing to analyse.")
        sys.exit(1)
    report(exs, args.json_out, debug=args.debug)


if __name__ == "__main__":
    main()
