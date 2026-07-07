#!/usr/bin/env python3
"""
Hermes Grid Search — parallel multi-timeline backtest exploration.
Runs N backtest configurations simultaneously across CPU cores,
collects results, ranks by objective (net PnL / Sortino / custom).

Usage:
  python3 src/hermes_gridsearch.py                    → run default grid
  python3 src/hermes_gridsearch.py --grid wide         → wider exploration
  python3 src/hermes_gridsearch.py --grid aggressive   → aggressive bias
  python3 src/hermes_gridsearch.py --parallel 6        → 6 concurrent workers
  python3 src/hermes_gridsearch.py --top 5             → show top 5 results

Grid presets:
  default  = 3×3×3 = 27 combos (balanced)
  wide     = 5×5×3 = 75 combos (full exploration)
  focus    = 2×2×2 = 8 combos  (quick check around current)
  aggressive = 4×4×3 = 48 combos (TP/sizing biased wider)
"""

import json, os, subprocess, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from itertools import product
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
_PYTHON = str(ROOT / ".venv" / "bin" / "python3")


# ── Grid definitions ─────────────────────────────────────

GRIDS = {
    "default": {
        "sl_atr_mult":      [2.5, 2.8, 3.2],
        "tp_atr_mult":      [8.0, 10.0, 12.0],
        "entry_threshold":  [65, 70, 75],
    },
    "wide": {
        "sl_atr_mult":      [2.0, 2.5, 2.8, 3.2, 3.5],
        "tp_atr_mult":      [6.0, 8.0, 10.0, 12.0, 14.0],
        "entry_threshold":  [60, 70, 75],
    },
    "focus": {
        "sl_atr_mult":      [2.5, 2.8],
        "tp_atr_mult":      [8.0, 10.0],
        "entry_threshold":  [65, 70],
    },
    "aggressive": {
        "sl_atr_mult":      [2.5, 2.8, 3.2, 3.5],
        "tp_atr_mult":      [10.0, 12.0, 14.0, 16.0],
        "entry_threshold":  [60, 65, 70],
    },
}


# ── Worker (runs in subprocess) ──────────────────────────

def _run_one(config: dict) -> dict:
    """Run a single backtest with given params. Returns metrics + config."""
    code = (
        f"import sys, json; sys.path.insert(0, '{ROOT}'); "
        f"from src.config import settings; "
        f"from src.backtest import BacktestConfig, run_backtest; "
        f"cfg = BacktestConfig("
        f"days=180, "
        f"threshold={config['entry_threshold']}, "
        f"account_usd=5000.0, "
        f"risk_per_trade=0.05, "
        f"sl_atr_mult={config['sl_atr_mult']}, "
        f"tp_atr_mult={config['tp_atr_mult']}, "
        f"max_hold_days=7, "
        f"max_position_pct=0.40, "
        # ── LIVE-PARITY flags (must match main.py behaviour) ──
        f"apply_momentum_strategy=True, "   # live always scores trend + momentum
        f"apply_mr_strategy=settings.mr_enabled, "
        f"use_breakeven_stop=settings.use_breakeven_stop, "
        f"breakeven_trigger_r=settings.breakeven_trigger_r, "
        f"use_scale_out=settings.use_scale_out, "
        f"tp1_r=settings.tp1_r, tp2_r=settings.tp2_r, "
        f"max_gap_pct=settings.max_gap_pct, "
        f"apply_max_positions=True"
        f"); "
        f"result = run_backtest(cfg); "
        f"metrics = result.get('metrics', result); "
        f"metrics['_config'] = {json.dumps(config)}; "
        f"print(json.dumps(metrics, default=str))"
    )
    try:
        rc = subprocess.run(
            [_PYTHON, "-c", code],
            capture_output=True, text=True, timeout=600,
            cwd=str(ROOT),
        )
        if rc.returncode != 0:
            return {"error": rc.stderr[-300:], "config": config}
        # Parse last JSON line
        for line in reversed(rc.stdout.strip().split("\n")):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {"error": "no json output", "stdout": rc.stdout[-300:], "config": config}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "config": config}
    except Exception as e:
        return {"error": str(e), "config": config}


# ── Rank & score ─────────────────────────────────────────

def _score(result: dict) -> float:
    """Composite score: Sortino-weighted net PnL. Higher = better."""
    if "error" in result:
        return -999999
    pnl = result.get("net_pnl_usd", 0) or 0
    sortino = result.get("sortino_ratio", 0) or 0
    dd = result.get("max_drawdown_pct", 20) or 20
    wr = result.get("win_rate_pct", 0) or 0
    trades = result.get("total_trades", 1) or 1

    # Penalize very low trade counts (overfitted)
    trade_penalty = min(1.0, trades / 20.0)

    # PnL weighted by risk-adjusted return, scaled by trade count
    score = pnl * (1 + sortino / 10) * trade_penalty / (1 + dd / 100)
    return score


# ── Main ─────────────────────────────────────────────────

def gridsearch(grid_name: str = "default", parallel: int = 4, top_n: int = 10) -> dict:
    """Run grid search, return ranked results."""
    grid = GRIDS.get(grid_name, GRIDS["default"])
    keys = list(grid.keys())
    combos = [dict(zip(keys, vals)) for vals in product(*grid.values())]

    print(f"Grid: {grid_name} — {len(combos)} combinations")
    print(f"Workers: {parallel} (M3 has 8 cores, limit to 6 max)")
    print(f"Params: { {k: len(v) for k, v in grid.items()} }")
    print(f"{'='*60}")

    t0 = time.time()
    results = []
    completed = 0

    with ProcessPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(_run_one, c): c for c in combos}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            completed += 1
            cfg = r.get("_config", r.get("config", {}))
            pnl = r.get("net_pnl_usd", "?")
            err = r.get("error", "")
            status = f"ERR: {err[:40]}" if err else f"PnL=${pnl:,.0f}"
            eta = (time.time() - t0) / completed * (len(combos) - completed)
            print(f"  [{completed}/{len(combos)}] SL={cfg.get('sl_atr_mult','?')} "
                  f"TP={cfg.get('tp_atr_mult','?')} THR={cfg.get('entry_threshold','?')} "
                  f"→ {status} | ETA {eta:.0f}s")

    elapsed = time.time() - t0
    ranked = sorted([r for r in results if "error" not in r], key=_score, reverse=True)

    # Baseline: current config
    baseline = None
    for r in results:
        c = r.get("_config", r.get("config", {}))
        if c.get("sl_atr_mult") == 2.8 and c.get("tp_atr_mult") == 10.0 and c.get("entry_threshold") == 70:
            baseline = r
            break

    return {
        "grid": grid_name,
        "total": len(combos),
        "completed": completed,
        "errors": len(results) - len(ranked),
        "elapsed_sec": round(elapsed, 1),
        "top": [{
            "rank": i + 1,
            "config": r.get("_config", r.get("config")),
            "net_pnl_usd": r.get("net_pnl_usd"),
            "win_rate_pct": r.get("win_rate_pct"),
            "profit_factor": r.get("profit_factor"),
            "sortino_ratio": r.get("sortino_ratio"),
            "max_drawdown_pct": r.get("max_drawdown_pct"),
            "total_trades": r.get("total_trades"),
            "score": round(_score(r), 2),
        } for i, r in enumerate(ranked[:top_n])],
        "baseline": {
            "config": baseline.get("_config", baseline.get("config")) if baseline else None,
            "net_pnl_usd": baseline.get("net_pnl_usd") if baseline else None,
            "sortino_ratio": baseline.get("sortino_ratio") if baseline else None,
        } if baseline else None,
        "recommendation": _recommend(ranked[:top_n], baseline),
    }


def _recommend(ranked: list, baseline: Optional[dict]) -> dict:
    """Generate recommendation from top results."""
    if not ranked:
        return {"action": "no_valid_results", "reason": "all backtests failed"}
    best = ranked[0]
    bc = best.get("_config", best.get("config", {}))
    bp = best.get("net_pnl_usd", 0) or 0

    if baseline:
        base_pnl = baseline.get("net_pnl_usd", 0) or 0
        delta_pct = (bp - base_pnl) / abs(base_pnl) * 100 if base_pnl != 0 else 0
    else:
        delta_pct = 0

    return {
        "best_config": bc,
        "pnl_improvement_pct": round(delta_pct, 1),
        "action": "apply" if delta_pct > 2 else "review",
        "reason": (
            f"Top config improves PnL by {delta_pct:+.1f}% vs baseline. "
            f"Apply with: python3 src/hermes_improve.py apply "
            + " ".join(f"{k}={v}" for k, v in bc.items())
        ) if delta_pct > 2 else (
            f"Top config only improves {delta_pct:+.1f}% — below 2% threshold. "
            f"Review manually or broaden grid."
        ),
    }


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Hermes Grid Search")
    ap.add_argument("--grid", choices=list(GRIDS), default="default")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--output", type=str, default=None)
    args = ap.parse_args()

    result = gridsearch(args.grid, args.parallel, args.top)

    print(f"\n{'='*60}")
    print(f"Grid: {args.grid} | {result['total']} combos | {result['elapsed_sec']}s | {result['errors']} errors")
    print(f"\nTOP {min(args.top, len(result['top']))} RESULTS:")
    for r in result["top"]:
        c = r["config"]
        print(f"  #{r['rank']:2d}  SL={c['sl_atr_mult']:.1f}  TP={c['tp_atr_mult']:.1f}  "
              f"THR={c['entry_threshold']:.0f}  →  PnL=${r['net_pnl_usd']:,.0f}  "
              f"WR={r['win_rate_pct']:.0f}%  PF={r['profit_factor']:.2f}  "
              f"Sortino={r['sortino_ratio']:.1f}  DD={r['max_drawdown_pct']:.1f}%  "
              f"Score={r['score']:.0f}")

    rec = result["recommendation"]
    print(f"\nRECOMMENDATION: {rec['action'].upper()}")
    print(f"  {rec['reason']}")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nSaved to {args.output}")
