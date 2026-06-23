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


GAP_PROMPT = """You are an EXIT risk gate for an automated US-stock trading bot.

The bot currently HOLDS {symbol}. We want to avoid carrying it into a likely
GAP-DOWN at the next session (an overnight gap fills past our stop-loss, so the
stop can't protect us — the only defense is to exit NOW, during regular hours,
while there is still liquidity).

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Judge ONLY concrete, already-PUBLIC catalysts that raise overnight gap-DOWN risk:
- a fresh analyst downgrade, price-target cut, or sell rating
- a lawsuit, SEC action, fraud/accounting probe, or guidance cut
- sector-wide bad news (tariffs, regulation, a peer's blow-up) hitting tonight
- a scheduled binding event tonight/tomorrow that reads clearly negative
Do NOT try to predict an earnings NUMBER — you cannot, and an unknown coin-flip
is NOT a reason to sell. Sell ONLY on concrete negative news above.

Respond with STRICT JSON, no markdown fence:
{{"action": "sell" | "hold", "confidence": 0-100, "reason": "one sentence citing the specific headline; if holding, say why"}}

If you have no concrete negative catalyst, default to "hold".
"""


def assess_gap_risk(symbol: str) -> tuple[bool, int, str]:
    """EXIT-side check for a HELD name: is there concrete public bad news raising
    overnight gap-DOWN risk? Returns (should_sell, confidence_0_100, reason).

    FAIL-SAFE: any missing key / quota / error / malformed reply → (False, 0, ...)
    i.e. HOLD. We never liquidate a real position on AI doubt — only on a clear,
    high-confidence 'sell' verdict citing concrete negative news.
    """
    keys = list(settings.gemini_keys)
    if not keys:
        return False, 0, "no Gemini key — sentinel AI layer inert (hold)"

    # Cost control: skip the Gemini call entirely when there is no fresh ticker
    # news — no news ⇒ no concrete catalyst ⇒ hold, for free.
    try:
        news_items = news_fetcher.fetch_ticker_news(symbol)
    except Exception as e:
        return False, 0, f"news fetch failed ({e}) — hold"
    if not news_items:
        return False, 0, "no fresh ticker news — hold (no AI call)"

    try:
        ticker_news = news_fetcher.format_news(news_items, "Ticker news")
        macro_news = news_fetcher.format_news(news_fetcher.fetch_macro_news(), "Macro")
    except Exception as e:
        return False, 0, f"news format failed ({e}) — hold"
    prompt = GAP_PROMPT.format(symbol=symbol, ticker_news=ticker_news, macro_news=macro_news)

    # Cost control: ONE fixed cheap model (not the strongest-first entry cascade).
    import time
    model_name = settings.gap_sentinel_model
    for key in keys:
        for attempt in range(2):
            try:
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(model=model_name, contents=prompt)
                text = resp.text.strip()
                match = re.search(r"\{.*\}", text, re.DOTALL)
                payload = json.loads(match.group(0) if match else text)
                action = str(payload.get("action", "hold")).lower()
                confidence = int(payload.get("confidence", 0))
                reason = payload.get("reason", "")
                should_sell = action == "sell"
                log.info("gap sentinel [%s] %s → %s (%d)", model_name, symbol, action, confidence)
                return should_sell, confidence, reason
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    break  # quota on this key → try next key
                if attempt == 0 and any(x in err for x in ("503", "500", "UNAVAILABLE")):
                    time.sleep(4)
                    continue
                log.warning("gap sentinel: %s error on %s: %s", model_name, symbol, e)
                break
    # Exhausted → FAIL-SAFE hold (never sell on AI unavailability).
    log.warning("gap sentinel: AI unavailable for %s — holding (fail-safe)", symbol)
    return False, 0, "AI unavailable — hold (fail-safe)"


EXIT_PROMPT = """You are an EXIT risk gate for an automated, LONG-ONLY US-stock
trading bot. The bot HOLDS {symbol} (entry ${entry}, now ${last}, unrealized
{r_now:+.1f}R). It wants to decide whether to EXIT NOW (during regular hours)
instead of waiting for the price target — to LOCK IN a gain that is about to
reverse, or to cut before a known bearish catalyst plays out.

Current technical picture (computed, not opinion):
{tech_summary}

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Decide SELL only on a CONCRETE, already-PUBLIC bearish development the indicators
can't fully price, e.g.:
- a fresh analyst downgrade, price-target CUT, or sell rating
- bearish research, a guidance cut, lawsuit, SEC action, or accounting probe
- sector-wide bad news (a peer's blow-up, tariffs, regulation) hitting this name
- a clear distribution/reversal signature confirmed by the technicals above
Do NOT sell on vague unease, normal volatility, or to "take profit early" without
a real catalyst. If the position is in profit AND a concrete catalyst threatens
it, SELL to bank the gain. If nothing concrete, HOLD.

Respond with STRICT JSON, no markdown fence:
{{"action": "sell" | "hold", "confidence": 0-100, "reason": "one sentence citing the specific catalyst/technical; if holding, say why"}}

If you have no concrete reason, default to "hold".
"""


def assess_exit(symbol: str, tech_summary: str, entry: float = 0.0,
                last: float = 0.0, r_now: float = 0.0) -> tuple[bool, int, str]:
    """Phase 2A smart-exit AI judge for a HELD long. Broader than assess_gap_risk:
    weighs concrete bearish news + the supplied technical state to decide whether
    to exit now (lock profit / cut on catalyst). Returns (should_sell, conf, reason).

    Same machinery + FAIL-SAFE policy as assess_gap_risk: no key / no fresh news /
    quota / error → (False, 0, ...) i.e. HOLD. We never liquidate on AI doubt.
    Uses the dedicated smart_exit_model (≥3.5-flash), one fixed model, key-cycled.
    """
    keys = list(settings.gemini_keys)
    if not keys:
        return False, 0, "no Gemini key — smart-exit AI inert (hold)"

    # Cost control: no fresh ticker news ⇒ no concrete catalyst ⇒ hold, for free.
    try:
        news_items = news_fetcher.fetch_ticker_news(symbol)
    except Exception as e:
        return False, 0, f"news fetch failed ({e}) — hold"
    if not news_items:
        return False, 0, "no fresh ticker news — hold (no AI call)"

    try:
        ticker_news = news_fetcher.format_news(news_items, "Ticker news")
        macro_news = news_fetcher.format_news(news_fetcher.fetch_macro_news(), "Macro")
    except Exception as e:
        return False, 0, f"news format failed ({e}) — hold"
    prompt = EXIT_PROMPT.format(symbol=symbol, entry=entry, last=last, r_now=r_now,
                                tech_summary=tech_summary,
                                ticker_news=ticker_news, macro_news=macro_news)

    import time
    model_name = settings.smart_exit_model
    for key in keys:
        for attempt in range(2):
            try:
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(model=model_name, contents=prompt)
                text = resp.text.strip()
                match = re.search(r"\{.*\}", text, re.DOTALL)
                payload = json.loads(match.group(0) if match else text)
                action = str(payload.get("action", "hold")).lower()
                confidence = int(payload.get("confidence", 0))
                reason = payload.get("reason", "")
                should_sell = action == "sell"
                log.info("smart-exit [%s] %s → %s (%d)", model_name, symbol, action, confidence)
                return should_sell, confidence, reason
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    break  # quota on this key → next key
                if attempt == 0 and any(x in err for x in ("503", "500", "UNAVAILABLE")):
                    time.sleep(4)
                    continue
                log.warning("smart-exit: %s error on %s: %s", model_name, symbol, e)
                break
    log.warning("smart-exit: AI unavailable for %s — holding (fail-safe)", symbol)
    return False, 0, "AI unavailable — hold (fail-safe)"


SENTIMENT_PROMPT = """You are a multi-factor analyst for a LONG-ONLY US-stock
trading bot — produce a moomoo-style 看好/中性/看空 read on BUY candidate {symbol}
(now ${price}).

Technical signals the bot already computed:
{reasons}

Recent news (last 3 days):
{ticker_news}

Macro / market context:
{macro_news}

Options flow (期权异动 — empty if unavailable):
{options_flow}

Weigh the factors and give an overall 0-100 BULLISHNESS score (50 = neutral,
>50 = bullish, <50 = bearish):
- news: fresh catalysts, partnerships, guidance
- analyst_target: direction of analyst rating / price-target changes
- technical: do the computed signals above confirm or contradict a long?
- options: does the options positioning (puts vs calls, unusual volume) lean
  bullish or bearish?

Respond with STRICT JSON, no markdown fence:
{{"verdict": "bullish" | "neutral" | "bearish", "score": 0-100,
  "factors": {{"news": 0-100, "analyst_target": 0-100, "technical": 0-100}},
  "reason": "one short sentence"}}

If you have no strong view, return neutral with score ~50.
"""


def assess_sentiment(signal, options_summary: str = "") -> tuple[str, int, str]:
    """Phase 2B moomoo-style sentiment read for a BUY candidate. Returns
    (verdict, score_0_100, reason). ADVISORY — never vetoes. FAIL-SAFE → neutral
    ('neutral', 50, ...) on no key / quota / error. Uses sentiment_model (≥3.5).
    options_summary (optional) injects the options-flow read as a 4th factor."""
    keys = list(settings.gemini_keys)
    if not keys:
        return "neutral", 50, "no Gemini key — sentiment inert (neutral)"

    try:
        ticker_news = news_fetcher.format_news(
            news_fetcher.fetch_ticker_news(signal.symbol), "Ticker news")
        macro_news = news_fetcher.format_news(news_fetcher.fetch_macro_news(), "Macro")
    except Exception as e:
        return "neutral", 50, f"news fetch failed ({e}) — neutral"
    prompt = SENTIMENT_PROMPT.format(
        symbol=signal.symbol, price=signal.price,
        reasons="\n".join(f"- {r}" for r in signal.reasons),
        ticker_news=ticker_news, macro_news=macro_news,
        options_flow=options_summary or "(none)")

    import time
    model_name = settings.sentiment_model
    for key in keys:
        for attempt in range(2):
            try:
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(model=model_name, contents=prompt)
                text = resp.text.strip()
                match = re.search(r"\{.*\}", text, re.DOTALL)
                payload = json.loads(match.group(0) if match else text)
                verdict = str(payload.get("verdict", "neutral")).lower()
                score = int(payload.get("score", 50))
                reason = payload.get("reason", "")
                log.info("sentiment [%s] %s → %s (%d)", model_name, signal.symbol, verdict, score)
                return verdict, score, reason
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    break
                if attempt == 0 and any(x in err for x in ("503", "500", "UNAVAILABLE")):
                    time.sleep(4)
                    continue
                log.warning("sentiment: %s error on %s: %s", model_name, signal.symbol, e)
                break
    log.warning("sentiment: AI unavailable for %s — neutral (fail-safe)", signal.symbol)
    return "neutral", 50, "AI unavailable — neutral (fail-safe)"
