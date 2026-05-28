"""Single-shot backtest with ML gate DISABLED, to test if the ML model
(now AUC ~0.51, basically noise) is helping or hurting the technical signal."""
from __future__ import annotations
import logging, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.backtest import BacktestConfig, run_backtest, print_report  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
from src.config import settings
cfg = BacktestConfig(
    days=180, timeframe="HOUR_1", threshold=70.0,
    tickers=[],
    account_usd=settings.account_usd,
    risk_per_trade=settings.risk_per_trade,
    max_position_pct=settings.max_position_pct,
    max_hold_days=settings.max_hold_days,
    tp_atr_mult=settings.tp_atr_mult,
    sl_atr_mult=settings.sl_atr_mult,
    max_gap_pct=settings.max_gap_pct,
    dd_size_cut_pct=settings.dd_size_cut_pct,
    dd_halt_pct=settings.dd_halt_pct,
    apply_ml_gate=False,   # ← key change
)
print("=== BACKTEST: ML GATE OFF, 180d, threshold=70 ===")
result = run_backtest(cfg)
print_report(result)
