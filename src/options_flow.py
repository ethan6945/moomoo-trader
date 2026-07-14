"""Phase 2D — unusual options-activity / flow signal (broker-style 期权异动).

Replicates the options block on the broker's analysis card: detect option VOLUME that
far exceeds OPEN INTEREST (a freshly-built position), put/call volume skew, and
strike-level OI concentration (acts as support/resistance). Intended to feed 2A
(smart exit) and 2B (sentiment) as one more factor.

⚠️ STATUS — BLOCKED / SCAFFOLD: the account currently has NO US options quote
permission, so the broker API denies get_option_chain / get_market_snapshot for
options ("No permission to get quotes ..."). Until the user subscribes to the broker
US options market data this CANNOT be validated. The module therefore:
  • is OFF by default (settings.options_flow_enabled) and NOT wired into any live
    path — once data is available, 2A/2B can call assess() and use the result;
  • degrades to {"signal":"neutral","score":0,...} on ANY permission/data/SDK/
    error condition and NEVER raises, so enabling it can't break the main loop.

Column names follow the broker SDK option chain (option_type / strike_price /
code) and snapshot (volume / option_open_interest); all access is defensive so a
future SDK shape change just yields neutral instead of an exception.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from .config import settings

log = logging.getLogger(__name__)

try:
    from moomoo import RET_OK
except Exception:                      # SDK shape guard (keeps import safe)
    RET_OK = 0


def _neutral(detail: str) -> dict:
    return {"signal": "neutral", "score": 0, "detail": detail}


def probe(symbol: str, client, timeout_s: float = 15.0) -> tuple[bool, str]:
    """Health probe (bypasses options_flow_enabled): can we actually fetch an
    option chain + snapshot with volume & open-interest? Returns (ok, detail).

    Bounded by a hard timeout in a worker thread so a hung/denied OpenD options
    call can never stall the caller (the health watchdog). Used by health_check
    and the manual verify step. Never raises.
    """
    import concurrent.futures

    def _do() -> tuple[bool, str]:
        code = client._format_code(symbol)
        q = client.quote
        ret, exp = q.get_option_expiration_date(code=code)
        if ret != RET_OK or exp is None or len(exp) == 0:
            return False, f"no options data ({str(exp)[:90]})"
        expiry = _nearest_expiry(exp, 60)
        if not expiry:
            col0 = exp.columns[0]
            expiry = str(exp[col0].iloc[0])[:10]
        ret, chain = q.get_option_chain(code=code, start=expiry, end=expiry)
        if ret != RET_OK or chain is None or len(chain) == 0:
            return False, f"no chain ({str(chain)[:90]})"
        codes = [c for c in chain["code"].tolist() if c][:5] if "code" in chain.columns else []
        if not codes:
            return False, "chain has no option codes"
        ret, snap = q.get_market_snapshot(codes)
        if ret != RET_OK or snap is None or len(snap) == 0:
            return False, f"no snapshot ({str(snap)[:90]})"
        if "volume" in snap.columns and "option_open_interest" in snap.columns:
            return True, f"ok — {len(chain)} contracts, volume+OI present (exp {expiry})"
        return False, f"snapshot missing volume/OI (cols {list(snap.columns)[:8]})"

    # shutdown(wait=False) so a wedged OpenD call can't block past the timeout
    # (a `with` block would wait for the stuck thread). See assess() note.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_do).result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return False, f"timeout — OpenD options 无响应 (>{timeout_s:.0f}s)"
    except Exception as e:
        return False, f"options error ({e})"
    finally:
        ex.shutdown(wait=False)


def _nearest_expiry(exp_df, lookahead_days: int) -> str | None:
    """First expiry within `lookahead_days` from today; None if none/parse-fail."""
    col = "strike_time" if "strike_time" in getattr(exp_df, "columns", []) else None
    if col is None:
        return None
    cutoff = date.today() + timedelta(days=lookahead_days)
    for v in exp_df[col].tolist():
        try:
            d = date.fromisoformat(str(v)[:10])
        except ValueError:
            continue
        if date.today() <= d <= cutoff:
            return str(v)[:10]
    return None


def assess(symbol: str, client, timeout_s: float = 20.0, **kwargs) -> dict:
    """Return {"signal": bullish|bearish|neutral, "score": 0-100 (50=neutral),
    "detail": str}. Gated by OPTIONS_FLOW_ENABLED. SELF-PROTECTING: the whole
    chain+snapshot fetch runs in a worker thread bounded by timeout_s, so a slow
    or wedged OpenD can NEVER stall the 5-min manage tick / scan loop that calls
    it — it degrades to neutral. Any error/permission/no-data → neutral. Never
    raises. Extra kwargs (lookahead_days, vol_oi_mult, strike_pct, max_contracts)
    pass through to the implementation."""
    if not settings.options_flow_enabled:
        return _neutral("disabled")
    import concurrent.futures
    # NOTE: do NOT use `with ThreadPoolExecutor()` — its __exit__ calls
    # shutdown(wait=True), which blocks until the (possibly wedged) worker
    # finishes and defeats the timeout. shutdown(wait=False) returns control now;
    # a stuck broker call leaks one daemon thread until it unblocks (rare — only a
    # wedged OpenD), which is acceptable vs. stalling the whole bot.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_assess_impl, symbol, client, **kwargs).result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return _neutral(f"timeout — OpenD options 无响应 (>{timeout_s:.0f}s)")
    except Exception as e:
        return _neutral(f"options error ({e})")
    finally:
        ex.shutdown(wait=False)


def _assess_impl(symbol: str, client, lookahead_days: int = 45,
                 vol_oi_mult: float = 3.0, strike_pct: float = 0.15,
                 max_contracts: int = 120) -> dict:
    """Core options-flow computation (run inside assess()'s timeout guard)."""
    try:
        code = client._format_code(symbol)
        q = client.quote

        ret, exp = q.get_option_expiration_date(code=code)
        if ret != RET_OK or exp is None or len(exp) == 0:
            return _neutral(f"no options permission/data ({str(exp)[:70]})")
        expiry = _nearest_expiry(exp, lookahead_days)
        if not expiry:
            return _neutral("no expiry within window")

        ret, chain = q.get_option_chain(code=code, start=expiry, end=expiry)
        if ret != RET_OK or chain is None or len(chain) == 0:
            return _neutral(f"no chain ({str(chain)[:70]})")
        if "code" not in chain.columns:
            return _neutral("chain missing code column")
        strike_col = "strike_price" if "strike_price" in chain.columns else None

        # Narrow to NEAR-THE-MONEY strikes so we snapshot tens, not hundreds, of
        # legs (a full chain can be 800+; ATM flow is what matters and the broker
        # caps list size / rate). Needs spot + a strike column.
        spot = 0.0
        try:
            kdf = client.get_kline(symbol, bars=2)
            if kdf is not None and len(kdf):
                spot = float(kdf["close"].iloc[-1])
        except Exception:
            spot = 0.0
        if spot > 0 and strike_col:
            lo, hi = spot * (1 - strike_pct), spot * (1 + strike_pct)
            chain = chain[(chain[strike_col].astype(float) >= lo)
                          & (chain[strike_col].astype(float) <= hi)]
        codes = [c for c in chain["code"].tolist() if c][:max_contracts]
        if not codes:
            return _neutral("no near-the-money contracts")

        # Snapshot the narrowed contracts (batched — broker caps list size).
        rows = []
        for i in range(0, len(codes), 200):
            ret, snap = q.get_market_snapshot(codes[i:i + 200])
            if ret == RET_OK and snap is not None and len(snap):
                rows.append(snap)
        if not rows:
            return _neutral("no option snapshot (permission?)")
        import pandas as pd
        snap = pd.concat(rows, ignore_index=True)
        oi_col = "option_open_interest" if "option_open_interest" in snap.columns else None
        if oi_col is None or "volume" not in snap.columns:
            return _neutral("snapshot missing volume/OI columns")
        # Resolve call/put type from the SNAPSHOT if it carries it (it usually
        # does); only merge from the chain for columns the snapshot lacks — a
        # blind merge collides option_type/strike_price into _x/_y suffixes.
        merged = snap
        need = [col for col in ("option_type", "strike_price")
                if col not in snap.columns and col in chain.columns]
        if need:
            merged = snap.merge(chain[["code"] + need], on="code", how="left")
        type_col = "option_type" if "option_type" in merged.columns else None
        if type_col is None:
            return _neutral("no option_type in snapshot or chain")

        def _is(t, kind):
            return str(t).upper().find(kind) >= 0
        call_vol = float(merged.loc[merged[type_col].apply(lambda t: _is(t, "CALL")), "volume"].fillna(0).sum())
        put_vol = float(merged.loc[merged[type_col].apply(lambda t: _is(t, "PUT")), "volume"].fillna(0).sum())

        # Unusual activity: any contract whose volume ≫ its open interest.
        merged["_vol"] = merged["volume"].fillna(0).astype(float)
        merged["_oi"] = merged[oi_col].fillna(0).astype(float)
        unusual = merged[(merged["_oi"] > 0) & (merged["_vol"] > vol_oi_mult * merged["_oi"])
                         & (merged["_vol"] >= 500)]
        unusual_calls = unusual[unusual[type_col].apply(lambda t: _is(t, "CALL"))]
        unusual_puts = unusual[unusual[type_col].apply(lambda t: _is(t, "PUT"))]

        pc_ratio = (put_vol / call_vol) if call_vol > 0 else (99.0 if put_vol > 0 else 1.0)
        # Score: start neutral, push by put/call skew and unusual flow direction.
        score = 50
        if pc_ratio >= 1.5:
            score -= 20
        elif pc_ratio <= 0.6:
            score += 20
        score += min(20, 5 * len(unusual_calls)) - min(20, 5 * len(unusual_puts))
        score = max(0, min(100, score))
        signal = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")
        detail = (f"P/C vol={pc_ratio:.2f}, unusual calls={len(unusual_calls)} "
                  f"puts={len(unusual_puts)} (exp {expiry})")
        return {"signal": signal, "score": int(score), "detail": detail}
    except Exception as e:
        return _neutral(f"options error ({e})")
