"""adversary.py — the RED TEAM for moomoo-trader.

Premise: every "practice" the live bot believes in is GUILTY until the data
proves it innocent. This script does two adversarial jobs:

  LAYER A — "earn your keep" ablation.
    Take each thing the live system has switched ON (each entry gate, each
    sizing rule, each exit overlay) and REMOVE it. A practice survives only if
    deleting it costs real $/day or worsens drawdown. If the system does just
    as well (or better) without it, the practice is DEAD WEIGHT — complexity
    and live-vs-backtest divergence risk with no measured payoff.

  LAYER B — "is the edge even real" credibility attack.
    The headline backtest (≈$20/day, Sortino 28, +75%) is fit on ONE bull
    window of semis. Attack it: stress real-world costs 2-3×, walk the window,
    and test whether the 70-score selection is doing anything. A real edge
    survives; a curve-fit one folds.

Parity: reuses optimize_levers' live_base/evaluate, which mirror
`main._run_live_engine` EXACTLY (momentum+trend, VIX sizing, regime ×mult,
earnings gate, MTF/gap/regime gates, breakeven, real commissions, enforce_cash
$5k). So every number here is apples-to-apples with what the bot actually
trades on. One lever changes per row.

Run:
  .venv/bin/python3 scripts/adversary.py                 # Layer A+B, watchlist
  .venv/bin/python3 scripts/adversary.py --pool          # + pool robustness (Layer A)
  .venv/bin/python3 scripts/adversary.py --windows       # + window-stability walk (Layer B)
  .venv/bin/python3 scripts/adversary.py --all           # everything
  .venv/bin/python3 scripts/adversary.py --json out.json  # also dump machine-readable verdicts
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Reuse the EXACT live-parity harness (importing runs only module-level setup,
# main() is __name__-guarded). live_base() already reflects current .env:
# TP=10 SL=3.5 RISK=0.05 MAXPOS%=0.40 MAXHOLD=7 newnames=2 regime_mult=1.4.
from scripts.optimize_levers import (  # noqa: E402
    WATCHLIST, POOL, live_base, evaluate, kelly_fraction,
)
from src.backtest import prefetch_data  # noqa: E402
from src.config import settings          # noqa: E402

# Iron rule thresholds. A practice EARNS ITS KEEP only if removing it drops
# $/day by more than KEEP_DELTA OR worsens MTM-DD by more than DD_TOL.
KEEP_DELTA = 1.5    # $/day considered material (vs ~$20/day baseline)
DD_TOL = 0.5        # percentage points of MTM drawdown considered material


def p(*a):  # flushing print
    print(*a, flush=True)


# ───────────────────────── verdict logic ─────────────────────────
def classify(base: dict, off: dict) -> tuple[str, str]:
    """Compare baseline (practice ON) vs ablated (practice removed/flipped).

    Returns (verdict_tag, emoji). Convention for an ablation that REMOVES a
    practice: cost_daily = base.$ - off.$ (what you lose by removing); a
    POSITIVE cost_daily means the practice was helping. dd_relief = off.DD -
    base.DD (how much DD grows when removed); POSITIVE means the practice was
    suppressing drawdown.
    """
    cost_daily = base["daily"] - off["daily"]
    dd_relief = off["ddm"] - base["ddm"]

    helps_pnl = cost_daily > KEEP_DELTA
    helps_dd = dd_relief > DD_TOL
    hurts_pnl = cost_daily < -KEEP_DELTA      # removing it ADDS money
    hurts_dd = dd_relief < -DD_TOL            # removing it LOWERS DD

    if helps_pnl or helps_dd:
        return "KEEP", "🟢"          # removing it clearly costs you → it earns its keep
    if hurts_pnl or hurts_dd:
        return "HARMFUL", "🔴"       # removing it makes you MORE money / less risk → cut it
    return "DEAD", "⚪"              # neutral both ways → dead weight, simplify away


VERDICT_NOTE = {
    "KEEP": "保留 — 拿掉它会掉钱/加回撤",
    "HARMFUL": "有害 — 拿掉反而更赚/更稳，建议关掉",
    "DEAD": "死重量 — 拿掉无差别，纯复杂度，建议简化",
}

ROW = "{emoji} {label:<26}{trd:>4}{wr:>6.1f}{pf:>6.2f}{daily:>+8.1f}{ddm:>8.2f}{ddl:>+9.1f}{dddd:>+8.2f}  {tag}"
HDR = "  {h:<26}{t:>4}{w:>6}{p:>6}{d:>8}{dd:>8}{dl:>9}{ddc:>8}".format(
    h="practice (ablated→OFF)", t="trd", w="WR%", p="PF", d="$/day", dd="mtmDD%",
    dl="Δ$/day", ddc="ΔDD")


def show_ablation(title: str, base: dict, rows: list[tuple[str, dict]], collect: list) -> None:
    p(f"\n── {title} ──")
    p(HDR)
    p(f"  {'BASELINE (all live ON)':<26}{base['trd']:>4}{base['wr']:>6.1f}{base['pf']:>6.2f}"
      f"{base['daily']:>+8.1f}{base['ddm']:>8.2f}{'—':>9}{'—':>8}")
    p("  " + "-" * 95)
    for label, off in rows:
        tag, emoji = classify(base, off)
        ddl = off["daily"] - base["daily"]      # raw Δ$/day from removing (what the OFF run shows)
        dddd = off["ddm"] - base["ddm"]
        p(ROW.format(emoji=emoji, label=label, trd=off["trd"], wr=off["wr"], pf=off["pf"],
                     daily=off["daily"], ddm=off["ddm"], ddl=ddl, dddd=dddd,
                     tag=VERDICT_NOTE[tag]))
        collect.append({"section": title, "practice": label, "verdict": tag,
                        "base_daily": round(base["daily"], 2), "off_daily": round(off["daily"], 2),
                        "d_daily": round(ddl, 2), "base_dd": round(base["ddm"], 2),
                        "off_dd": round(off["ddm"], 2), "d_dd": round(dddd, 2)})


# ───────────────────────── LAYER A ─────────────────────────
def layer_a(name: str, tickers: list[str], days: int, collect: list) -> dict:
    p(f"\n{'='*97}")
    p(f"  LAYER A · 消融对抗 · {name} ({len(tickers)} names) · {days}d {settings.timeframe} · enforce_cash $5k")
    p(f"  基底=当前实盘(.env): TP={settings.tp_atr_mult} SL={settings.sl_atr_mult} "
      f"RISK={settings.risk_per_trade} MAXPOS%={settings.max_position_pct} "
      f"MAXHOLD={settings.max_hold_days} 新名/扫描={settings.max_new_names_per_scan} 大盘×{settings.regime_bull_mult}")
    p(f"  规则: 拿掉一项做法后 $/day 掉>${KEEP_DELTA} 或 DD 恶化>{DD_TOL}pp ⇒ 🟢值得留；"
      f"拿掉反而更赚/更稳 ⇒ 🔴有害；都无差别 ⇒ ⚪死重量")
    p(f"{'='*97}")

    base = live_base(days, tickers)
    p("  prefetching bars …")
    cache = prefetch_data(base)
    if not cache.get("per_ticker"):
        raise RuntimeError("0 tickers — OpenD down?")
    base_row = evaluate(base, {}, cache)

    # 1) ENTRY GATES — each claims to filter out losing trades. Remove one at a time.
    show_ablation("1. 入场过滤门 (逐一移除)", base_row, [
        ("MTF 多周期门 off", evaluate(base, dict(apply_mtf_gate=False), cache)),
        ("Gap 跳空门 off", evaluate(base, dict(apply_gap_gate=False), cache)),
        ("Regime 大盘门 off", evaluate(base, dict(apply_regime_gate=False), cache)),
        ("Earnings 财报门 off", evaluate(base, dict(apply_earnings_gate=False), cache)),
        ("★全部门一起 off", evaluate(base, dict(apply_mtf_gate=False, apply_gap_gate=False,
                                              apply_regime_gate=False, apply_earnings_gate=False), cache)),
    ], collect)

    # 2) SIZING / RISK overlays.
    show_ablation("2. 仓位 / 风控叠加层 (逐一移除)", base_row, [
        ("VIX 缩仓 off", evaluate(base, dict(apply_vix_sizing=False), cache)),
        (f"大盘×{settings.regime_bull_mult}加仓 → ×1.0", evaluate(base, dict(regime_bull_mult=1.0), cache)),
        ("DD 熔断 off", evaluate(base, dict(apply_dd_breaker=False), cache)),
        ("集中度限制 off (新名不限)", evaluate(base, dict(max_new_names_per_scan=0), cache)),
    ], collect)

    # 3) EXIT overlays.
    be_trg = base.breakeven_trigger_r
    show_ablation("3. 出场叠加层", base_row, [
        ("Breakeven 保本止损 off", evaluate(base, dict(use_breakeven_stop=False), cache)),
        ("Scale-out 分批 ON(.env声称开)", evaluate(base, dict(use_scale_out=True, tp1_r=settings.tp1_r,
                                                          tp2_r=settings.tp2_r), cache)),
    ], collect)
    p(f"  ↑ Scale-out: .env 写 USE_SCALE_OUT=true / TP1_R={settings.tp1_r}，但 TP1=={settings.tp1_r}×{settings.sl_atr_mult}"
      f"={settings.tp1_r*settings.sl_atr_mult:.1f} ATR ≥ TP {settings.tp_atr_mult} ATR → 永不触发。"
      f"若上面这行与基底逐字相同，证明它是**幽灵开关**(配置/README 宣称有，实盘等于没有)。")

    return {"base": base_row, "cache": cache, "base_cfg": base}


# ───────────────────────── LAYER B ─────────────────────────
def layer_b(name: str, tickers: list[str], days: int, ctx: dict, do_windows: bool, collect: list) -> None:
    base_cfg, cache, base_row = ctx["base_cfg"], ctx["cache"], ctx["base"]
    p(f"\n{'='*97}")
    p(f"  LAYER B · 可信度对抗 · {name} · 「这个 edge 是真的还是过拟合一段牛市?」")
    p(f"{'='*97}")

    # B1 — COST STRESS. Real fills are worse than the model. Multiply slippage +
    # commission 2× and 3×; a real edge survives, a fragile one folds.
    def cost_mult(k: float) -> dict:
        return dict(base_slip_bp=2.0 * k, atr_slip_k=0.5 * k,
                    commission_pct_per_order=0.0003 * k,
                    platform_fee_per_order=0.99 * k, sell_regulatory_bps=0.5 * k)
    p("\n── B1. 成本压力 (滑点+佣金 ×N) — 真实成交永远比模型差 ──")
    p(HDR)
    p(f"  {'1× (base 模型成本)':<26}{base_row['trd']:>4}{base_row['wr']:>6.1f}{base_row['pf']:>6.2f}"
      f"{base_row['daily']:>+8.1f}{base_row['ddm']:>8.2f}")
    for k in (2.0, 3.0):
        r = evaluate(base_cfg, cost_mult(k), cache)
        ddl = r["daily"] - base_row["daily"]
        surv = "🟢 仍盈利" if r["daily"] > 0 else "🔴 翻负"
        p(ROW.format(emoji="  ", label=f"{k:.0f}× 成本", trd=r["trd"], wr=r["wr"], pf=r["pf"],
                     daily=r["daily"], ddm=r["ddm"], ddl=ddl, dddd=r["ddm"]-base_row["ddm"], tag=surv))
        collect.append({"section": "B1 cost-stress", "practice": f"{k:.0f}x cost",
                        "off_daily": round(r["daily"], 2), "d_daily": round(ddl, 2)})

    # B2 — SELECTION NULL TEST. The 70-score gate is the core "alpha". Sweep the
    # threshold: if dropping it to ~everything keeps $/day/trade, the score isn't
    # selecting winners — it's just a trade-count throttle.
    p("\n── B2. 选股门槛是不是真在选 alpha (门槛 50/60/70/80) ──")
    p(f"  {'threshold':<14}{'trd':>4}{'WR%':>7}{'PF':>6}{'$/day':>8}{'$/trade':>9}{'mtmDD%':>8}")
    for th in (50.0, 60.0, 70.0, 80.0):
        r = base_row if abs(th - base_cfg.threshold) < 1e-6 else evaluate(base_cfg, dict(threshold=th), cache)
        per = (r["net"] / r["trd"]) if r["trd"] else 0.0
        mark = "  ← 实盘门槛" if abs(th - 70.0) < 1e-6 else ""
        p(f"  {th:<14.0f}{r['trd']:>4}{r['wr']:>7.1f}{r['pf']:>6.2f}{r['daily']:>+8.1f}{per:>+9.1f}{r['ddm']:>8.2f}{mark}")
        collect.append({"section": "B2 selection", "practice": f"thr {th:.0f}",
                        "trd": r["trd"], "off_daily": round(r["daily"], 2),
                        "per_trade": round(per, 2)})
    p("  解读: 门槛升高若 WR/$每笔/PF 单调改善 → 评分真在排序赢家;若 $/每笔 不随门槛升高 → 评分只是个交易数节流阀,没有真排序力。")

    # B3 — WINDOW STABILITY (optional; needs fresh prefetch per window).
    if do_windows:
        p("\n── B3. 时间窗稳定性 (近 N 天) — edge 是稳定的还是只活在某一段? ──")
        p("  (HOUR_1 上限 ~150d，故 60/90/120/最长 是「最近 N 天」的递推，不是独立窗)")
        p(f"  {'window':<14}{'trd':>4}{'WR%':>7}{'PF':>6}{'$/day':>8}{'mtmDD%':>8}{'净%':>8}")
        for d in (60, 90, 120, days):
            wcfg = live_base(d, tickers)
            wcache = prefetch_data(wcfg)
            r = evaluate(wcfg, {}, wcache)
            net_pct = r["_metrics"].get("total_return_pct", 0)
            p(f"  last {d:<9}{r['trd']:>4}{r['wr']:>7.1f}{r['pf']:>6.2f}{r['daily']:>+8.1f}{r['ddm']:>8.2f}{net_pct:>+8.1f}")
            collect.append({"section": "B3 window", "practice": f"last {d}d",
                            "off_daily": round(r["daily"], 2), "dd": round(r["ddm"], 2)})

    # B4 — sanity: how implausible is the headline risk-adjusted return?
    m = base_row["_metrics"]
    kf, khalf = kelly_fraction(base_row)
    p("\n── B4. 体检: 这些指标真实吗? ──")
    p(f"  Sortino={m.get('sortino_ratio',0):.1f}  Sharpe={m.get('sharpe_ratio',0):.1f}  PF={m.get('profit_factor',0):.2f}  "
      f"WR={m.get('win_rate_pct',0):.0f}%  full-Kelly f*={kf:.2f}")
    p("  现实参照: 顶级量化基金常年 Sharpe ~1-2、Sortino ~2-3。Sharpe>8 / Sortino>20 几乎肯定是")
    p("  「单段牛市 + 单时间窗 + 样本内」放大出来的，不是可持续期望。把它当**上限**而非预期，实盘按 ~1/3 打折看。")


# ───────────────────────── main ─────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--pool", action="store_true", help="also run Layer A on the full liquidity pool")
    ap.add_argument("--windows", action="store_true", help="run the Layer-B window-stability walk")
    ap.add_argument("--all", action="store_true", help="--pool + --windows")
    ap.add_argument("--json", type=str, default="", help="dump machine-readable verdicts to this path")
    args = ap.parse_args()
    if args.all:
        args.pool = args.windows = True

    collect: list = []
    unis = [("watchlist", WATCHLIST)] + ([("pool", POOL)] if args.pool and POOL else [])
    for nm, tk in unis:
        ctx = layer_a(nm, tk, args.days, collect)
        # Layer B only on the primary (watchlist) to keep it focused.
        if nm == "watchlist":
            layer_b(nm, tk, args.days, ctx, args.windows, collect)

    # roll-up
    a_rows = [c for c in collect if c.get("verdict")]
    keep = [c for c in a_rows if c["verdict"] == "KEEP"]
    dead = [c for c in a_rows if c["verdict"] == "DEAD"]
    harm = [c for c in a_rows if c["verdict"] == "HARMFUL"]
    p(f"\n{'='*97}\n  裁决汇总 (Layer A)\n{'='*97}")
    p(f"  🟢 值得留 ({len(keep)}): " + ", ".join(sorted({c['practice'] for c in keep})))
    p(f"  ⚪ 死重量 ({len(dead)}): " + ", ".join(sorted({c['practice'] for c in dead})))
    p(f"  🔴 有害   ({len(harm)}): " + ", ".join(sorted({c['practice'] for c in harm})))
    p("\n  注: 单一时间窗 + 单宇宙 的裁决只有方向性。--pool / --windows 用第二宇宙与递推窗做稳健性确认;")
    p("  只有在 watchlist 与 pool 上裁决一致的项，才足以据此动手关/留。")

    if args.json:
        Path(args.json).write_text(json.dumps(collect, indent=1, ensure_ascii=False))
        p(f"\n  → verdicts dumped to {args.json}")


if __name__ == "__main__":
    main()
