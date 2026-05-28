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

from google import genai

from .config import settings, gemini_cascade
from .indicators import Signal
from . import news_fetcher

log = logging.getLogger(__name__)

_KEY_CYCLE = itertools.cycle(settings.gemini_keys) if settings.gemini_keys else None


def _next_key() -> str | None:
    return next(_KEY_CYCLE) if _KEY_CYCLE else None


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
    """Returns (pass, ai_sub_score_0_100, reason). On error, defaults to pass.

    Cascades through models from strongest to lightest (gemini_cascade()).
    On 429 / quota exhausted, moves to the next model automatically.
    """
    keys = list(settings.gemini_keys)
    if not keys:
        return True, 50, "no Gemini key configured — neutral"

    ticker_news = news_fetcher.format_news(
        news_fetcher.fetch_ticker_news(signal.symbol), "Ticker news"
    )
    macro_news = news_fetcher.format_news(
        news_fetcher.fetch_macro_news(), "Macro"
    )
    prompt = PROMPT.format(
        symbol=signal.symbol,
        price=signal.price,
        reasons="\n".join(f"- {r}" for r in signal.reasons),
        ticker_news=ticker_news,
        macro_news=macro_news,
    )

    import time
    for model_name in gemini_cascade():
        for key in keys:
            for attempt in range(2):
                try:
                    client = genai.Client(api_key=key)
                    resp = client.models.generate_content(model=model_name, contents=prompt)
                    text = resp.text.strip()
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    payload = json.loads(match.group(0) if match else text)
                    verdict = payload.get("verdict", "pass")
                    confidence = int(payload.get("confidence", 50))
                    reason = payload.get("reason", "")
                    is_pass = verdict == "pass"
                    sub_score = confidence if is_pass else 0
                    log.info("AI validator [%s] %s → %s (%d)",
                             model_name, signal.symbol, verdict, confidence)
                    return is_pass, sub_score, reason
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        log.info("AI validator: %s quota hit on %s → next key",
                                 model_name, signal.symbol)
                        break  # quota → try next key
                    if attempt == 0 and any(x in err for x in ("503", "500", "UNAVAILABLE")):
                        time.sleep(4)
                        continue
                    log.warning("AI validator: %s error on %s: %s", model_name, signal.symbol, e)
                    break
        log.info("AI validator: all keys quota'd on %s for %s → next model",
                 model_name, signal.symbol)

    log.warning("AI validator: all models exhausted for %s — defaulting to pass", signal.symbol)
    return True, 50, "AI quota exhausted across all models — neutral"
