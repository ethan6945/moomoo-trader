"""Entry point. Two modes:

    python -m src.main scan      # one-shot scan + trade decisions
    python -m src.main run       # APScheduler loop during US market hours
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler

from moomoo import KLType

from . import (
    adaptive_sizing, ai_validator, approvals, audit, blacklist, clock,
    cron_state, db, executor, history, indicators, kill_switch, notifier,
    portfolio, regime as regime_mod, risk_manager, runtime_config, self_improve,
    self_review, strategy_momentum, strategy_mr, tg_approvals,
)
from .config import settings
from .earnings import earnings_block
from .moomoo_client import client
from .reconcile import log_reconcile, reconcile

SPREAD_MAX_PCT = 0.5   # refuse entry if bid-ask spread > 0.5% of mid
# Frames where the last K-line is still forming when we fetch — drop it
# before scoring to eliminate look-ahead bias.
_INTRADAY_TFS = {"HOUR_1", "MIN_10", "MIN_30"}


def _drop_forming_bar(df, timeframe: str):
    """Look-ahead audit: strip the last (currently-forming) intraday bar.

    MooMoo's `request_history_kline` returns bars up to and including the bar
    that contains "now" — i.e. the last row of an intraday df is OPEN, not
    closed. Scoring on an open bar means the close, volume, and indicators
    all keep moving after we evaluate, so the live signal won't match the
    backtest's closed-bar signal. We always drop the last intraday row.

    Daily bars are left intact — the daily scan path runs once after close.
    """
    if timeframe.upper() in _INTRADAY_TFS and len(df) > 1:
        return df.iloc[:-1]
    return df

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(settings.root / "logs" / "trader.log"),
    ],
)
log = logging.getLogger("main")

WATCHLIST_FILE = settings.root / "config" / "watchlist.json"
NY = pytz.timezone("America/New_York")


def load_watchlist() -> list[str]:
    return json.loads(WATCHLIST_FILE.read_text())["tickers"]


def _refresh_account_snapshot(c, vix: float | None = None,
                              regime: "regime_mod.Regime | None" = None,
                              full: bool = True) -> None:
    """Write data/account.json — used by the GUI for status + heartbeat.

    Called from BOTH the full trade-phase scan AND the outside-trade-phase
    branch so the GUI heartbeat stays current at every scan interval, not
    just during 09:45–15:30 ET.

    When `full=False` we skip VIX + regime fetches (slow + only matter for
    entries that won't happen anyway).
    """
    try:
        cash = c.get_account_cash()
        positions = c.get_positions()
    except Exception as e:
        log.warning("snapshot fetch failed: %s", e)
        return

    if vix is None:
        if full:
            try:
                vix = c.get_vix()
            except Exception:
                vix = 0.0
        else:
            # Carry over last-known VIX from previous snapshot (don't blank it).
            try:
                prev = json.loads((settings.root / "data" / "account.json").read_text())
                vix = float(prev.get("vix") or 0)
            except Exception:
                vix = 0.0

    if regime is None:
        if full:
            try:
                spy_df = c.get_kline("SPY", bars=250, ktype=KLType.K_DAY)
                regime = regime_mod.assess(spy_df)
            except Exception:
                regime = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "not assessed")
        else:
            try:
                prev = json.loads((settings.root / "data" / "account.json").read_text())
                regime = regime_mod.Regime(
                    prev.get("regime", "NEUTRAL"), 0, 0, 0, False, False,
                    prev.get("regime_note", "carried over from last full scan"),
                )
            except Exception:
                regime = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "unknown")

    try:
        real_held = positions[positions["qty"].astype(float) > 0] if not positions.empty else positions
        invested = 0.0
        unrealized = 0.0
        symbols: list[str] = []
        # Per-position live snapshot — the GUI overlays this onto the open_trades
        # table so each row shows current price + unrealized P&L without making
        # an extra API call from the GUI thread.
        per_pos: dict[str, dict] = {}
        if not real_held.empty:
            invested = float((real_held["qty"].astype(float) * real_held["cost_price"].astype(float)).sum())
            unrealized = float(real_held.get("pl_val", 0).astype(float).sum()) if "pl_val" in real_held.columns else 0.0
            symbols = [code.split(".")[-1] for code in real_held["code"].tolist()]
            for _, row in real_held.iterrows():
                sym = row["code"].split(".")[-1]
                per_pos[sym] = {
                    "qty": int(float(row.get("qty", 0) or 0)),
                    "last": float(row.get("nominal_price") or 0)
                            or float(row.get("market_val", 0)) / max(float(row.get("qty", 1)), 1),
                    "pl_val": float(row.get("pl_val", 0) or 0),
                    "pl_ratio": float(row.get("pl_ratio", 0) or 0),
                }
        state = risk_manager._load_state()
        realized_total = float(state.get("realized_pnl_total", 0.0))
        snap = {
            "ts": clock.ny_now().isoformat(),
            "cash": cash,
            "positions_count": len(real_held),
            "invested": invested,
            "budget": risk_manager.budget_usd(),
            "budget_used_pct": round(invested / risk_manager.budget_usd() * 100, 1) if risk_manager.budget_usd() else 0,
            "unrealized_pnl": unrealized,
            "realized_pnl_total": realized_total,
            "total_pnl": unrealized + realized_total,
            "symbols": symbols,
            "ai_model": settings.gemini_model,
            "scan_interval_min": settings.scan_interval_min,
            "entry_threshold": settings.entry_threshold,
            "max_hold_days": settings.max_hold_days,
            "timeframe": settings.timeframe,
            "trade_env": settings.moomoo_trade_env,
            "vix": round(vix or 0, 1),
            "regime": regime.label,
            "regime_note": regime.note,
            "open_risk": round(portfolio.current_open_risk(), 2),
            "heat_cap": round(risk_manager.budget_usd() * portfolio.PORTFOLIO_HEAT_PCT, 2),
            "trade_stats": portfolio.trade_stats(50),
            "skip_gates": audit.gate_summary(200),
            "last_scan_utc": clock.utc_now_corrected().isoformat(),
            "phase": "trade" if full else "manage_only",
            "clock": clock.status(),
            "per_position": per_pos,
            "realized_pnl_today": float(state.get("realized_pnl_today", 0.0)),
            "budget_usd": risk_manager.budget_usd(),
            "pending_approvals": approvals.list_pending(),
        }
        (settings.root / "data" / "account.json").write_text(
            json.dumps(snap, indent=2, default=str)
        )
        if full:
            history.append({
                "invested": round(invested, 2),
                "budget": risk_manager.budget_usd(),
                "unrealized_pnl": round(unrealized, 2),
                "realized_pnl_total": round(realized_total, 2),
                "total_pnl": round(unrealized + realized_total, 2),
                "positions_count": len(real_held),
                "symbols": symbols,
                "timeframe": settings.timeframe,
            })
    except Exception as e:
        log.warning("snapshot write failed: %s", e)


def scan_once() -> None:
    log.info("=== scan start ===")
    audit.record("scan_start")
    tickers = load_watchlist()

    # Pre-kill-switch fast path: if we're outside trade phase, do management
    # work + snapshot refresh, then exit before any candidate logic runs.
    # The full kill_switch.evaluate() happens later (after we have cash + regime).
    if not kill_switch.in_trade_phase():
        log.info("Outside trade phase — managing only, no new entries")
        try:
            with client() as c:
                executor.manage_open_trades(c)
                _refresh_account_snapshot(c, full=False)
        except Exception as e:
            log.exception("manage during off-phase failed: %s", e)
        audit.record("scan_end", gate="trade_phase",
                     reason="outside trade phase — manage only")
        return

    with client() as c:
        # 1. Manage existing positions first
        try:
            actions = executor.manage_open_trades(c)
            for a in actions:
                notifier.send(notifier.trade_action_msg(a))
        except Exception as e:
            log.exception("manage_open_trades failed: %s", e)

        # 2. Refresh account state and VIX, then persist snapshot for the GUI.
        try:
            cash = c.get_account_cash()
            positions = c.get_positions()
            pending_value = c.get_pending_buy_value()
            pending_symbols = c.get_pending_symbols()
        except Exception as e:
            log.exception("account fetch failed: %s", e)
            return

        # Dynamic capital: feed live equity (cash + open-position market value)
        # to the risk manager so sizing/budget/DD derive from real account state,
        # capped by the owner's allocated budget. No restart when budget changes.
        try:
            pos_mv = 0.0
            if positions is not None and not positions.empty:
                if "market_val" in positions.columns:
                    pos_mv = float(positions["market_val"].astype(float).sum())
                elif "qty" in positions.columns:
                    px = (positions["nominal_price"] if "nominal_price" in positions.columns
                          else positions.get("cost_price", 0))
                    pos_mv = float((positions["qty"].astype(float) * px.astype(float)).sum())
            risk_manager.set_live_equity(cash + pos_mv)
        except Exception as e:
            log.debug("set_live_equity failed (sizing falls back to budget): %s", e)

        # Apply any owner-APPROVED suggestions (analyze→notify→approve→execute).
        # Nothing here runs until the owner approved it in the GUI / via CLI —
        # the feedback 铁律 choke point. Confirm what was applied.
        try:
            applied = approvals.apply_approved()
            for a in applied:
                notifier.send(f"✅ 已执行你批准的建议: {a.get('action', a.get('detail',''))}")
        except Exception as e:
            log.debug("apply_approved skipped: %s", e)

        # 2b. Reconcile broker vs internal records — catch drift before trading.
        try:
            recon = reconcile(positions)
            msg = log_reconcile(recon)
            if msg:
                notifier.send(msg)
        except Exception as e:
            log.warning("reconcile failed: %s", e)

        # 3. Fetch VIX + classify market regime (SPY 200MA).
        try:
            vix = c.get_vix()
            log.info("VIX=%.1f%s", vix,
                     " ⚠ HIGH — sizes halved" if 25 < vix <= 35 else
                     " ⚠ CRISIS — sizes quartered" if vix > 35 else "")
        except Exception as e:
            log.warning("VIX fetch failed: %s", e)
            vix = 15.0

        # Market regime via SPY
        regime = regime_mod.Regime("NEUTRAL", 0, 0, 0, False, False, "not assessed")
        try:
            spy_df = c.get_kline("SPY", bars=250, ktype=KLType.K_DAY)
            regime = regime_mod.assess(spy_df)
            log.info("Regime: %s — %s", regime.label, regime.note)
        except Exception as e:
            log.warning("regime assessment failed: %s", e)

        # Daily rollover — clear stale halt / realized_pnl_today on day boundary.
        try:
            kill_switch.reset_for_new_day(cash)
        except Exception as e:
            log.warning("reset_for_new_day failed: %s", e)

        # Unified kill switch — replaces three separate checks (trade_phase /
        # regime block / halt / drawdown). Single source of truth for "can we
        # open new positions right now?". manage_open_trades has already run
        # so existing stops/TPs still fire even when entries are blocked.
        verdict = kill_switch.evaluate(
            regime_block_new=regime.block_new_entries,
            regime_label=regime.label,
            regime_note=regime.note,
            current_cash=cash,
        )
        if not verdict.can_trade:
            log.warning("kill_switch [%s]: %s", verdict.gate, verdict.reason)
            audit.record("scan_end", gate=f"kill_{verdict.gate}",
                         reason=verdict.reason)
            if verdict.gate in ("regime", "drawdown"):
                notifier.send(f"⚠ {verdict.reason}")
            return

        try:
            real_held = positions[positions["qty"].astype(float) > 0] if not positions.empty else positions
            invested = 0.0
            unrealized = 0.0
            symbols = []
            per_pos: dict[str, dict] = {}
            if not real_held.empty:
                invested = float((real_held["qty"].astype(float) * real_held["cost_price"].astype(float)).sum())
                unrealized = float(real_held.get("pl_val", 0).astype(float).sum()) if "pl_val" in real_held.columns else 0.0
                symbols = [code.split(".")[-1] for code in real_held["code"].tolist()]
                for _, row in real_held.iterrows():
                    sym = row["code"].split(".")[-1]
                    per_pos[sym] = {
                        "qty": int(float(row.get("qty", 0) or 0)),
                        "last": float(row.get("nominal_price") or 0),
                        "pl_val": float(row.get("pl_val", 0) or 0),
                        "pl_ratio": float(row.get("pl_ratio", 0) or 0),
                    }
            state = risk_manager._load_state()
            realized_total = float(state.get("realized_pnl_total", 0.0))
            snap = {
                "ts": clock.ny_now().isoformat(),
                "cash": cash,
                "positions_count": len(real_held),
                "invested": invested,
                "budget": risk_manager.budget_usd(),
                "budget_used_pct": round(invested / risk_manager.budget_usd() * 100, 1) if risk_manager.budget_usd() else 0,
                "unrealized_pnl": unrealized,
                "realized_pnl_total": realized_total,
                "total_pnl": unrealized + realized_total,
                "symbols": symbols,
                "ai_model": settings.gemini_model,
                "scan_interval_min": settings.scan_interval_min,
                "entry_threshold": settings.entry_threshold,
                "max_hold_days": settings.max_hold_days,
                "timeframe": settings.timeframe,
                "trade_env": settings.moomoo_trade_env,
                "vix": round(vix, 1),
                "regime": regime.label,
                "regime_note": regime.note,
                "open_risk": round(portfolio.current_open_risk(), 2),
                "heat_cap": round(risk_manager.budget_usd() * portfolio.PORTFOLIO_HEAT_PCT, 2),
                "trade_stats": portfolio.trade_stats(50),
                "skip_gates": audit.gate_summary(200),
                "last_scan_utc": clock.utc_now_corrected().isoformat(),
                "phase": "trade",
                "clock": clock.status(),
                "per_position": per_pos,
                "realized_pnl_today": float(state.get("realized_pnl_today", 0.0)),
            }
            (settings.root / "data" / "account.json").write_text(
                json.dumps(snap, indent=2, default=str)
            )
            history.append({
                "invested": round(invested, 2),
                "budget": risk_manager.budget_usd(),
                "unrealized_pnl": round(unrealized, 2),
                "realized_pnl_total": round(realized_total, 2),
                "total_pnl": round(unrealized + realized_total, 2),
                "positions_count": len(real_held),
                "symbols": symbols,
                "timeframe": settings.timeframe,
            })
        except Exception as e:
            log.warning("snapshot/history write failed: %s", e)

        # 4. Score candidates with TWO strategies (trend + mean-revert) in parallel.
        # For each ticker, keep the higher-scoring signal — they're complementary
        # and rarely both fire (trend wants ADX↑, MR wants ADX↓).
        ranked: list[indicators.Signal] = []
        # Marginal-setup buffer: anything within 10 pts BELOW threshold also
        # enters the funnel, but gets half-conviction (smaller position).
        # Without this buffer the bot scores ~60 for most US large-caps and
        # NEVER hits 70+, so top_5 = [] every scan → zero trades. This zone
        # is the same idea as the ML "neutral" band — try it, but small.
        # Runtime-overridable (owner-approved optimizer); falls back to .env.
        entry_thr = runtime_config.entry_threshold()
        # 2026-06-03 live↔backtest parity: HARD threshold (no marginal sub-70
        # band). The honest backtest uses a hard threshold and still generates
        # ~105 trades/180d on trend+momentum, and the gate-ablation proved sub-70
        # setups destroy returns (thr 65 = −$11/day). The old −10 marginal band
        # was a trend-only-era workaround; momentum scoring clears 70 fine now.
        threshold_floor = entry_thr
        # Refresh the blacklist before this scan's symbol loop. Cheap (file read).
        _bl_active = blacklist.get_blacklist()
        for sym in tickers:
            # Adaptive blacklist — skip symbols flagged as recent net losers.
            if sym in _bl_active:
                audit.record("skip", symbol=sym, gate="blacklist",
                             reason=_bl_active[sym].reason)
                continue
            try:
                df = c.get_kline(sym, bars=120)
                # Look-ahead audit — never score on a still-forming intraday bar.
                df = _drop_forming_bar(df, settings.timeframe)
                # Independent strategies: each can submit if its own score
                # clears the floor. 2026-05-29: added strategy_momentum
                # (stricter breakout setup) — sized identically to trend, just
                # a different filter mix biased toward explosive setups.
                # Mean-revert gated by MR_ENABLED (combo sweep showed it was
                # net-negative in current bull-skewed watchlist).
                sig_trend = indicators.evaluate(sym, df)
                sig_mom = strategy_momentum.evaluate(sym, df)
                if sig_trend.score >= threshold_floor:
                    ranked.append(sig_trend)
                if sig_mom.score >= threshold_floor:
                    ranked.append(sig_mom)
                if settings.mr_enabled:
                    sig_mr = strategy_mr.evaluate(sym, df)
                    if sig_mr.score >= threshold_floor:
                        ranked.append(sig_mr)
            except Exception as e:
                log.warning("scoring %s failed: %s", sym, e)

        ranked.sort(key=lambda s: s.score, reverse=True)
        log.info("top 5: %s", [(s.symbol, s.score) for s in ranked[:5]])

        # ----- Portfolio-full notification (edge-triggered) -----
        # Compute current unique-symbol count (matches risk_manager's
        # MAX_POSITIONS semantics: 1 ticker = 1 slot regardless of stacks).
        held_syms = set(symbols) | set(pending_symbols)
        cap = risk_manager.max_positions()
        currently_full = len(held_syms) >= cap
        was_full = bool(risk_manager._load_state().get("portfolio_full_notified"))
        if currently_full and not was_full:
            notifier.send(
                f"📦 仓位已满 {len(held_syms)}/{cap} — "
                f"暂停寻新单（仍会扫加仓机会）。\n"
                f"持仓: {', '.join(sorted(held_syms))}"
            )
            db.update_state({"portfolio_full_notified": True})
        elif not currently_full and was_full:
            db.update_state({"portfolio_full_notified": False})

        # Per-scan cap on brand-new tickers (stacking add-ons exempt).
        new_names_opened = 0
        new_names_limit = settings.max_new_names_per_scan

        # (Regime / halt / drawdown kill switches already evaluated above
        # via kill_switch.evaluate() — by this point we're cleared to trade.)

        # 5. Candidate funnel — 3-layer decision architecture:
        #    Rule score (0-100)   → ranking + initial pass (≥ entry_threshold)
        #    ML proba   (0-1)     → conviction multiplier (veto if too low)
        #    AI verdict (pass/veto) → independent veto (no score blend!)
        #
        # No more "final_score" — each layer answers its own question:
        #   • Rule:  "is this a textbook technical setup?"          → rank + filter
        #   • ML:    "did similar setups historically work out?"    → size
        #   • AI:    "is there fresh news that contradicts this?"   → veto
        #
        # AI budget: Gemini 2.5-flash free tier is 50 RPM / 1500 RPD. With 12
        # scans/day (trade-phase only) × budget=5 = 60 calls/day → well under
        # quota. Previous budget=2 was leaving most candidates unchecked.
        ai_budget = 10
        scan_skips: list[tuple[str, str]] = []   # (symbol, gate) — summarised at scan end

        for sig in ranked:
            # Ranked is sorted desc by score; stop once we drop below the floor.
            if sig.score < threshold_floor:
                break

            # Concentration gate — once per-scan new-names budget is spent,
            # only stacking add-ons (already-held symbol) may proceed. Silently
            # skip extras (don't audit or Telegram-summarise — it's by design).
            is_stack_candidate = sig.symbol in held_syms
            if not is_stack_candidate and new_names_opened >= new_names_limit:
                continue

            # Stacking add-ons require the rule score to clear full threshold —
            # don't pyramid on a marginal setup, even if base setup was strong.
            if is_stack_candidate and sig.score < entry_thr:
                continue

            # "Marginal" = rule score in [threshold-10, threshold). These setups
            # still enter the funnel but get sized down via conviction.
            marginal_setup = sig.score < entry_thr
            setup_conviction = 0.5 if marginal_setup else 1.0

            def _skip(gate: str, reason: str, _sig=sig) -> None:
                log.info("Skip %s [%s]: %s", _sig.symbol, gate, reason)
                audit.record("skip", symbol=_sig.symbol, gate=gate,
                             reason=reason, score=_sig.score)
                # Silence "max positions" / "stacking gate" reasons from the
                # Telegram skip summary — they're routine, not actionable.
                if gate == "risk" and (
                    "max positions" in reason
                    or "max stacks" in reason
                    or "unrealised" in reason
                    or "stacking disabled" in reason
                ):
                    return
                scan_skips.append((_sig.symbol, gate))

            # --- Context fetches (daily df for MTF/gap, ML proba already cached) ---
            df_d = None
            try:
                df_d = c.get_kline(sig.symbol, bars=60, ktype=KLType.K_DAY)
            except Exception as e:
                log.warning("daily fetch failed %s: %s — skipping MTF/gap", sig.symbol, e)

            if df_d is not None:
                if settings.timeframe == "HOUR_1":
                    daily_ok, daily_reason = indicators.daily_trend_bullish(df_d)
                    if not daily_ok:
                        _skip("mtf", daily_reason)
                        continue
                gap_ok, gap_reason = indicators.check_gap(df_d, max_gap_pct=settings.max_gap_pct)
                if not gap_ok:
                    _skip("gap", gap_reason)
                    continue

            # --- Earnings / spread (cheap context veto) ---
            # 2026-06-03: sector-exposure gate removed — structurally non-binding
            # (MAX_PER_SECTOR=5 >= MAX_POSITIONS=5, so a sector can never exceed
            # the total-position cap). check_sector_exposure() is kept in sector.py
            # but no longer called here. SECTOR_MAP/get_sector still used elsewhere.
            ern_blocked, ern_reason = earnings_block(sig.symbol)
            if ern_blocked:
                _skip("earnings", ern_reason)
                continue

            try:
                spread_pct = c.get_spread_pct(sig.symbol)
                if spread_pct > SPREAD_MAX_PCT:
                    _skip("spread", f"bid-ask {spread_pct:.2f}% > {SPREAD_MAX_PCT}%")
                    continue
            except Exception:
                pass

            # Position conviction (ML removed 2026-06-03; with the hard
            # threshold, setup_conviction is 1.0 — kept for the VIX/DD sizing path).
            conviction = setup_conviction

            # --- AI verdict (Gemini + Tavily news, independent advisory) ---
            # Stacking add-ons skip AI consult — we've already done the diligence
            # on the original entry, and the AI budget should reserve for fresh
            # names where context might differ. (Audit 2026-05-28.)
            if is_stack_candidate:
                ai_pass, ai_score, ai_reason = True, 60, "stack — AI re-check skipped"
            elif ai_budget <= 0:
                ai_pass, ai_score, ai_reason = True, 50, "AI budget exhausted — neutral"
            else:
                ai_pass, ai_score, ai_reason = ai_validator.validate(sig)
                ai_budget -= 1
            log.info("%s rule=%.1f ai=%s conviction=%.2f (%s)",
                     sig.symbol, sig.score,
                     "pass" if ai_pass else "veto", conviction, ai_reason)
            # 2026-06-03 live↔backtest parity: the backtest has no AI layer, so AI
            # is ADVISORY by default (logged, shown in the card) and does NOT block
            # — live trades then match the backtest the $/day figure is built on.
            # Set AI_VETO_BLOCKING=true to restore hard blocking.
            if not ai_pass:
                if settings.ai_veto_blocking:
                    _skip("ai_veto", ai_reason)
                    continue
                ai_reason = f"[advisory veto, not blocking] {ai_reason}"

            # --- Risk / heat / sizing (all factor in conviction) ---
            ok, reason = risk_manager.can_open_new(
                sig, positions, cash, pending_value, pending_symbols,
                vix=vix, conviction=conviction,
            )
            if not ok:
                _skip("risk", reason)
                continue

            # Regime up-scaling (owner-approved tailwind press) — mirror the honest
            # engine exactly: boost size ONLY in a confirmed strong bull AND calm
            # VIX. settings.regime_bull_mult defaults to 1.0 (inert) until the
            # owner sets REGIME_BULL_MULT in .env.
            regime_mult = (settings.regime_bull_mult
                           if (regime is not None and regime.bullish
                               and vix < settings.regime_vix_calm)
                           else 1.0)
            qty = risk_manager.calc_position_size(
                sig, vix=vix, conviction=conviction, regime_mult=regime_mult)

            # 2026-06-03: portfolio.heat_check gate removed — structurally
            # non-binding (heat cap = 20% of account while per-trade risk is also
            # a % of account, so both scale together and it never fires).
            # portfolio.heat_check() is kept as the heat primitive but not called.

            try:
                executor.open_position(c, sig, qty)
                if not is_stack_candidate:
                    new_names_opened += 1
                    held_syms.add(sig.symbol)
                audit.record("buy", symbol=sig.symbol, score=sig.score,
                             reason=ai_reason,
                             extra={"qty": qty, "price": sig.price,
                                    "stop": sig.stop_loss, "tp": sig.take_profit,
                                    "vix": vix, "regime": regime.label,
                                    "conviction": conviction,
                                    "setup_quality": "marginal" if marginal_setup else "full",
                                    "is_stack": is_stack_candidate,
                                    "strategy": getattr(sig, "strategy", "trend")})
                notifier.send(notifier.signal_msg(sig, ai_reason, qty))
                cash -= qty * sig.price
                pending_value += qty * sig.price
                pending_symbols.add(sig.symbol)
            except Exception as e:
                log.exception("open_position failed for %s: %s", sig.symbol, e)
                audit.record("error", symbol=sig.symbol, reason=str(e))
                # Buffer into the end-of-scan summary (line ~545) instead of
                # firing one Telegram per failure — keeps the chat readable
                # when several executions trip the same broker-side issue.
                scan_skips.append((sig.symbol, "exec_error"))

    # Send one skip-summary per scan instead of one message per candidate.
    if scan_skips:
        by_gate: dict[str, list[str]] = {}
        for sym, gate in scan_skips:
            by_gate.setdefault(gate, []).append(sym)
        lines = [f"  • {gate}: {', '.join(syms)}" for gate, syms in sorted(by_gate.items())]
        notifier.send(f"📋 *Scan skips* ({len(scan_skips)} total)\n" + "\n".join(lines))

    audit.record("scan_end")
    log.info("=== scan end ===")


def _nyse_holidays(year: int) -> set:
    """Return the set of NYSE full-day holiday dates for `year`."""
    from datetime import date, timedelta

    def observed(d: date) -> date:
        if d.weekday() == 5:   # Saturday → Friday
            return d - timedelta(days=1)
        if d.weekday() == 6:   # Sunday → Monday
            return d + timedelta(days=1)
        return d

    def nth_weekday(y: int, m: int, wd: int, n: int) -> date:
        """nth occurrence (1-based) of weekday wd (0=Mon) in month m."""
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def last_monday(y: int, m: int) -> date:
        import calendar
        last = date(y, m, calendar.monthrange(y, m)[1])
        return last - timedelta(days=last.weekday())   # weekday 0 = Mon

    def easter(y: int) -> date:
        a = y % 19
        b, c = divmod(y, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        ll = (32 + 2 * e + 2 * i - h - k) % 7
        m2 = (a + 11 * h + 22 * ll) // 451
        mo, dy = divmod(114 + h + ll - 7 * m2, 31)
        return date(y, mo, dy + 1)

    hols = {
        observed(date(year, 1, 1)),            # New Year's Day
        nth_weekday(year, 1, 0, 3),            # MLK Day (3rd Mon Jan)
        nth_weekday(year, 2, 0, 3),            # Presidents Day (3rd Mon Feb)
        easter(year) - timedelta(days=2),      # Good Friday
        last_monday(year, 5),                  # Memorial Day (last Mon May)
        observed(date(year, 6, 19)),           # Juneteenth
        observed(date(year, 7, 4)),            # Independence Day
        nth_weekday(year, 9, 0, 1),            # Labor Day (1st Mon Sep)
        nth_weekday(year, 11, 3, 4),           # Thanksgiving (4th Thu Nov)
        observed(date(year, 12, 25)),          # Christmas
    }
    return hols


def in_market_hours() -> bool:
    now = clock.ny_now()
    if now.weekday() >= 5:
        return False
    if now.date() in _nyse_holidays(now.year):
        log.info("NYSE holiday today (%s) — skipping scan", now.date())
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _weekly_backtest_validation_job() -> None:
    """Sunday 22:30 ET — runs a 90-day backtest under current .env settings.

    Purpose: continuous health-check. If the strategy starts degrading (rule
    changes in the market, watchlist drift, etc.) the Sortino on a rolling
    90-day window will fall before live results do, giving early warning.

    Result is logged + a one-line summary is sent to Telegram. Does NOT
    interrupt or modify the live trading loop — purely diagnostic.
    """
    log.info("weekly backtest validation: starting")
    try:
        from .backtest import BacktestConfig, run_backtest
        cfg = BacktestConfig(
            days=90,
            timeframe=settings.timeframe,
            threshold=settings.entry_threshold,
            account_usd=settings.account_usd,
            risk_per_trade=settings.risk_per_trade,
            max_position_pct=settings.max_position_pct,
            max_hold_days=settings.max_hold_days,
            tp_atr_mult=settings.tp_atr_mult,
            sl_atr_mult=settings.sl_atr_mult,
            max_gap_pct=settings.max_gap_pct,
        )
        result = run_backtest(cfg)
        m = result.get("metrics", {})
        mc = m.get("monte_carlo", {})
        prob_str = (f"{mc['prob_profitable_pct']}%"
                    if "prob_profitable_pct" in mc else "—")
        notifier.send(
            f"📊 *Weekly backtest* ({cfg.timeframe}, {cfg.days}d)\n"
            f"  Trades:     {m.get('total_trades', 0)}\n"
            f"  Win rate:   {m.get('win_rate_pct', 0)}%\n"
            f"  Sortino:    {m.get('sortino_ratio', 0)}\n"
            f"  Profit factor: {m.get('profit_factor', 0)}\n"
            f"  Net PnL:    ${m.get('net_pnl_usd', 0):+.2f}  "
            f"({m.get('total_return_pct', 0):+.1f}%)\n"
            f"  Max DD:     {m.get('max_drawdown_pct', 0):.1f}%\n"
            f"  P(profit):  {prob_str}"
        )
        log.info("weekly backtest done: trades=%d sortino=%s",
                 m.get("total_trades", 0), m.get("sortino_ratio", 0))
        cron_state.record_run("weekly_backtest")
    except Exception as e:
        log.exception("weekly backtest failed: %s", e)
        notifier.send(f"⚠ Weekly backtest failed: {e}")


def _monthly_optuna_job() -> None:
    """1st of each month 03:00 ET — runs a 30-trial Optuna walk-forward study.

    Best params are saved to `data/optimizer/*.json` and a summary is sent
    to Telegram. Does **NOT** auto-update `.env` — the user reviews and
    decides whether to apply. This is intentional: blindly chasing the latest
    Optuna winner is overfitting to last month's regime.
    """
    log.info("monthly Optuna optimization: starting")
    try:
        from .optimizer import run_study
        summary = run_study(
            n_trials=30,
            days=60,
            n_folds=3,
            min_trades=30,
            timeframe=settings.timeframe,
            fast_mode=True,
        )
        bp = summary.get("best_params", {})
        attrs = summary.get("best_user_attrs", {})
        msg = (
            f"🧪 *Monthly Optuna* ({summary['base_config']['timeframe']}, "
            f"{summary['base_config']['days']}d, {summary['n_trials']} trials)\n"
            f"  Best Sortino: {summary.get('best_value_sortino', '?')}\n"
            f"  n_trades:     {attrs.get('n_trades', '?')}\n"
            f"  worst-fold:   {attrs.get('sortino_min', '?')}\n"
            f"\n*Suggested params* (review before applying):\n"
            f"  threshold     = {bp.get('threshold', '?')}\n"
            f"  tp_atr_mult   = {bp.get('tp_atr_mult', '?')}\n"
            f"  sl_atr_mult   = {bp.get('sl_atr_mult', '?')}\n"
            f"  max_gap_pct   = {bp.get('max_gap_pct', '?')}\n"
            f"  base_slip_bp  = {bp.get('base_slip_bp', '?')}\n"
            f"\n*Live .env now*: thr={settings.entry_threshold}, "
            f"tp={settings.tp_atr_mult}, sl={settings.sl_atr_mult}, "
            f"gap={settings.max_gap_pct}"
        )
        notifier.send(msg)
        log.info("monthly Optuna done: best_sortino=%s params=%s",
                 summary.get("best_value_sortino"), bp)
        cron_state.record_run("monthly_optuna")
    except Exception as e:
        log.exception("monthly Optuna failed: %s", e)
        notifier.send(f"⚠ Monthly Optuna failed: {e}")


def _daily_blacklist_review_job() -> None:
    """Daily 23:00 ET — adaptive blacklist evaluation.

    Adds new chronic losers, removes recovered names, extends review periods
    for symbols that are still bad. Telegram summary fires only when anything
    changed (no spam on quiet days).
    """
    log.info("daily blacklist review: starting")
    try:
        summary = blacklist.evaluate_all(notifier_send=notifier.send)
        log.info("blacklist review done: %s", summary)
        cron_state.record_run("daily_blacklist")
    except Exception as e:
        log.exception("blacklist review failed: %s", e)


def _weekly_self_review_job() -> None:
    """Sunday 23:00 ET — review the past week's REAL fills and emit suggestions
    (analyze→notify→approve). Never changes live behavior on its own. Each
    autonomous step runs in its OWN try, so a failure in one (e.g. a notify hiccup
    in the review) can't silently skip self-improvement or the optimizer."""
    log.info("weekly self-review: starting")
    report = None
    try:
        report = self_review.run_and_notify(days=7)
        log.info("weekly self-review done: %d trades, $%.2f/day, %d suggestions",
                 report.get("n_trades", 0), report.get("per_day", 0.0),
                 len(report.get("suggestions", [])))
    except Exception as e:
        log.exception("weekly self-review failed: %s", e)
        try:
            notifier.send(f"⚠ Weekly self-review failed: {e}")
        except Exception:
            pass
    # Evidence-based self-improvement (half-Kelly risk + universe review) → approval
    # queue. Reads real fills directly, so it's independent of the review/notify above.
    try:
        si = self_improve.run_all()
        log.info("self-improve: kelly_proposed=%s universe_drops=%s",
                 si.get("kelly_proposed"), si.get("universe_dropped_proposed"))
    except Exception as e:
        log.warning("self-improve proposals failed: %s", e)
    # Autonomous DeepSeek optimizer — INDEPENDENT step so a review/notify failure
    # doesn't silently skip the one auto path that proposes param changes. Reuses
    # this run's report, else recomputes. No-op until DEEPSEEK_API_KEY is set.
    try:
        from . import optimizer_ai
        rev = report if report is not None else self_review.weekly_review(days=7)
        n = optimizer_ai.propose_from_review(rev)
        if n:
            notifier.send(f"🤖 DeepSeek 优化器提了 {n} 条参数建议 — 待你在 GUI/CLI 批准。")
    except Exception as e:
        log.warning("weekly optimizer step failed: %s", e)
    # Bookkeeping — also independent so it always runs.
    try:
        approvals.purge_resolved()
        cron_state.record_run("self_review")
    except Exception as e:
        log.warning("self-review bookkeeping failed: %s", e)


def _run_catchup_on_startup() -> None:
    """Detect and run any scheduled jobs that were missed while the laptop was off.

    Compares each known job's persisted `last_run` to its most-recent expected
    fire time. If `last_run < expected`, we fire the job ONCE (no backlog
    flooding — one catchup per missed cycle is enough).

    On a first-ever boot we seed the state with NOW so we don't run every
    job at once. Sends a Telegram summary so the user knows what fired.
    """
    # Seed empty state — fresh installs don't fire anything.
    is_first_boot = cron_state.initialize_if_empty()
    if is_first_boot:
        log.info("catchup: first boot detected — cron_state seeded, no catchup needed")
        return

    # Map each known job → (expected_last_fire, runner_function, label).
    plan = [
        ("daily_blacklist",
         cron_state.expected_last_fire_daily(23, 0, weekdays_only=True),
         _daily_blacklist_review_job, "Daily blacklist review"),
        # watchlist_refresh + ml_retrain catch-up removed 2026-06-03 (see run_loop):
        # both were silent-execution violations; now manual GUI actions only.
        ("weekly_backtest",
         cron_state.expected_last_fire_weekly(6, 22, 30),  # Sunday 22:30
         _weekly_backtest_validation_job, "Weekly backtest"),
        ("self_review",
         cron_state.expected_last_fire_weekly(6, 23, 0),   # Sunday 23:00
         _weekly_self_review_job, "Weekly self-review"),
        ("monthly_optuna",
         cron_state.expected_last_fire_monthly(1, 3, 0),
         _monthly_optuna_job, "Monthly Optuna optimization"),
    ]

    missed = [(name, fn, label) for name, exp, fn, label in plan
              if cron_state.needs_catchup(name, exp)]

    if not missed:
        log.info("catchup: no missed jobs — all schedules up to date")
        return

    labels = [label for _, _, label in missed]
    log.info("catchup: %d missed job(s) → %s", len(missed), labels)
    try:
        notifier.send("🔄 *Catching up missed scheduled jobs*\n  "
                      + "\n  ".join(f"• {label}" for label in labels)
                      + "\n  (laptop was off when these were due)")
    except Exception as e:
        log.warning("catchup: notifier failed: %s", e)

    for name, fn, label in missed:
        log.info("catchup: running %s (%s)", label, name)
        try:
            fn()
        except Exception as e:
            log.exception("catchup: %s failed: %s", label, e)
            try:
                notifier.send(f"⚠ Catchup '{label}' failed: {e}")
            except Exception:
                pass

    try:
        notifier.send(f"✓ Catchup complete — ran {len(missed)} missed job(s)")
    except Exception:
        pass


def run_loop() -> None:
    # Detect missed scheduled jobs and run them once before the normal loop
    # takes over. Synchronous on purpose — a quick Telegram lets the user
    # know what's happening; long jobs (Optuna ~10 min) just delay the first
    # market scan, which is fine.
    _run_catchup_on_startup()

    sched = BlockingScheduler(timezone=NY)

    def job():
        if in_market_hours():
            scan_once()
        else:
            log.info("outside market hours, skipping")

    # Misfire policy:
    #   • coalesce=True       → if N runs were missed (laptop slept etc), only fire ONCE
    #   • misfire_grace_time  → drop runs more than this many seconds late
    #   • max_instances=1     → never let two scans overlap (prevents API rate-limit blowups)
    sched.add_job(
        job, "interval",
        minutes=settings.scan_interval_min,
        next_run_time=clock.ny_now(),
        coalesce=True,
        misfire_grace_time=60,
        max_instances=1,
    )
    # 2026-06-03: watchlist is PINNED to 10; universe changes go through the
    # weekly self-review → approval queue (no auto-refresh, no manual button).
    # ML retrain removed entirely (subsystem deleted after proving inert).

    # Weekly backtest validation: Sunday 22:30 ET — health-check the strategy
    # on a rolling 90-day window.
    # on the fresh ticker set.
    sched.add_job(_weekly_backtest_validation_job, "cron",
                  day_of_week="sun", hour=22, minute=30,
                  coalesce=True, misfire_grace_time=1800, max_instances=1)

    # Weekly SELF-REVIEW of REAL fills: Sunday 23:00 ET. Reviews what the bot
    # actually did (not a backtest), emits suggestions for owner approval.
    sched.add_job(_weekly_self_review_job, "cron",
                  day_of_week="sun", hour=23, minute=0,
                  coalesce=True, misfire_grace_time=1800, max_instances=1)

    # Monthly Optuna re-optimization: 1st of each month at 03:00 ET. Runs
    # AFTER the ML retrain at 02:00 so the search uses the freshest model.
    # Telegrams suggested params; user reviews before editing .env.
    sched.add_job(_monthly_optuna_job, "cron", day=1, hour=3, minute=0,
                  coalesce=True, misfire_grace_time=3600, max_instances=1)

    # Telegram approval sync: every 60s, any time of day. Posts pending
    # suggestion cards with Approve/Reject buttons, processes your taps into the
    # shared approval queue, and applies anything you approved (same queue the
    # GUI reads, so both stay in sync). Cheap HTTP; no-op if Telegram unset.
    def _tg_sync():
        try:
            tg_approvals.sync()
            approvals.apply_approved()   # apply off-hours approvals promptly
        except Exception as e:
            log.debug("tg approval sync failed: %s", e)
    sched.add_job(_tg_sync, "interval", seconds=60,
                  coalesce=True, misfire_grace_time=30, max_instances=1)

    # Daily blacklist review: 23:00 ET every weekday. Reads recent closed
    # trades, adds chronic losers, removes recovered names, extends watch on
    # symbols still in the doghouse. Notifies via Telegram only on changes.
    sched.add_job(_daily_blacklist_review_job, "cron",
                  day_of_week="mon-fri", hour=23, minute=0,
                  coalesce=True, misfire_grace_time=1800, max_instances=1)

    log.info(
        "scheduler started — scan=%dm, weekly backtest=Sun 22:30, "
        "monthly Optuna=1st@03:00, daily blacklist=23:00 ET "
        "(ML retrain + watchlist refresh disabled — manual GUI only)",
        settings.scan_interval_min,
    )
    from .i18n import t
    notifier.send(t("tg_started", env=settings.moomoo_trade_env))
    sched.start()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "scan":
        scan_once()
    elif cmd == "run":
        run_loop()
    elif cmd == "review":
        # On-demand weekly self-review (also the Sunday cron). analyze→notify.
        _weekly_self_review_job()
    elif cmd == "approvals":
        # List pending owner-approval suggestions.
        pend = approvals.list_pending()
        if not pend:
            print("No pending approvals.")
        for a in pend:
            print(f"[{a['id']}] {a['kind']}: {a.get('detail','')}\n"
                  f"        → {a.get('action','')}")
    elif cmd in ("approve", "reject") and len(sys.argv) >= 3:
        ok = approvals.resolve(sys.argv[2], approved=(cmd == "approve"))
        print(f"{cmd} {sys.argv[2]}: {'done — applies next scan' if ok else 'not found / already resolved'}"
              if cmd == "approve" else
              f"{cmd} {sys.argv[2]}: {'rejected' if ok else 'not found / already resolved'}")
    else:
        print(f"unknown command: {cmd}")
        print("commands: scan | run | review | approvals | approve <id> | reject <id>")
        sys.exit(2)


if __name__ == "__main__":
    main()
