"""Inference layer — load the trained model once, predict per scan candidate.

Returns:
  • probability  ∈ [0, 1] of "TP-before-SL within horizon"
  • sub_score    ∈ [0, 100]  (proba × 100 minus a 50-centered offset)
  • verdict      "pass" / "veto" / "neutral" + reason

Blended into the final score by main.py.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ..config import settings
from .features import FEATURE_NAMES, compute_features

log = logging.getLogger(__name__)

ML_DIR = settings.root / "data" / "ml"
MODEL_FILE = ML_DIR / "model.joblib"
META_FILE = ML_DIR / "model_meta.json"

# Threshold below which we veto; above which we boost.
ML_VETO_THRESHOLD = 0.35
ML_BOOST_THRESHOLD = 0.55

_model = None
_meta: dict = {}
_model_lock = threading.Lock()


def is_available() -> bool:
    return MODEL_FILE.exists()


def model_meta() -> dict:
    global _meta
    if not _meta and META_FILE.exists():
        try:
            _meta = json.loads(META_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return _meta


def _load() -> object | None:
    """Lazy-load on first call.  Reloads if the file mtime changed (retrains)."""
    global _model, _meta
    if not MODEL_FILE.exists():
        return None
    with _model_lock:
        mtime = MODEL_FILE.stat().st_mtime
        if _model is None or getattr(_model, "_loaded_mtime", 0) != mtime:
            try:
                _model = joblib.load(MODEL_FILE)
                _model._loaded_mtime = mtime
                if META_FILE.exists():
                    _meta = json.loads(META_FILE.read_text())
                log.info("ML model loaded (trained %s)", _meta.get("trained_at", "?"))
            except Exception as e:
                log.error("failed to load ML model: %s", e)
                _model = None
        return _model


def predict_proba(df: pd.DataFrame) -> float | None:
    """Run inference on the latest bar of `df`.  Returns None if model
    unavailable or features can't be computed yet."""
    model = _load()
    if model is None:
        return None
    feats = compute_features(df)
    if feats.empty:
        return None
    last = feats.iloc[-1]
    if last.isna().any():
        return None
    X = last.to_frame().T[FEATURE_NAMES]
    try:
        proba = float(model.predict_proba(X)[0, 1])
        return proba
    except Exception as e:
        log.warning("predict failed: %s", e)
        return None


def evaluate(df: pd.DataFrame) -> tuple[str, float, str]:
    """Wrapper that returns (verdict, sub_score, reason) similar to ai_validator.

    verdict:
      • 'veto'    if proba < ML_VETO_THRESHOLD
      • 'pass'    if proba > ML_BOOST_THRESHOLD
      • 'neutral' otherwise

    sub_score:  proba × 100  (0-100)  for blending with rule score.
    """
    proba = predict_proba(df)
    if proba is None:
        return "neutral", 50.0, "ML model unavailable or insufficient data"
    score = round(proba * 100, 1)
    if proba < ML_VETO_THRESHOLD:
        return "veto", score, f"ML proba {proba:.2f} < {ML_VETO_THRESHOLD} (likely loser)"
    if proba > ML_BOOST_THRESHOLD:
        return "pass", score, f"ML proba {proba:.2f} > {ML_BOOST_THRESHOLD} (high-conviction)"
    return "neutral", score, f"ML proba {proba:.2f} (neutral zone)"
