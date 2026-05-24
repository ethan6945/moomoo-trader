"""Tavily news search for the AI validator.

Two queries:
  • per-ticker news: "AAPL stock earnings news"
  • macro news: shared across the scan (Fed / CPI / tariff)

We cache results in-process for `CACHE_TTL_S` seconds so a single scan does
not hammer the API. Tavily free tier = 1000 req/month, which is plenty if we
respect the cache (one macro call per scan + one per top-candidate).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .config import settings

log = logging.getLogger(__name__)

CACHE_TTL_S = 60 * 30  # 30 min — news doesn't change every minute
_cache: dict[str, tuple[float, list[dict]]] = {}


def _tavily(query: str, days: int = 3, max_results: int = 5) -> list[dict]:
    if not settings.tavily_key:
        return []
    cache_key = f"{query}|{days}|{max_results}"
    now = time.time()
    if cache_key in _cache:
        ts, cached = _cache[cache_key]
        if now - ts < CACHE_TTL_S:
            return cached
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "days": days,
                "topic": "news",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("Tavily %s -> %s %s", query, resp.status_code, resp.text[:120])
            return []
        items = [
            {"title": i.get("title", "")[:100], "content": i.get("content", "")[:120]}
            for i in resp.json().get("results", [])
        ]
        _cache[cache_key] = (now, items)
        return items
    except Exception as e:
        log.warning("Tavily error for %r: %s", query, e)
        return []


def fetch_macro_news() -> list[dict]:
    """Shared across all tickers in a scan."""
    return _tavily(
        "US Federal Reserve interest rate CPI stock market tariff earnings season",
        days=3,
        max_results=3,
    )


def fetch_ticker_news(symbol: str) -> list[dict]:
    """Per-ticker, 3-day window."""
    return _tavily(f"{symbol} stock news earnings guidance", days=3, max_results=3)


def format_news(items: list[dict], header: str) -> str:
    """Compact format — titles + 1-line summary, capped per item."""
    if not items:
        return f"{header}: (none)"
    lines = [f"{header}:"]
    for it in items:
        title = it.get("title", "").strip()
        body = it.get("content", "").strip()
        if body:
            lines.append(f"- {title} | {body}")
        else:
            lines.append(f"- {title}")
    return "\n".join(lines)
