"""FinBERT-based sentiment scoring for news headlines.

Loads ProsusAI/finbert (HuggingFace) lazily on first call — a 440 MB download
that's cached locally after the first run. Returns a label (positive / neutral /
negative) plus a normalised "net sentiment" score in [-1, +1] that the signal
reporter can show on each premarket card.

Why not Gemini for this?
  • Gemini IS used (ai_validator) for veto decisions on the trading path.
  • FinBERT is small, fast, and runs entirely offline once cached — useful for
    the signal_reporter cards that fire every premarket without burning Gemini
    quota.
  • FinBERT is purpose-trained on financial text; for headline sentiment
    specifically it usually outperforms a general LLM at comparable size.

Live-only design: we DO NOT use FinBERT in features.py / ML training. Getting
clean historical news per ticker per HOUR_1 bar from a free source isn't
practical, so the training pipeline stays headline-free. Sentiment shows up
only in human-facing signal cards.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

_pipe = None
_pipe_lock = threading.Lock()
_LOADED_MODEL = "ProsusAI/finbert"


def _load():
    """Lazy-load FinBERT into a singleton pipeline. Returns None on error so
    callers can degrade gracefully — sentiment is a nice-to-have, never a hard
    dependency."""
    global _pipe
    if _pipe is not None:
        return _pipe
    with _pipe_lock:
        if _pipe is not None:
            return _pipe
        try:
            from transformers import (
                AutoTokenizer,
                AutoModelForSequenceClassification,
                pipeline,
            )
            tok = AutoTokenizer.from_pretrained(_LOADED_MODEL)
            mdl = AutoModelForSequenceClassification.from_pretrained(_LOADED_MODEL)
            _pipe = pipeline("sentiment-analysis", model=mdl, tokenizer=tok,
                             truncation=True, max_length=256, device=-1)  # CPU
            log.info("FinBERT loaded (%s) — CPU inference", _LOADED_MODEL)
        except Exception as e:
            log.warning("FinBERT load failed: %s — sentiment disabled", e)
            _pipe = None
    return _pipe


def score_headlines(headlines: list[str]) -> Optional[dict]:
    """Aggregate sentiment over a batch of headlines.

    Returns dict:
      label         "positive" | "neutral" | "negative" — majority vote
      score_net     [-1, +1]: (pos_share - neg_share)
      pos_share / neg_share / neu_share — fractions in [0, 1]
      n_headlines
      n_pos / n_neg / n_neu
    Returns None when FinBERT is unavailable.

    Empty input → "neutral" with 0 net score.
    """
    if not headlines:
        return {"label": "neutral", "score_net": 0.0,
                "pos_share": 0, "neg_share": 0, "neu_share": 0,
                "n_headlines": 0, "n_pos": 0, "n_neg": 0, "n_neu": 0}

    pipe = _load()
    if pipe is None:
        return None

    # Strip whitespace + dedupe — duplicates are common across news APIs.
    seen = set()
    cleaned: list[str] = []
    for h in headlines:
        h = (h or "").strip()
        if h and h not in seen:
            seen.add(h)
            cleaned.append(h)

    try:
        results = pipe(cleaned)
    except Exception as e:
        log.warning("FinBERT inference failed (%d headlines): %s", len(cleaned), e)
        return None

    n_pos = sum(1 for r in results if r["label"].lower() == "positive")
    n_neg = sum(1 for r in results if r["label"].lower() == "negative")
    n_neu = len(results) - n_pos - n_neg
    n = len(results) or 1
    pos_share = n_pos / n
    neg_share = n_neg / n
    neu_share = n_neu / n

    score_net = round(pos_share - neg_share, 3)
    if n_pos > n_neg and n_pos > n_neu:
        label = "positive"
    elif n_neg > n_pos and n_neg > n_neu:
        label = "negative"
    else:
        label = "neutral"

    return {
        "label": label,
        "score_net": score_net,
        "pos_share": round(pos_share, 3),
        "neg_share": round(neg_share, 3),
        "neu_share": round(neu_share, 3),
        "n_headlines": len(results),
        "n_pos": n_pos, "n_neg": n_neg, "n_neu": n_neu,
    }


def is_available() -> bool:
    """Quick liveness check used by callers to decide whether to bother
    gathering headlines. Doesn't trigger the model load by itself."""
    try:
        import transformers  # noqa: F401
        return True
    except ImportError:
        return False
