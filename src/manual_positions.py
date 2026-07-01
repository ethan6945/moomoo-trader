"""Adopt & supervise MANUAL positions — orders YOU placed yourself in the moomoo
app (or any broker position the bot never opened).

reconcile.py already pulls such a position ("orphan": broker has it, we don't)
into open_trades — and now flags it user_managed=True so NO auto-exit touches it
until this module has reviewed it. This module runs ONCE per adoption to make the
hand-off intelligent:

  • re-derive stop/TP from a FRESH 6-factor signal (a real ATR, not reconcile's
    bare 2%% proxy), anchored to YOUR actual cost,
  • judge entry risk (bear regime / already underwater / below the plan stop /
    weak technicals / imminent earnings),
  • Telegram you the takeover plan + an AI read, and
  • branch on risk:
      NORMAL → clear user_managed so the bot takes over the stops/TP automatically
               (incl. the 1-minute fast-stop tick), and notify you.
      HIGH   → keep user_managed=True and enqueue an APPROVAL. The bot cuts the
               loss for you ONLY after you tap ✅; tap ✖ and it stays yours to
               manage. Either way you get the bot's recommendation.

Wiring: scan_once() calls review_adopted() with the symbols reconcile just adopted
this scan, BEFORE the kill-switch can early-return — so a bear-regime adoption is
still reviewed and flagged.
"""
from __future__ import annotations

import logging

from . import approvals, earnings, executor, indicators, notifier, runtime_config, tg_approvals
from .config import settings

log = logging.getLogger(__name__)

# Unrealized R at/below this on first sight = already a real loser → HIGH risk.
_DEEP_UNDERWATER_R = -0.8
# 6-factor score below this while red = technically broken → HIGH risk.
_WEAK_SCORE = 35.0


def review_adopted(client, symbols, regime=None) -> list[dict]:
    """One-time intelligent review for each just-adopted manual symbol.

    `symbols` come from reconcile()'s ORPHAN_ADOPTED fixes. Safe with an empty
    list (no-op). Each symbol is isolated so one failure can't block the others."""
    out: list[dict] = []
    for sym in symbols or []:
        try:
            r = _review_one(client, sym, regime)
            if r:
                out.append(r)
        except Exception as e:
            log.exception("manual-position review %s failed: %s", sym, e)
    return out


def _ai_read(sig) -> str:
    """Best-effort one-line AI read for the card. Never raises; '' if no key."""
    from . import ai
    if not ai.has_key() or sig is None:
        return ""
    try:
        from . import ai_validator
        _ok, _score, reason = ai_validator.validate(sig)
        return (reason or "").strip()
    except Exception as e:
        log.debug("manual-position AI read failed: %s", e)
        return ""


def _review_one(client, symbol: str, regime=None) -> dict | None:
    trade = executor._load_open_trades().get(symbol)
    if not trade or trade.get("manual_reviewed"):
        return None
    entry = float(trade.get("entry_price") or 0)
    qty = int(trade.get("qty") or 0)
    if entry <= 0 or qty <= 0:
        return None

    # --- fresh 6-factor signal → real ATR + technical score + last price ---
    score: float | None = None
    atr = float(trade.get("atr") or 0) or max(entry * 0.02, 0.01)
    last = entry
    sig = None
    try:
        df = client.get_kline(symbol, bars=120)
        sig = indicators.evaluate(symbol, df)
        atr = float(sig.atr) or atr
        score = float(sig.score)
        last = float(sig.price) or entry
    except Exception as e:
        log.warning("manual %s: signal eval failed (%s) — ATR proxy", symbol, e)
        try:
            last = float(executor._last_price(client, symbol) or entry)
        except Exception:
            last = entry

    sl = round(entry - runtime_config.sl_atr_mult() * atr, 2)
    tp = round(entry + runtime_config.tp_atr_mult() * atr, 2)
    risk_per_share = max(entry - sl, 0.01)
    unrl_r = (last - entry) / risk_per_share

    # --- risk judgement ---
    reasons: list[str] = []
    if getattr(regime, "label", None) == "BEAR":
        reasons.append("大盘空头 (regime BEAR)")
    if last <= sl:
        reasons.append(f"现价 ${last:.2f} 已跌破计划止损 ${sl:.2f}")
    if unrl_r <= _DEEP_UNDERWATER_R:
        reasons.append(f"已浮亏 {unrl_r:.1f}R")
    if score is not None and score < _WEAK_SCORE and last < entry:
        reasons.append(f"技术面偏弱 (score {score:.0f}/100)")
    try:
        eb, enote = earnings.earnings_block(symbol)
        if eb:
            reasons.append(f"临近财报 ({enote})")
    except Exception:
        pass
    high_risk = bool(reasons)

    # --- persist the supervised plan (locked: serialise against the manage loop) ---
    with executor._TRADES_LOCK:
        trades = executor._load_open_trades()
        trade = trades.get(symbol)
        if not trade or trade.get("manual_reviewed"):
            return None
        trade["stop_loss"] = sl
        trade["take_profit"] = tp
        trade["atr"] = atr
        trade["init_risk_per_share"] = risk_per_share
        trade["strategy"] = "manual_adopt"
        trade["manual_adopted"] = True
        trade["manual_reviewed"] = True
        trade["pending_review"] = False
        trade["adopt_risk"] = "HIGH" if high_risk else "NORMAL"
        if score is not None:
            trade["adopt_score"] = round(score, 1)
        # NORMAL → release to the bot's auto-exits. HIGH → keep owner-only until
        # the takeover-sell is approved.
        trade["user_managed"] = bool(high_risk)
        trades[symbol] = trade
        executor._save_open_trades(trades)

    # --- compose + send ---
    ai = _ai_read(sig)
    score_txt = f"{score:.0f}/100" if score is not None else "n/a"
    plan = (f"持仓 {qty} 股 @ ${entry:.2f}（现价 ${last:.2f}, {unrl_r:+.1f}R）\n"
            f"我的计划: 止损 ${sl:.2f} · 止盈 ${tp:.2f} · 技术分 {score_txt}")
    if ai:
        plan += f"\nAI 解读: {ai}"

    if not high_risk:
        notifier.send(f"🖐 *检测到手动持仓 {symbol}*\n{plan}\n\n"
                      f"✅ 已纳入监控并接管止损/止盈（含 1 分钟快速止损）。")
        log.info("manual adopt NORMAL: %s @ %.2f sl=%.2f tp=%.2f", symbol, entry, sl, tp)
        return {"symbol": symbol, "risk": "NORMAL", "sl": sl, "tp": tp}

    # HIGH risk → approval-gated takeover-sell; bot leaves it alone until decided.
    why = "；".join(reasons)
    urgent = (last <= sl) or (unrl_r <= _DEEP_UNDERWATER_R)
    rec = ("建议立即止损卖出以控制风险。" if urgent
           else "建议尽快减仓/止损，或你自行盯盘。")
    detail = (f"🖐 *手动持仓 {symbol} — 风险偏高*\n{plan}\n\n"
              f"⚠ 风险点: {why}\n💡 {rec}\n\n"
              f"批准 = 我接管并立即市价止损卖出；拒绝 = 你自己盯盘（我不会动它）。")
    approvals.enqueue(
        kind="manual_takeover_sell",
        detail=detail,
        action=f"接管并立即市价止损卖出 {symbol}（{qty} 股）",
        payload={"symbol": symbol, "qty": qty, "last": last,
                 "reasons": reasons, "sl": sl, "tp": tp},
    )
    # Push the card to your phone now (don't wait for the ~5-min tg sync).
    try:
        tg_approvals.post_pending()
    except Exception as e:
        log.debug("manual takeover card post failed: %s", e)
    log.warning("manual adopt HIGH-RISK: %s — %s (approval queued)", symbol, why)
    return {"symbol": symbol, "risk": "HIGH", "reasons": reasons}
