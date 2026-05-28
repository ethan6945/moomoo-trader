"""SEC EDGAR Form 4 (insider transactions) fetcher.

What this gives us:
  Historical insider buy/sell activity for any US-listed ticker, going back
  as far as SEC has records. Yahoo Finance's `insider_transactions` only
  returns the most recent ~50 rows; SEC EDGAR is the canonical source with
  full history.

How we use it:
  Build a per-day rolling 30-day "net dollar value of insider trades" for
  each ticker, then join into the HOUR_1 training set as a new ML feature
  (`insider_30d_net_usd`). Insider buying is one of the few signals with
  documented academic edge — corporate officers know things 30 days before
  the press release.

API basics:
  • Base host: data.sec.gov
  • SEC requires a User-Agent identifying the user (no API key)
  • Rate limit: 10 requests/second per IP
  • Filings list:  /submissions/CIK{cik}.json
  • Filing doc:    /Archives/edgar/data/{cik}/{accession}/{primary}.xml
  • CIK lookup:    https://www.sec.gov/files/company_tickers.json

Caches:
  data/sec_edgar/ciks.json              ticker → 10-digit CIK
  data/sec_edgar/filings/{ticker}.json  parsed transactions per ticker
"""
from __future__ import annotations

import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from .config import settings

log = logging.getLogger(__name__)

CACHE_DIR = settings.root / "data" / "sec_edgar"
CIK_CACHE = CACHE_DIR / "ciks.json"
FILING_CACHE_DIR = CACHE_DIR / "filings"

# SEC requires a contact identifier in the User-Agent. Generic but fine.
USER_AGENT = "moomoo-trader research junxian@local.dev"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",   # overridden per-request as needed
}

# Per-process rate limiter — SEC limits to 10 req/s per IP.
# Keep a comfortable 1.5× margin so concurrent parser/fetcher calls won't trip it.
_rate_lock = threading.Lock()
_last_request_at: float = 0.0
_MIN_INTERVAL_SEC = 0.15


def _rate_limit() -> None:
    global _last_request_at
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < _MIN_INTERVAL_SEC:
            time.sleep(_MIN_INTERVAL_SEC - elapsed)
        _last_request_at = time.monotonic()


def _get(url: str, host_override: Optional[str] = None,
         timeout: float = 12.0) -> requests.Response:
    """Rate-limited GET with required headers. Caller handles HTTPError."""
    _rate_limit()
    h = dict(HEADERS)
    if host_override:
        h["Host"] = host_override
    else:
        # Strip the Host override if the URL is on a different host.
        h.pop("Host", None)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


# ---------- CIK lookup ----------

def _load_cik_cache() -> dict[str, str]:
    if not CIK_CACHE.exists():
        return {}
    try:
        return json.loads(CIK_CACHE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_cik_cache(d: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CIK_CACHE.write_text(json.dumps(d, indent=2))


def get_cik(ticker: str) -> Optional[str]:
    """Return zero-padded 10-digit CIK for `ticker`, or None if unknown.

    First call fetches SEC's master ticker→CIK JSON (cached locally — file
    only changes when new tickers go public, refresh manually if needed).
    """
    ticker = ticker.upper()
    cache = _load_cik_cache()
    if ticker in cache:
        return cache[ticker]
    # Fetch the master list.
    try:
        r = _get("https://www.sec.gov/files/company_tickers.json")
        data = r.json()
    except Exception as e:
        log.warning("SEC ticker→CIK fetch failed: %s", e)
        return None
    new_cache: dict[str, str] = {}
    for entry in data.values():
        sym = entry.get("ticker", "").upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if sym and cik:
            new_cache[sym] = cik
    _save_cik_cache(new_cache)
    return new_cache.get(ticker)


# ---------- Form 4 filings list ----------

def fetch_form4_filings(cik: str, since: date) -> list[dict]:
    """Return Form 4 filings for `cik` filed on or after `since`.

    SEC's submissions endpoint returns the most-recent ~1000 filings. For
    most tickers that's > 2 years of Form 4s. For very active filers (large
    companies with many insiders) it might be < 2 years — we accept that
    limitation; the older edge of the training window will have NaN insider
    rather than synthetic data.

    Returns dicts: {"date", "accession", "doc_url"}.
    """
    if not cik:
        return []
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = _get(url, host_override="data.sec.gov")
        data = r.json()
    except Exception as e:
        log.warning("submissions fetch failed for CIK %s: %s", cik, e)
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    primaries = recent.get("primaryDocument", [])

    out: list[dict] = []
    int_cik = int(cik)
    for form, fd, an, pd_ in zip(forms, dates, accs, primaries):
        if form != "4":
            continue
        try:
            d = date.fromisoformat(fd)
        except ValueError:
            continue
        if d < since:
            continue
        an_clean = an.replace("-", "")
        # The `primaryDocument` field is the XSL-wrapped HTML view
        # (`xslF345X06/wk-form4_*.xml`). Strip the XSL prefix to get the raw
        # XML SEC stores alongside. Without this, we'd be parsing HTML and
        # finding zero `nonDerivativeTransaction` tags.
        raw_doc = pd_
        if "/" in pd_:
            raw_doc = pd_.split("/")[-1]   # drop the xslF345X06/ segment
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int_cik}/{an_clean}/{raw_doc}"
        out.append({"date": fd, "accession": an, "doc_url": doc_url})
    return out


# ---------- Form 4 XML parsing ----------

# Transaction codes worth tracking. We weight purchases (P) heaviest.
_BUY_CODES = {"P"}        # P = open-market purchase
_SELL_CODES = {"S"}       # S = open-market sale
# Other codes (A = award, M = option exercise, etc) we ignore — they're not
# the "insider with conviction" signal we want.


def parse_form4(url: str) -> list[dict]:
    """Return non-derivative transactions from a Form 4 primary XML doc.

    Each transaction: {date, code, shares, price, dollar, is_buy, is_sell}.
    Returns [] on parse error — caller treats missing data as zero net.
    """
    try:
        r = _get(url)
    except Exception as e:
        log.debug("Form 4 fetch failed %s: %s", url, e)
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log.debug("Form 4 XML parse failed %s: %s", url, e)
        return []

    transactions: list[dict] = []
    # Form 4 ns is sometimes default-namespaced; ElementTree handles by tag.
    for t in root.iter():
        # Trick: each `nonDerivativeTransaction` element contains the fields
        # we want. Find them by tag name regardless of namespace.
        if t.tag.endswith("nonDerivativeTransaction"):
            try:
                tdate = _find_text(t, "transactionDate", "value")
                tcode = _find_text(t, "transactionCoding", "transactionCode")
                tshares = float(_find_text(t, "transactionAmounts",
                                           "transactionShares", "value") or 0)
                tprice = float(_find_text(t, "transactionAmounts",
                                          "transactionPricePerShare", "value") or 0)
                if not tdate or not tcode or tshares <= 0:
                    continue
                transactions.append({
                    "date": tdate,
                    "code": tcode,
                    "shares": tshares,
                    "price": tprice,
                    "dollar": tshares * tprice,
                    "is_buy": tcode in _BUY_CODES,
                    "is_sell": tcode in _SELL_CODES,
                })
            except (AttributeError, ValueError, TypeError):
                continue
    return transactions


def _find_text(node, *tag_chain) -> Optional[str]:
    """Walk a chain of nested tag names, return innermost text. Namespace-tolerant."""
    cur = node
    for tag in tag_chain:
        found = None
        for child in cur.iter():
            if child.tag.endswith(tag):
                found = child
                break
        if found is None:
            return None
        cur = found
    return cur.text.strip() if cur.text else None


# ---------- Per-ticker insider history (cached) ----------

def _filing_cache_path(ticker: str) -> Path:
    return FILING_CACHE_DIR / f"{ticker.upper()}.json"


def fetch_ticker_insider(ticker: str, days: int = 730,
                         use_cache: bool = True) -> list[dict]:
    """All insider transactions for `ticker` in last `days`. Cached on disk.

    Cache TTL is 1 day — new Form 4s land throughout the day and we want them
    in the model the next training run. Manual cache wipe if needed.
    """
    cache_file = _filing_cache_path(ticker)
    if use_cache and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            fetched_at = data.get("fetched_at")
            if fetched_at:
                age_h = (datetime.utcnow()
                         - datetime.fromisoformat(fetched_at)).total_seconds() / 3600
                if age_h < 24:
                    return data.get("transactions", [])
        except (json.JSONDecodeError, ValueError):
            pass

    cik = get_cik(ticker)
    if not cik:
        log.info("No CIK for %s — skipping", ticker)
        return []
    since = date.today() - timedelta(days=days)
    filings = fetch_form4_filings(cik, since)
    log.info("SEC %s: %d Form 4 filings since %s", ticker, len(filings), since)

    transactions: list[dict] = []
    for f in filings:
        trans = parse_form4(f["doc_url"])
        for t in trans:
            t["filing_date"] = f["date"]
            transactions.append(t)

    FILING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({
        "ticker": ticker,
        "fetched_at": datetime.utcnow().isoformat(),
        "n_filings": len(filings),
        "n_transactions": len(transactions),
        "transactions": transactions,
    }, indent=2, default=str))
    return transactions


def insider_rolling_30d_net(ticker: str, days: int = 730,
                            use_cache: bool = True) -> pd.DataFrame:
    """Daily series of (rolling 30-day net insider $ value) for `ticker`.

    Net = sum of buys − sum of sales over the trailing 30 days. Positive = net
    buying (bullish historically); negative = net selling (less informative
    because executives sell for many non-bearish reasons — stock comp diversification).

    Returns DataFrame with DatetimeIndex and columns:
      • insider_30d_net_usd    (raw dollar value, can be very large)
      • insider_30d_net_log    (sign-preserving log: sign(x) × log1p(|x|))

    Empty DataFrame if no data.
    """
    trans = fetch_ticker_insider(ticker, days=days, use_cache=use_cache)
    if not trans:
        return pd.DataFrame()
    df = pd.DataFrame(trans)
    df["date"] = pd.to_datetime(df["date"])
    df["signed_dollar"] = df.apply(
        lambda r: r["dollar"] if r["is_buy"]
        else (-r["dollar"] if r["is_sell"] else 0.0),
        axis=1,
    )
    daily = (df.groupby(df["date"].dt.normalize())["signed_dollar"]
             .sum()
             .reset_index()
             .rename(columns={"date": "date"})
             .set_index("date")["signed_dollar"])

    # Fill all days in range with 0; then rolling 30-day sum.
    full_range = pd.date_range(daily.index.min(), pd.Timestamp.today(), freq="D")
    full = daily.reindex(full_range).fillna(0.0)
    rolling = full.rolling("30D").sum()
    import numpy as np
    out = pd.DataFrame({
        "insider_30d_net_usd": rolling,
        "insider_30d_net_log": np.sign(rolling) * np.log1p(np.abs(rolling)),
    })
    return out


# ---------- batch helper ----------

def build_insider_history(tickers: list[str], days: int = 730
                          ) -> dict[str, pd.DataFrame]:
    """Pull & cache insider history for many tickers. Returns {ticker: df}."""
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = insider_rolling_30d_net(t, days=days)
            if not df.empty:
                out[t] = df
                log.info("SEC %s: %d days of insider history", t, len(df))
        except Exception as e:
            log.warning("insider history failed for %s: %s", t, e)
    return out
