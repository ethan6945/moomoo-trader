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


_vix_cache: dict = {"value": None, "ts": 0.0}
_sector_etf_cache: dict = {}   # {etf_symbol: (df, ts)}
_MACRO_TTL_SEC = 300   # refresh VIX + sector ETFs every 5 minutes


def _live_vix() -> float:
    """Get current VIX from MooMoo, cached for 5 min so we don't pound the
    snapshot endpoint each prediction. Falls back to 15.0 (neutral) on error
    so feature computation never blocks on macro fetch."""
    import time
    now = time.monotonic()
    if _vix_cache["value"] is not None and now - _vix_cache["ts"] < _MACRO_TTL_SEC:
        return _vix_cache["value"]
    try:
        from ..moomoo_client import MoomooClient
        c = MoomooClient()
        try:
            v = c.get_vix()
        finally:
            c.close()
        if v and v > 0:
            _vix_cache["value"] = v
            _vix_cache["ts"] = now
            return v
    except Exception as e:
        log.debug("live VIX fetch failed: %s — falling back to 15.0", e)
    return 15.0


def _live_sector_close(symbol: str, df_index) -> pd.Series | None:
    """Fetch sector ETF HOUR_1 close series for `symbol`'s sector, reindexed
    onto `df_index`. 5-minute cache so a full watchlist scan only fetches each
    sector ETF once. Returns None if sector unknown or fetch fails."""
    import time
    from ..sector import SECTOR_TO_ETF, get_sector
    etf = SECTOR_TO_ETF.get(get_sector(symbol))
    if not etf:
        return None
    now = time.monotonic()
    cached = _sector_etf_cache.get(etf)
    if cached and now - cached[1] < _MACRO_TTL_SEC:
        etf_df = cached[0]
    else:
        try:
            from ..moomoo_client import MoomooClient
            from moomoo import KLType
            c = MoomooClient()
            try:
                etf_df = c.get_kline(etf, bars=80, ktype=KLType.K_60M)
            finally:
                c.close()
            _sector_etf_cache[etf] = (etf_df, now)
        except Exception as e:
            log.debug("sector ETF %s fetch failed: %s", etf, e)
            return None
    if etf_df is None or etf_df.empty:
        return None
    try:
        return etf_df["close"].reindex(df_index, method="ffill").ffill().bfill()
    except Exception as e:
        log.debug("sector ETF reindex failed for %s: %s", etf, e)
        return None


def _live_insider_net(symbol: str) -> float:
    """Latest rolling 30-day insider log$ net for `symbol`. SEC EDGAR data is
    cached on-disk for 24h by `sec_edgar.fetch_ticker_insider` — so each
    ticker hits the SEC API at most once per day, and most live scans read
    from cache for free.
    """
    try:
        from ..sec_edgar import insider_rolling_30d_net
        ins_df = insider_rolling_30d_net(symbol, days=90, use_cache=True)
        if ins_df.empty:
            return 0.0
        return float(ins_df["insider_30d_net_log"].iloc[-1])
    except Exception as e:
        log.debug("live insider fetch %s failed: %s", symbol, e)
        return 0.0


def predict_proba(df: pd.DataFrame, symbol: str | None = None) -> float | None:
    """Run inference on the latest bar of `df`.  Returns None if model
    unavailable or features can't be computed yet.

    Injects three pieces of context into the df before computing features so
    the live pipeline mirrors training:
      • 'vix'                    ← live VIX snapshot (5-min cached)
      • 'sector_close'           ← sector ETF HOUR_1 close series (5-min cached)
      • 'insider_30d_net_log'    ← latest SEC EDGAR rolling 30d net (24h cached)

    `symbol` is needed for sector + insider lookups; older callers pass it as
    None and those features default to neutral (no crash).
    """
    model = _load()
    if model is None:
        return None
    df_aug = df.copy()
    if "vix" not in df_aug.columns:
        df_aug["vix"] = _live_vix()
    if "sector_close" not in df_aug.columns and symbol:
        sect_series = _live_sector_close(symbol, df_aug.index)
        df_aug["sector_close"] = sect_series if sect_series is not None else float("nan")
    if "insider_30d_net_log" not in df_aug.columns and symbol:
        df_aug["insider_30d_net_log"] = _live_insider_net(symbol)
    feats = compute_features(df_aug)
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
