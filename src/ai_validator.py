"""Gemini-based contextual check, augmented with real Tavily news.

The technical scorer can be fooled by:
  - Earnings announcements 24h away (volatility trap)
  - Sector-wide bad news the indicators haven't priced yet
  - Macro events (FOMC, CPI release)

We fetch live news (ticker-specific + macro) via Tavily, feed it to Gemini,
and parse a JSON verdict. A "veto" zeros the AI sub-score, a "pass" awards
the remaining 10 points.
"""
from __future__ import annotations

import itertools
import json
import logging
import re

import google.generativeai as genai

from .config import settings
from .indicators import Signal
from . import news_fetcher

log = logging.getLogger(__name__)

_KEY_CYCLE = itertools.cycle(settings.gemini_keys) if settings.gemini_keys else None


def _next_key() -> str | None:
    return next(_KEY_CYCLE) if _KEY_CYCLE else None


def _model():
    key = _next_key()
    if not key:
        return None
    genai.configure(api_key=key)
    return genai.GenerativeModel(settings.gemini_model)


PROMPT = """You are a risk gate for an automated short-term US-stock trading bot.

The technical scorer wants to BUY {symbol} at ${price}. Indicator breakdown:
{reasons}

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Using the news above, check ONLY context the indicators cannot see:
1. Is there an earnings release within the next 2 trading days?
2. Is there scheduled macro news (FOMC, CPI, jobs) that would dominate price?
3. Is the sector under fresh negative pressure (tariffs, regulation, downgrades)?
4. Is there a known overnight gap risk (lawsuits, guidance cut, analyst downgrade)?

Respond with STRICT JSON, no markdown fence:
{{"verdict": "pass" | "veto", "confidence": 0-100, "reason": "one sentence citing the specific news headline if any"}}

If you have no concerning info, default to "pass".
"""


def validate(signal: Signal) -> tuple[bool, int, str]:
    """Returns (pass, ai_sub_score_0_100, reason). On error, defaults to pass."""
    model = _model()
    if model is None:
        return True, 50, "no Gemini key configured — neutral"
    ticker_news = news_fetcher.format_news(
        news_fetcher.fetch_ticker_news(signal.symbol), "Ticker news"
    )
    macro_news = news_fetcher.format_news(
        news_fetcher.fetch_macro_news(), "Macro"
    )
    try:
        resp = model.generate_content(
            PROMPT.format(
                symbol=signal.symbol,
                price=signal.price,
                reasons="\n".join(f"- {r}" for r in signal.reasons),
                ticker_news=ticker_news,
                macro_news=macro_news,
            )
        )
        text = resp.text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        payload = json.loads(match.group(0) if match else text)
        verdict = payload.get("verdict", "pass")
        confidence = int(payload.get("confidence", 50))
        reason = payload.get("reason", "")
        is_pass = verdict == "pass"
        sub_score = confidence if is_pass else 0
        return is_pass, sub_score, reason
    except Exception as e:
        log.warning("AI validator failed (%s) — defaulting to pass", e)
        return True, 50, f"AI error: {e}"
