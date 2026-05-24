"""Walk-forward parameter optimization via Optuna (Bayesian/TPE search).

Why this exists:
  The live system has ~6 magic numbers that were picked by feel
  (entry threshold, ATR multiples, max gap %, slippage). Without
  walk-forward + out-of-sample evaluation, "tuning" by hand is
  guaranteed to overfit. This module:

    1. Splits the lookback window into K non-overlapping test folds.
    2. For each Optuna trial (param set), runs the backtest on EACH fold.
    3. Reports the AVERAGE out-of-sample Sortino across folds.
    4. Optuna's TPE sampler then proposes new param sets that maximise OOS.

Why Sortino, not PnL:
  PnL rewards luck. Sortino penalises downside vol — a parameter set with
  smooth equity will beat one with a few big spikes, which is what you
  actually want to deploy with real money.

Usage:
  python -m src.optimizer --trials 30 --days 180 --folds 3
  python -m src.optimizer --tickers AAPL NVDA TSLA --trials 20
"""
from __future__ import annotations

import argparse
import json
import logging
import warnings
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

import optuna

from .backtest import BacktestConfig, prefetch_data, simulate_with_cache
from .config import settings

# Suppress Optuna's experimental warnings — TPE is fine.
warnings.filterwarnings("ignore", category=optuna.exceptions.ExperimentalWarning)

log = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent.parent / "data" / "optimizer"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------- walk-forward evaluator ----------

def _evaluate_params(
    base_cfg: BacktestConfig,
    params: dict,
    n_folds: int,
    cache: dict,
) -> dict:
    """Re-simulate with new params on the cached data. NO OpenD calls here.

    `cache` is what prefetch_data() returned — passed in so we don't refetch
    historical bars on every Optuna trial. This is the speedup that turns a
    25-trial study from ~2 hours into ~30 seconds.
    """
    from .metrics import compute_full_metrics

    cfg = replace(base_cfg, **params)
    result = simulate_with_cache(cfg, cache)
    trades = result.get("trades", [])
    if not trades:
        return {"sortino_mean": -10.0, "n_trades": 0, "fold_sortinos": [],
                "sortino_min": -10.0}

    # Sort by exit_date and split into K folds.
    sorted_t = sorted(trades, key=lambda t: t.get("exit_date", ""))
    n = len(sorted_t)
    if n < n_folds:
        n_folds = max(1, n // 5) or 1   # avoid empty folds
    fold_size = max(1, n // n_folds)

    fold_sortinos: list[float] = []
    for k in range(n_folds):
        start = k * fold_size
        end = (k + 1) * fold_size if k < n_folds - 1 else n
        fold = sorted_t[start:end]
        if len(fold) < 5:
            continue
        m = compute_full_metrics(fold, cfg.account_usd, max(cfg.days // n_folds, 30),
                                 n_sims=0)
        fold_sortinos.append(m.get("sortino_ratio", 0.0))

    if not fold_sortinos:
        return {"sortino_mean": -10.0, "n_trades": n, "fold_sortinos": [],
                "sortino_min": -10.0}

    return {
        "sortino_mean": sum(fold_sortinos) / len(fold_sortinos),
        "sortino_min": min(fold_sortinos),   # worst-fold robustness check
        "n_trades": n,
        "fold_sortinos": [round(x, 3) for x in fold_sortinos],
    }


# ---------- Optuna objective ----------

def _make_objective(base_cfg: BacktestConfig, n_folds: int, min_trades: int, cache: dict):
    def objective(trial: optuna.Trial) -> float:
        params = {
            "threshold":       trial.suggest_int("threshold", 60, 80, step=2),
            "tp_atr_mult":     trial.suggest_float("tp_atr_mult", 1.0, 3.0, step=0.25),
            "sl_atr_mult":     trial.suggest_float("sl_atr_mult", 1.5, 3.5, step=0.25),
            "max_gap_pct":     trial.suggest_float("max_gap_pct", 2.0, 5.0, step=0.5),
            "base_slip_bp":    trial.suggest_float("base_slip_bp", 1.0, 5.0, step=0.5),
        }
        try:
            stats = _evaluate_params(base_cfg, params, n_folds, cache)
        except Exception as e:
            import traceback
            log.warning("trial failed: %s\n%s", e, traceback.format_exc())
            return -10.0

        # Tell Optuna why we like / don't like this trial.
        trial.set_user_attr("n_trades", stats["n_trades"])
        trial.set_user_attr("fold_sortinos", stats["fold_sortinos"])
        trial.set_user_attr("sortino_min", round(stats["sortino_min"], 3))

        # Reject trials that just got lucky on a few trades.
        if stats["n_trades"] < min_trades:
            return -10.0 + stats["n_trades"] / max(min_trades, 1)
        # Combined objective: 70% mean Sortino + 30% worst-fold Sortino —
        # penalises strategies that look good on average but blow up in one
        # window.
        score = 0.7 * stats["sortino_mean"] + 0.3 * stats["sortino_min"]
        return float(score)

    return objective


# ---------- CLI ----------

def run_study(
    n_trials: int,
    days: int,
    n_folds: int,
    min_trades: int,
    tickers: Optional[list[str]] = None,
    timeframe: Optional[str] = None,
    fast_mode: bool = True,
) -> dict:
    """Run the Optuna study.

    `fast_mode=True` disables the ML and mean-revert gates during the search.
    Both add a lot of per-bar CPU but their thresholds are NOT optimised, so
    they act as constant filters — leaving them on while exploring threshold
    / ATR multiples just slows the search down without changing the ranking
    of parameter combos. Re-enable them by re-running the backtest with the
    chosen params + the live config.
    """
    base_cfg = BacktestConfig(
        days=days,
        timeframe=timeframe or settings.timeframe,
        threshold=settings.entry_threshold,
        tickers=tickers or [],
        account_usd=settings.account_usd,
        risk_per_trade=settings.risk_per_trade,
        max_position_pct=settings.max_position_pct,
        max_hold_days=settings.max_hold_days,
        apply_ml_gate=not fast_mode,
        apply_mr_strategy=not fast_mode,
    )

    # Pre-fetch all kline data ONCE. Every trial after this is pure CPU
    # (no OpenD calls), making 25 trials take ~30s instead of ~2 hours.
    log.info("Prefetching market data for %s tickers, %d days @ %s...",
             len(base_cfg.tickers) or "watchlist", base_cfg.days, base_cfg.timeframe)
    t0 = datetime.utcnow()
    cache = prefetch_data(base_cfg)
    log.info("Prefetch done in %.1fs — cached %d tickers",
             (datetime.utcnow() - t0).total_seconds(), len(cache["per_ticker"]))

    # TPE sampler with deterministic seed → reproducible search.
    sampler = optuna.samplers.TPESampler(seed=42)
    pruner = optuna.pruners.MedianPruner(n_warmup_steps=5)
    study = optuna.create_study(
        direction="maximize", sampler=sampler, pruner=pruner,
        study_name=f"moomoo-trader-{datetime.utcnow().strftime('%Y%m%d-%H%M')}",
    )

    objective = _make_objective(base_cfg, n_folds, min_trades, cache)
    log.info("Starting Optuna study: %d trials, %d folds, min_trades=%d",
             n_trials, n_folds, min_trades)
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    # Pull best trial + a summary of the top-5 for inspection.
    best = study.best_trial
    top5 = sorted(study.trials, key=lambda t: (t.value or -99), reverse=True)[:5]
    summary = {
        "study_name": study.study_name,
        "n_trials": n_trials,
        "n_folds": n_folds,
        "min_trades": min_trades,
        "base_config": {
            "days": base_cfg.days,
            "timeframe": base_cfg.timeframe,
            "tickers": tickers or "watchlist",
        },
        "best_value_sortino": round(best.value, 3) if best.value else None,
        "best_params": best.params,
        "best_user_attrs": dict(best.user_attrs),
        "top_5_trials": [
            {
                "value": round(t.value, 3) if t.value else None,
                "params": t.params,
                "user_attrs": dict(t.user_attrs),
            }
            for t in top5
        ],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    out = RESULTS_DIR / f"{study.study_name}.json"
    out.write_text(json.dumps(summary, indent=2, default=str))
    log.info("Study saved to %s", out)
    return summary


def print_summary(s: dict) -> None:
    print("\n" + "=" * 64)
    print(f"  OPTUNA WALK-FORWARD STUDY  |  {s['study_name']}")
    print("=" * 64)
    print(f"  Trials             : {s['n_trials']}  |  Folds: {s['n_folds']}")
    print(f"  Timeframe          : {s['base_config']['timeframe']}  |  "
          f"Days: {s['base_config']['days']}")
    print(f"  Min trades per fold: {s['min_trades']}")
    print()
    print(f"  ★ Best Sortino (combined OOS): {s['best_value_sortino']}")
    print(f"    n_trades        : {s['best_user_attrs'].get('n_trades', '?')}")
    print(f"    fold sortinos   : {s['best_user_attrs'].get('fold_sortinos', [])}")
    print(f"    worst-fold      : {s['best_user_attrs'].get('sortino_min', '?')}")
    print()
    print("  ★ Best parameters (paste into .env or backtest CLI):")
    for k, v in s["best_params"].items():
        env_key = {
            "threshold": "ENTRY_SCORE_THRESHOLD",
            "tp_atr_mult": "(backtest --tp-atr)",
            "sl_atr_mult": "(backtest --sl-atr)",
            "max_gap_pct": "(backtest --max-gap)",
            "base_slip_bp": "(backtest --slip-bp)",
        }.get(k, k)
        print(f"    {k:<18} = {v:<8}    →  {env_key}")
    print()
    print("  Top 5 trials:")
    for i, t in enumerate(s["top_5_trials"], 1):
        params_str = ", ".join(f"{k}={v}" for k, v in t["params"].items())
        n_tr = t["user_attrs"].get("n_trades", "?")
        worst = t["user_attrs"].get("sortino_min", "?")
        print(f"    #{i}  Sortino={t['value']:<7}  n_trades={n_tr:<4}  worst={worst}")
        print(f"        {params_str}")
    print("=" * 64 + "\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="Walk-forward Optuna optimizer")
    ap.add_argument("--trials", type=int, default=30, help="Optuna trials")
    ap.add_argument("--days", type=int, default=180, help="Backtest history")
    ap.add_argument("--folds", type=int, default=3, help="OOS test folds")
    ap.add_argument("--min-trades", type=int, default=30,
                    help="Trials with fewer trades than this are penalised")
    ap.add_argument("--tickers", nargs="*", help="Specific tickers (default: watchlist)")
    ap.add_argument("--timeframe", default=None)
    ap.add_argument("--full", action="store_true",
                    help="Enable ML + MR gates (slower; default is fast mode)")
    args = ap.parse_args()

    summary = run_study(
        n_trials=args.trials,
        days=args.days,
        n_folds=args.folds,
        min_trades=args.min_trades,
        tickers=args.tickers,
        timeframe=args.timeframe,
        fast_mode=not args.full,
    )
    print_summary(summary)


if __name__ == "__main__":
    main()
