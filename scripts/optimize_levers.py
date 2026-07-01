"""optimize_levers — run every proposed change through the HONEST engine and
look at the data, head-to-head against the current live baseline.

Replaces "tune the number until it feels right" with a sweep that obeys the
project's iron rule: a lever only wins if it RAISES $/day AND does NOT worsen
max MTM drawdown (and ideally holds on a second, broader universe).

Faithful baseline: this mirrors `_run_live_engine` EXACTLY (momentum strategy,
VIX sizing, regime up-scaling at REGIME_BULL_MULT, earnings gate, real moomoo
commissions, breakeven stop, and all the Phase-0 realism knobs), then changes
ONE lever per row. enforce_cash=True → the deleveraged $5k cash account.

Levers tested (the text's exit-side + sizing ideas):
  · stop width            SL_ATR_MULT          (MAE study said winners top ~2.5 ATR)
  · scale-out ladder      TP1_R / TP2_R        (current 3R/6R sit ABOVE the 2.29R TP → dead)
  · take-profit           TP_ATR_MULT
  · trailing runner       use_trailing_stop    ("runner + trail the tail")
  · risk per trade        RISK_PER_TRADE       (+ Kelly-derived f*)
  · max hold              MAX_HOLD_DAYS        (from winner hold distribution)
  · combined data-derived best-of-each

Data note: moomoo OpenD HOUR_1 history caps at ~150d, so 180d and 360d return
the SAME bars — there is no independent 2nd time-window for hourly. The
robustness check here is a 2nd UNIVERSE (--pool, the full liquidity pool) on
the same window: a lever that helps on both the 20-name watchlist and the
72-name pool is less likely curve-fit.

Run:  .venv/bin/python3 scripts/optimize_levers.py            # watchlist
      .venv/bin/python3 scripts/optimize_levers.py --pool     # + pool robustness
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, prefetch_data  # noqa: E402
from src.backtest_v3 import simulate_v3                  # noqa: E402
from src.config import settings                          # noqa: E402
from src import runtime_config as rc                      # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(message)s")
import builtins as _b  # noqa: E402
_orig = _b.print
def print(*a, **k):  # noqa: A001
    k.setdefault("flush", True); _orig(*a, **k)

DD_CAP = settings.dd_halt_pct
WATCHLIST = json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"]
POOL = json.loads((ROOT / "config" / "universe_pool.json").read_text()).get("tickers", [])
FULL_TP_R = settings.tp_atr_mult / settings.sl_atr_mult


def live_base(days: int, tickers: list[str]) -> BacktestConfig:
    """The faithful current-live config — same transform `_run_live_engine` applies."""
    cfg = BacktestConfig(
        days=days, timeframe=str(settings.timeframe), threshold=70.0, tickers=tickers,
        account_usd=float(settings.account_usd), risk_per_trade=float(settings.risk_per_trade),
        max_position_pct=float(settings.max_position_pct), max_hold_days=int(settings.max_hold_days),
        tp_atr_mult=float(settings.tp_atr_mult), sl_atr_mult=float(settings.sl_atr_mult),
        max_gap_pct=float(settings.max_gap_pct),
        dd_size_cut_pct=float(settings.dd_size_cut_pct), dd_halt_pct=float(settings.dd_halt_pct),
        apply_mr_strategy=False,
    )
    # mirror _run_live_engine() exactly:
    return replace(
        cfg, apply_vix_sizing=True, apply_earnings_gate=True, use_realistic_commission=True,
        apply_momentum_strategy=True, use_regime_scaling=True,
        regime_bull_mult=settings.regime_bull_mult, regime_vix_calm=settings.regime_vix_calm,
        scan_grid_exits=True, entry_fill_open_only=True, no_same_day_daily=True,
        reclamp_position_cap=True, apply_trade_windows=True,
        max_new_names_per_scan=settings.max_new_names_per_scan,
        use_breakeven_stop=settings.use_breakeven_stop,
        breakeven_trigger_r=rc.breakeven_trigger_r(),
        # current live scale-out is auto-disabled (3R/6R above the 2.29R TP)
        use_scale_out=False,
    )


def evaluate(base_cfg: BacktestConfig, overrides: dict, cache: dict) -> dict:
    cfg = replace(base_cfg, **overrides)
    res = simulate_v3(cfg, cache, enforce_cash=True)
    m = res["metrics"]
    reasons: dict[str, int] = {}
    for t in res["trades"]:
        r = t.get("exit_reason") or "?"
        reasons[r] = reasons.get(r, 0) + 1
    return {
        "trd": m.get("total_trades", 0), "wr": m.get("win_rate_pct", 0),
        "pf": m.get("profit_factor", 0), "sortino": m.get("sortino_ratio", 0),
        "net": m.get("net_pnl_usd", 0), "daily": m.get("daily_pnl_usd", 0),
        "ddm": m.get("max_dd_mtm_pct", 0), "gross": m.get("peak_gross_exposure_pct", 0),
        "cashlim": m.get("n_cash_limited_entries", 0),
        "avg_win": m.get("avg_win_usd", 0), "avg_loss": m.get("avg_loss_usd", 0),
        "reasons": reasons, "_metrics": m, "_trades": res["trades"],
    }


HDR = (f"{'variant':<26}{'trd':>4}{'WR%':>6}{'PF':>6}{'Sortino':>8}{'$/day':>7}"
       f"{'mtmDD%':>8}{'fires':>16}{'Δ$/day':>8}  pass")


def fmt_fires(reasons: dict) -> str:
    parts = []
    for k in ("TP1", "TP2", "TRAIL"):
        if reasons.get(k):
            parts.append(f"{k}:{reasons[k]}")
    return ",".join(parts) if parts else "-"


def show(title: str, base_row: dict, rows: list[tuple[str, dict]]) -> None:
    print(f"\n── {title} ──")
    print(HDR)
    print("-" * 99)
    bd, bdd = base_row["daily"], base_row["ddm"]
    for label, r in rows:
        d = r["daily"] - bd
        is_base = label.endswith("(base)")
        # iron rule: more $/day AND not worse DD (allow 0.2pp noise)
        ok = (d > 0) and (r["ddm"] <= bdd + 0.2)
        mark = "BASE" if is_base else ("  ✓" if ok else ("  ✗DD" if r["ddm"] > bdd + 0.2 else "  ·"))
        print(f"{label:<26}{r['trd']:>4}{r['wr']:>6.1f}{r['pf']:>6.2f}{r['sortino']:>8.2f}"
              f"{r['daily']:>+7.1f}{r['ddm']:>8.2f}{fmt_fires(r['reasons']):>16}"
              f"{d:>+8.1f}{mark}")


def kelly_fraction(row: dict) -> tuple[float, float]:
    """f* = W − (1−W)/b, b = avg_win/|avg_loss|. Returns (full, half)."""
    m = row["_metrics"]
    w = m.get("win_rate_pct", 0) / 100.0
    aw, al = m.get("avg_win_usd", 0), abs(m.get("avg_loss_usd", 0))
    if al <= 0 or aw <= 0:
        return 0.0, 0.0
    b = aw / al
    f = w - (1 - w) / b
    return f, f / 2


def hold_distribution(trades: list[dict]) -> None:
    """Business-day hold per winner/loser → informs MAX_HOLD_DAYS."""
    import numpy as np
    def bdays(a, b):
        try:
            return int(np.busday_count(np.datetime64(a[:10]), np.datetime64(b[:10])))
        except Exception:
            return None
    win, los = [], []
    for t in trades:
        h = bdays(t.get("entry_date", ""), t.get("exit_date", ""))
        if h is None:
            continue
        (win if t.get("pnl", 0) > 0 else los).append(h)
    def pcts(v):
        if not v:
            return "n/a"
        a = np.asarray(v)
        return f"median={np.median(a):.0f}  P75={np.percentile(a,75):.0f}  P90={np.percentile(a,90):.0f}  max={a.max()}"
    print(f"\n── 持仓时长(交易日)→ MAX_HOLD_DAYS ──")
    print(f"  赢单 (n={len(win)}): {pcts(win)}")
    print(f"  亏单 (n={len(los)}): {pcts(los)}")
    print(f"  当前 MAX_HOLD_DAYS={settings.max_hold_days}")


def run_universe(name: str, tickers: list[str], days: int) -> dict:
    print(f"\n{'='*99}\n  UNIVERSE: {name} ({len(tickers)} names) · {days}d {settings.timeframe} · "
          f"enforce_cash $5k · DD cap {DD_CAP:.0f}%\n"
          f"  baseline = current live (.env): SL={settings.sl_atr_mult} TP={settings.tp_atr_mult} "
          f"(={FULL_TP_R:.2f}R)  RISK={settings.risk_per_trade}  MAXHOLD={settings.max_hold_days}  "
          f"scale-out=OFF(dead)\n{'='*99}")
    base = live_base(days, tickers)
    print("  prefetching bars …")
    cache = prefetch_data(base)
    if not cache.get("per_ticker"):
        raise RuntimeError("0 tickers — OpenD down?")

    base_row = evaluate(base, {}, cache)

    # 1. STOP WIDTH
    show("1. 止损宽度 SL_ATR_MULT", base_row, [
        ("SL 2.5", evaluate(base, dict(sl_atr_mult=2.5), cache)),
        ("SL 2.8", evaluate(base, dict(sl_atr_mult=2.8), cache)),
        ("SL 3.0", evaluate(base, dict(sl_atr_mult=3.0), cache)),
        ("SL 3.5 (base)", base_row),
        ("SL 4.0", evaluate(base, dict(sl_atr_mult=4.0), cache)),
    ])

    # 2. SCALE-OUT LADDER (tranches must sit BELOW the 2.29R TP ceiling to fire)
    show(f"2. 分批止盈 TP1_R/TP2_R (整体 TP={FULL_TP_R:.2f}R 是天花板)", base_row, [
        ("off (base)", base_row),
        ("ladder 1.0/2.0", evaluate(base, dict(use_scale_out=True, tp1_r=1.0, tp2_r=2.0), cache)),
        ("ladder 1.4/1.9", evaluate(base, dict(use_scale_out=True, tp1_r=1.4, tp2_r=1.9), cache)),
        ("ladder .75/1.5", evaluate(base, dict(use_scale_out=True, tp1_r=0.75, tp2_r=1.5), cache)),
        ("live 3/6 (dead)", evaluate(base, dict(use_scale_out=True, tp1_r=3.0, tp2_r=6.0), cache)),
    ])

    # 3. TAKE-PROFIT
    show("3. 止盈距离 TP_ATR_MULT", base_row, [
        ("TP 6", evaluate(base, dict(tp_atr_mult=6.0), cache)),
        ("TP 8 (base)", base_row),
        ("TP 10", evaluate(base, dict(tp_atr_mult=10.0), cache)),
        ("TP 12", evaluate(base, dict(tp_atr_mult=12.0), cache)),
    ])

    # 4. TRAILING RUNNER (let the trail, not the TP/calendar, decide the exit)
    show("4. 移动止损 runner (trail the tail, max_hold=14)", base_row, [
        ("no trail (base)", base_row),
        ("trail 2.5×ATR", evaluate(base, dict(use_trailing_stop=True, trail_atr_mult=2.5,
                                              trail_activate_r=1.0, max_hold_days=14), cache)),
        ("trail 3.0×ATR", evaluate(base, dict(use_trailing_stop=True, trail_atr_mult=3.0,
                                              trail_activate_r=1.0, max_hold_days=14), cache)),
    ])

    # 5. RISK PER TRADE (+ Kelly)
    kf, khalf = kelly_fraction(base_row)
    print(f"\n  [Kelly] baseline WR={base_row['wr']:.1f}%  avgWin={base_row['avg_win']:.0f} "
          f"avgLoss={base_row['avg_loss']:.0f}  → full f*={kf:.3f}  half={khalf:.3f}")
    risk_rows = [
        ("RISK 0.02", evaluate(base, dict(risk_per_trade=0.02), cache)),
        ("RISK 0.03", evaluate(base, dict(risk_per_trade=0.03), cache)),
        ("RISK 0.05 (base)", base_row),
    ]
    if 0.005 < khalf < 0.10:
        risk_rows.append((f"RISK {khalf:.3f} (½Kelly)", evaluate(base, dict(risk_per_trade=round(khalf, 3)), cache)))
    show("5. 单笔风险 RISK_PER_TRADE (看是否被 40% 仓位/现金墙卡住)", base_row, risk_rows)
    print(f"  (gross-exposure peak base={base_row['gross']:.0f}%  cash-limited entries={base_row['cashlim']} "
          f"— 若 RISK 改了 $/day 不动,说明被仓位上限/现金墙绑死，RISK 不是有效杠杆)")

    # 6. MAX HOLD
    show("6. 最长持仓 MAX_HOLD_DAYS", base_row, [
        ("hold 5", evaluate(base, dict(max_hold_days=5), cache)),
        ("hold 7 (base)", base_row),
        ("hold 10", evaluate(base, dict(max_hold_days=10), cache)),
        ("hold 14", evaluate(base, dict(max_hold_days=14), cache)),
    ])

    # 7. COMBINED data-derived candidates
    show("7. 组合(数据推出的 best-of)", base_row, [
        ("baseline", base_row),
        ("SL3.0", evaluate(base, dict(sl_atr_mult=3.0), cache)),
        ("SL3.0 + ladder1.4/1.9", evaluate(base, dict(sl_atr_mult=3.0, use_scale_out=True,
                                                      tp1_r=1.4, tp2_r=1.9), cache)),
        ("SL3.0 + trail2.5/h14", evaluate(base, dict(sl_atr_mult=3.0, use_trailing_stop=True,
                                                     trail_atr_mult=2.5, trail_activate_r=1.0,
                                                     max_hold_days=14), cache)),
    ])

    hold_distribution(base_row["_trades"])
    return {"base": base_row, "kelly_half": khalf}


def run_tp(name: str, tickers: list[str], days: int) -> None:
    """Finer-grid TP confirmation — is the 8→10 win a plateau (robust) or a spike (overfit)?"""
    print(f"\n{'='*99}\n  TP CONFIRM · UNIVERSE: {name} ({len(tickers)} names) · {days}d · enforce_cash $5k\n"
          f"  baseline TP_ATR_MULT={settings.tp_atr_mult}; want a smooth plateau, not a single-point spike\n{'='*99}")
    base = live_base(days, tickers)
    print("  prefetching bars …")
    cache = prefetch_data(base)
    base_row = evaluate(base, {}, cache)
    rows = []
    for tp in [6, 7, 8, 9, 10, 11, 12, 14]:
        r = base_row if abs(tp - settings.tp_atr_mult) < 1e-6 else evaluate(base, dict(tp_atr_mult=float(tp)), cache)
        rows.append((f"TP {tp}" + (" (base)" if abs(tp - settings.tp_atr_mult) < 1e-6 else ""), r))
    show(f"TP sweep · {name}", base_row, rows)


def run_entry(name: str, tickers: list[str], days: int) -> None:
    """Entry-side levers: strategy mix (regime-routing proxy) + continuous score sizing."""
    print(f"\n{'='*99}\n  ENTRY-SIDE · UNIVERSE: {name} ({len(tickers)} names) · {days}d · enforce_cash $5k\n"
          f"  baseline = trend+momentum, take max(score), binary size at threshold 70\n{'='*99}")
    base = live_base(days, tickers)
    print("  prefetching bars …")
    cache = prefetch_data(base)
    base_row = evaluate(base, {}, cache)

    # A. strategy mix — "add the complementary arm" (MR) the text wants regime-routed in.
    show("A. 策略组合(regime 路由的代理:加 MR 臂)", base_row, [
        ("trend+mom (base)", base_row),
        ("+ MR always-on", evaluate(base, dict(apply_mr_strategy=True), cache)),
    ])

    # B. continuous score → size (replaces binary 70 gate).
    show("B. 连续仓位函数 size∝score (替二元 70 门槛)", base_row, [
        ("binary (base)", base_row),
        ("score 0.6→1.3", evaluate(base, dict(use_score_sizing=True, score_size_lo=0.6, score_size_hi=1.3), cache)),
        ("score 0.5→1.5", evaluate(base, dict(use_score_sizing=True, score_size_lo=0.5, score_size_hi=1.5), cache)),
        ("score 0.8→1.2", evaluate(base, dict(use_score_sizing=True, score_size_lo=0.8, score_size_hi=1.2), cache)),
    ])
    # score distribution at entry — shows how much headroom the function has
    import numpy as np
    scores = [t.get("score", 0) for t in base_row["_trades"]]
    if scores:
        a = np.asarray(scores)
        print(f"\n  入场分数分布: median={np.median(a):.0f}  P75={np.percentile(a,75):.0f}  "
              f"P90={np.percentile(a,90):.0f}  max={a.max():.0f}  (门槛=70 → 大多挤在低分端,"
              f"连续函数主要是「降marginal」而非「加高分」)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--pool", action="store_true", help="also run the full liquidity pool as a robustness check")
    ap.add_argument("--universe", choices=["watchlist", "pool", "both"], default="watchlist",
                    help="which universe(s) to run")
    ap.add_argument("--entry", action="store_true", help="run ONLY the entry-side levers (strategy mix + score sizing)")
    ap.add_argument("--tp", action="store_true", help="run ONLY the finer-grid TP confirmation")
    args = ap.parse_args()

    unis = {"watchlist": [("watchlist", WATCHLIST)], "pool": [("pool", POOL)],
            "both": [("watchlist", WATCHLIST), ("pool", POOL)]}[args.universe]
    if args.pool and args.universe == "watchlist":
        unis = unis + [("pool", POOL)]   # back-compat: --pool adds pool
    fn = run_tp if args.tp else (run_entry if args.entry else run_universe)
    for nm, tk in unis:
        if tk:
            fn(nm, tk, args.days)
    print("\nDONE — apply only the levers that win on $/day without worsening DD (ideally on both universes).")


if __name__ == "__main__":
    main()
