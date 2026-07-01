"""ML scorer — P1-2 (2026-06-26): XGBoost probability model trained on real fills.

Replaces the hand-written 6-factor weight table (indicators.py) with a model
that learns which factor combinations actually delivered profit.

ARCHITECTURE:
  • Trains on closed trades from SQLite (label: pnl>0 = win, else loss)
  • Features: the same factors the hand-written system computes
    (EMA cross, MACD, RSI, volume surge, ADX, pattern quality, regime)
  • Output: 0-1 probability of a winning trade
  • Blend: new ML score × ml_weight + hand-written score × (1-ml_weight)
    - ml_weight starts at 0.3 (30% ML), graduates to 0.8 after 100+ trades
    - This avoids the "cold-start ML" problem — the system still trades
      while the model warms up

WHEN TO TRAIN:
  • After every 30 new closed trades (or weekly, whichever comes first)
  • The model file (data/scorer_v1.json) is loaded at startup
  • If the file doesn't exist, the hand-written score is used unchanged
    (ml_weight = 0 for the first 30 trades)

REQUIRES: xgboost installed (pip install xgboost). Falls back gracefully
to hand-written scoring when xgboost is not available.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from . import db
from .config import settings

log = logging.getLogger(__name__)

MODEL_FILE = settings.root / "data" / "scorer_v1.json"

# Minimum trades before the model trains or activates.
MIN_TRADES_FOR_TRAIN = 20
# ML weight schedule: (min_trades, ml_weight)
WEIGHT_SCHEDULE = [
    (0,   0.0),    # no model → 100% hand-written
    (30,  0.30),   # 30+ trades → 70% hand-written, 30% ML
    (60,  0.50),
    (100, 0.70),
    (150, 0.85),
]

# Feature names (must match _extract_features order)
FEATURE_NAMES = [
    "trend_score", "momentum_score", "volume_score", "pattern_score",
    "adx_score", "vwap_score", "daily_trend_ok", "gap_ok",
    "regime_bull", "regime_bear", "vix", "vol_surge_ratio",
    "rsi", "atr_pct", "price_above_ema20",
]


def _current_ml_weight() -> float:
    """Return the current ML weight based on how many real trades we have."""
    n = _trade_count()
    weight = 0.0
    for min_n, w in WEIGHT_SCHEDULE:
        if n >= min_n:
            weight = w
    return weight


def _trade_count() -> int:
    try:
        rows = db.closed_trades(limit=10_000)
        return len(rows) if rows else 0
    except Exception:
        return 0


def model_exists() -> bool:
    return MODEL_FILE.exists()


def _load_model():
    """Load the trained XGBoost model. Returns None if unavailable."""
    try:
        import xgboost as xgb
        if not MODEL_FILE.exists():
            return None
        model = xgb.XGBClassifier()
        model.load_model(str(MODEL_FILE))
        return model
    except ImportError:
        log.debug("xgboost not installed — ML scorer disabled")
        return None
    except Exception as e:
        log.warning("Failed to load ML model: %s", e)
        return None


def _extract_features(signal, opendf, dailydf, regime_label="NEUTRAL",
                      vix=15.0) -> list[float]:
    """Extract the same features for both training and live scoring.

    signal:  indicators.Signal (has .breakdown dict with factor scores)
    opendf:  pd.DataFrame of intraday bars (e.g. 1H klines)
    dailydf: pd.DataFrame of daily bars (may be None)
    """
    bd = signal.breakdown or {}
    features = [
        float(bd.get("trend", 50)),
        float(bd.get("momentum", 50)),
        float(bd.get("volume", 50)),
        float(bd.get("pattern", 50)),
        float(bd.get("adx", 50)),
        float(bd.get("vwap", 50)),
    ]

    # Daily trend + gap signals (MTF context)
    daily_ok, gap_ok = 1.0, 1.0
    if dailydf is not None and len(dailydf) >= 55:
        try:
            import pandas_ta_classic as ta
            close = dailydf["close"]
            e20 = float(ta.ema(close, length=20).iloc[-1])
            e50 = float(ta.ema(close, length=50).iloc[-1])
            daily_ok = 1.0 if e20 > e50 else 0.0
            # Gap: today's open vs yesterday's close
            if len(dailydf) >= 2:
                prev_close = float(dailydf["close"].iloc[-2])
                curr_open = float(dailydf["open"].iloc[-1])
                gap_pct = abs(curr_open - prev_close) / prev_close if prev_close > 0 else 0
                gap_ok = 1.0 if gap_pct < 0.03 else 0.0
        except Exception:
            pass
    features.extend([daily_ok, gap_ok])

    # Regime
    regime_bull = 1.0 if regime_label == "BULL" else 0.0
    regime_bear = 1.0 if regime_label == "BEAR" else 0.0
    features.extend([regime_bull, regime_bear])

    # VIX and volume
    features.append(float(vix))
    if opendf is not None and len(opendf) >= 21:
        try:
            vol_ratio = (float(opendf["volume"].iloc[-1]) /
                         float(opendf["volume"].iloc[-21:-1].mean())
                         if opendf["volume"].iloc[-21:-1].mean() > 0 else 1.0)
        except Exception:
            vol_ratio = 1.0
    else:
        vol_ratio = 1.0
    features.append(vol_ratio)

    # RSI, ATR%, price vs EMA20
    rsi_val, atr_pct, price_above = 50.0, 1.0, 0.0
    if opendf is not None and len(opendf) >= 20:
        try:
            import pandas_ta_classic as ta
            r = ta.rsi(opendf["close"], length=14)
            rsi_val = float(r.iloc[-1]) if r is not None and not r.empty else 50.0
            a = ta.atr(opendf["high"], opendf["low"], opendf["close"], length=14)
            atr_val = float(a.iloc[-1]) if a is not None and not a.empty else 0.0
            price = float(opendf["close"].iloc[-1])
            atr_pct = (atr_val / price * 100) if price > 0 else 1.0
            e = ta.ema(opendf["close"], length=20)
            e20 = float(e.iloc[-1]) if e is not None and not e.empty else price
            price_above = 1.0 if price > e20 else 0.0
        except Exception:
            pass
    features.extend([rsi_val, atr_pct, price_above])

    return features


def train(force: bool = False) -> dict:
    """Train the XGBoost model on ALL closed trades. Returns training summary.

    force=True: retrain even if we don't have MIN_TRADES_FOR_TRAIN new trades
    since last train. Used by the weekly autopilot.
    """
    try:
        import xgboost as xgb
    except ImportError:
        return {"status": "skipped", "reason": "xgboost not installed"}

    rows = db.closed_trades(limit=10_000)
    if not rows:
        return {"status": "skipped", "reason": "no closed trades"}

    # Build training set — each row is a closed trade with PnL
    X, y = [], []
    skip_count = 0
    for r in rows:
        try:
            # Reconstruct features from the trade record's metadata
            # The features at entry time were stored in `extra` JSON
            extra = r.get("extra")
            if extra:
                if isinstance(extra, str):
                    extra = json.loads(extra)
                feats = extra.get("ml_features")
                if feats and len(feats) == len(FEATURE_NAMES):
                    X.append(feats)
                    y.append(1 if r.get("pnl", 0) > 0 else 0)
                    continue
            skip_count += 1
        except Exception:
            skip_count += 1

    if len(X) < MIN_TRADES_FOR_TRAIN:
        return {"status": "skipped",
                "reason": f"only {len(X)} trades with features (need {MIN_TRADES_FOR_TRAIN})",
                "n_total": len(rows), "n_labeled": len(X)}

    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.int32)

    # Count class balance
    n_wins = int(y_arr.sum())
    n_losses = len(y_arr) - n_wins
    scale_pos_weight = n_losses / max(n_wins, 1)

    model = xgb.XGBClassifier(
        max_depth=4,
        n_estimators=100,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        random_state=42,
    )
    model.fit(X_arr, y_arr)

    MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(MODEL_FILE))

    # Quick self-eval
    train_acc = float((model.predict(X_arr) == y_arr).mean())

    return {
        "status": "trained",
        "n_total": len(rows),
        "n_labeled": len(X),
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": round(n_wins / len(X) * 100, 1) if X else 0,
        "train_accuracy_pct": round(train_acc * 100, 1),
        "scale_pos_weight": round(scale_pos_weight, 2),
        "model_file": str(MODEL_FILE),
    }


def score(signal, opendf=None, dailydf=None,
          regime_label: str = "NEUTRAL", vix: float = 15.0) -> dict:
    """Score a live signal. Returns {ml_score: 0-100, ml_proba: 0-1, weight: 0-1,
    blended_score: 0-100}. When the model is unavailable, returns ml_score=50
    and weight=0 (fully hand-written).

    Caller takes signal.score and blends with ml_score using `weight`:
      blended = signal.score * (1 - weight) + ml_score * weight
    """
    model = _load_model()
    weight = _current_ml_weight() if model else 0.0

    if model is None or weight <= 0:
        return {"ml_score": 50.0, "ml_proba": 0.5, "weight": 0.0,
                "blended_score": signal.score}

    try:
        feats = _extract_features(signal, opendf, dailydf,
                                  regime_label=regime_label, vix=vix)
        proba = float(model.predict_proba([feats])[0][1])
        ml_score = proba * 100
        blended = signal.score * (1 - weight) + ml_score * weight
        return {
            "ml_score": round(ml_score, 1),
            "ml_proba": round(proba, 3),
            "weight": weight,
            "blended_score": round(blended, 1),
            "ml_features": feats,   # persisted in trade audit → ML can retrain
        }
    except Exception as e:
        log.debug("ML scorer failed: %s — falling back to hand-written", e)
        return {"ml_score": 50.0, "ml_proba": 0.5, "weight": 0.0,
                "blended_score": signal.score}
