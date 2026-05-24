"""Tkinter control panel for moomoo-trader.

Run with:
    .venv/bin/python gui.py

What you can do:
    • Start / stop the 15-min scheduler
    • Trigger a one-shot scan
    • See OpenD connectivity, market hours, cash, positions, daily P&L
    • Live tail of the trader log

No extra dependencies — uses only the stdlib.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# i18n — must be imported AFTER sys.path is set up so src.i18n resolves.
from src.i18n import t as _t, current_lang, set_lang

PID_FILE = ROOT / "logs" / "scheduler.pid"
LOG_FILE = ROOT / "logs" / "trader.log"
SCHEDULER_LOG = ROOT / "logs" / "scheduler.log"
OPEN_TRADES = ROOT / "data" / "open_trades.json"
STATE_FILE = ROOT / "data" / "state.json"
ACCOUNT_FILE = ROOT / "data" / "account.json"
HISTORY_FILE = ROOT / "data" / "history.jsonl"
BACKTEST_FILE = ROOT / "data" / "backtest_results.json"
RECONCILE_FILE = ROOT / "data" / "reconcile.json"
AUDIT_FILE = ROOT / "data" / "audit.jsonl"
TRADES_FILE = ROOT / "data" / "trades.jsonl"
WATCHLIST_FILE = ROOT / "config" / "watchlist.json"
PYTHON = str(ROOT / ".venv" / "bin" / "python")

REFRESH_MS = 5000


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid if is_process_alive(pid) else None
    except ValueError:
        return None


def opend_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11111), timeout=1):
            return True
    except OSError:
        return False


def ny_now() -> datetime:
    """Authoritative NY time via src.clock — pytz handles EDT/EST DST,
    network sources (timeapi.io / worldtimeapi / NTP) catch machine clock drift."""
    from src import clock
    return clock.ny_now()


def in_market_hours() -> bool:
    now = ny_now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(_t("app_title"))
        # Sized for 13" MacBook (1280×800 logical) — fits with menu bar + dock.
        # Compact 2-row controls + 2-row strategy so nothing wraps off-screen.
        self.geometry("1180x720")
        self.minsize(1024, 640)
        self._build_menu()
        self._build()
        self._refresh()

    # ---------- menu bar ----------
    def _build_menu(self) -> None:
        bar = tk.Menu(self)
        lang_menu = tk.Menu(bar, tearoff=0)
        lang_menu.add_command(label=_t("menu_lang_zh"),
                              command=lambda: self._switch_lang("zh"))
        lang_menu.add_command(label=_t("menu_lang_en"),
                              command=lambda: self._switch_lang("en"))
        bar.add_cascade(label=_t("menu_language"), menu=lang_menu)
        self.config(menu=bar)

    def _switch_lang(self, lang: str) -> None:
        if lang == current_lang():
            return
        set_lang(lang)
        from tkinter import messagebox
        messagebox.showinfo(_t("lang_switched_title"),
                            _t("lang_switched_body"),
                            parent=self)

    # ---------- layout ----------
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}

        # Top status row
        status = ttk.LabelFrame(self, text=_t("status_section"))
        status.pack(fill="x", **pad)
        self.lbl_scheduler = ttk.Label(status, text=f"{_t('scheduler')}: ?")
        self.lbl_opend = ttk.Label(status, text=f"{_t('opend')}: ?")
        self.lbl_unlock = ttk.Label(status, text=f"{_t('trade')}: ?")
        self.lbl_market = ttk.Label(status, text=f"{_t('market')}: ?")
        self.lbl_clock = ttk.Label(status, text=f"{_t('et_prefix')} --:--", cursor="hand2")
        self.lbl_clock.bind("<Button-1>", lambda _: self._force_clock_sync())
        self.lbl_heart = ttk.Label(status, text=_t("last_scan_none"))
        self.lbl_regime = ttk.Label(status, text=_t("regime_none"))
        for i, w in enumerate([self.lbl_scheduler, self.lbl_opend, self.lbl_unlock,
                                self.lbl_market, self.lbl_clock, self.lbl_heart,
                                self.lbl_regime]):
            w.grid(row=0, column=i, padx=8, pady=6, sticky="w")

        # Budget row — most important number for the user
        bud = ttk.LabelFrame(self, text=_t("budget_section"))
        bud.pack(fill="x", **pad)
        self.lbl_budget = ttk.Label(bud, text="—", font=("SF Pro", 13, "bold"))
        self.lbl_budget.grid(row=0, column=0, padx=10, pady=4, sticky="w")
        self.bar = ttk.Progressbar(bud, mode="determinate", maximum=100, length=300)
        self.bar.grid(row=0, column=1, padx=10, pady=4, sticky="w")
        self.lbl_pnl = ttk.Label(bud, text="PnL: —", font=("SF Pro", 12))
        self.lbl_pnl.grid(row=0, column=2, padx=20, pady=4, sticky="w")

        # Account row
        acct = ttk.LabelFrame(self, text=_t("account_section"))
        acct.pack(fill="x", **pad)
        self.lbl_cash = ttk.Label(acct, text=f"{_t('broker_cash')}: —")
        self.lbl_positions = ttk.Label(acct, text=f"{_t('positions')}: —")
        self.lbl_streak = ttk.Label(acct, text=f"{_t('loss_streak')}: —")
        self.lbl_halted = ttk.Label(acct, text=f"{_t('halted')}: —", cursor="hand2")
        self.lbl_halted.bind("<Button-1>", lambda _: self._reset_halt())
        self.lbl_recon = ttk.Label(acct, text=f"{_t('reconcile')}: —")
        for i, w in enumerate([self.lbl_cash, self.lbl_positions, self.lbl_streak, self.lbl_halted, self.lbl_recon]):
            w.grid(row=0, column=i, padx=12, pady=6, sticky="w")

        # Config row (AI model + thresholds)
        cfg = ttk.LabelFrame(self, text=_t("strategy_section"))
        cfg.pack(fill="x", **pad)
        # Two-row strategy panel — config knobs on row 0, runtime stats on row 1.
        self.lbl_ai = ttk.Label(cfg, text=f"{_t('ai_model')}: —")
        self.lbl_thresh = ttk.Label(cfg, text=f"{_t('entry_threshold')} —")
        self.lbl_interval = ttk.Label(cfg, text=f"{_t('scan_every')} —")
        self.lbl_hold = ttk.Label(cfg, text=f"{_t('max_hold')} —")
        self.lbl_env = ttk.Label(cfg, text=f"{_t('env')}: —")
        for i, w in enumerate([self.lbl_ai, self.lbl_thresh, self.lbl_interval,
                                self.lbl_hold, self.lbl_env]):
            w.grid(row=0, column=i, padx=6, pady=2, sticky="w")
        # Row 1 — live runtime: VIX / ML / Strategy mix
        self.lbl_vix = ttk.Label(cfg, text="VIX: —")
        self.lbl_ml = ttk.Label(cfg, text="ML: —")
        self.lbl_strat = ttk.Label(cfg, text="🎯 —")
        for i, w in enumerate([self.lbl_vix, self.lbl_ml, self.lbl_strat]):
            w.grid(row=1, column=i, padx=6, pady=2, sticky="w", columnspan=2)

        # Controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)
        # Two rows of buttons — row 0 = scheduler control, row 1 = data windows.
        self.btn_start = ttk.Button(ctrl, text=_t("btn_start"), command=self.start_scheduler)
        self.btn_stop = ttk.Button(ctrl, text=_t("btn_stop"), command=self.stop_scheduler)
        self.btn_scan = ttk.Button(ctrl, text=_t("btn_scan"), command=self.scan_now)
        self.btn_logs = ttk.Button(ctrl, text=_t("btn_log"), command=self.open_logs)
        for i, b in enumerate([self.btn_start, self.btn_stop, self.btn_scan, self.btn_logs]):
            b.grid(row=0, column=i, padx=3, pady=2, sticky="ew")

        self.btn_hist = ttk.Button(ctrl, text=_t("btn_history"), command=self.open_history)
        self.btn_bt = ttk.Button(ctrl, text=_t("btn_backtest"), command=self.open_backtest)
        self.btn_equity = ttk.Button(ctrl, text=_t("btn_equity"), command=self.open_equity)
        self.btn_audit = ttk.Button(ctrl, text=_t("btn_audit"), command=self.open_audit)
        self.btn_wl = ttk.Button(ctrl, text=_t("btn_watchlist"), command=self.open_watchlist)
        self.btn_wl_refresh = ttk.Button(ctrl, text="🔄 Refresh WL",
                                          command=self.refresh_watchlist)
        self.btn_sect = ttk.Button(ctrl, text=_t("btn_sectors"), command=self.open_sectors)
        self.btn_ml = ttk.Button(ctrl, text=_t("btn_ml"), command=self.open_ml)
        row1 = [self.btn_hist, self.btn_bt, self.btn_equity, self.btn_audit,
                self.btn_wl, self.btn_wl_refresh, self.btn_sect, self.btn_ml]
        for i, b in enumerate(row1):
            b.grid(row=1, column=i, padx=3, pady=2, sticky="ew")
        # Make columns expand evenly so buttons fill the row.
        for i in range(max(4, len(row1))):
            ctrl.columnconfigure(i, weight=1, uniform="btn")

        # Positions
        pos = ttk.LabelFrame(self, text=_t("positions_section"))
        pos.pack(fill="x", **pad)
        cols = ("symbol", "qty", "entry", "last", "stop", "tp", "atr", "pnl")
        col_headers = [_t("col_symbol"), _t("col_qty"), _t("col_entry"),
                       _t("col_last"), _t("col_stop"), _t("col_tp"),
                       _t("col_atr"), _t("col_pnl")]
        self.tree = ttk.Treeview(pos, columns=cols, show="headings", height=4)
        widths = (75, 55, 85, 85, 85, 85, 60, 110)
        for c, h, w in zip(cols, col_headers, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        # Colour tags for P&L
        self.tree.tag_configure("win", foreground="#006600")
        self.tree.tag_configure("loss", foreground="#aa0000")
        self.tree.pack(fill="x", padx=6, pady=6)

        # Right-click context menu for manual override.
        self.pos_menu = tk.Menu(self, tearoff=0)
        self.pos_menu.add_command(label=_t("menu_close_now"), command=self._ctx_close)
        self.pos_menu.add_command(label=_t("menu_edit_stop"), command=self._ctx_edit_stop)
        self.tree.bind("<Button-2>", self._on_pos_right_click)   # macOS right-click
        self.tree.bind("<Button-3>", self._on_pos_right_click)   # Linux/Windows
        self.tree.bind("<Control-Button-1>", self._on_pos_right_click)  # macOS ctrl-click

        # Log tail
        logbox = ttk.LabelFrame(self, text=_t("log_section"))
        logbox.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(logbox, wrap="word", height=14, font=("Menlo", 11))
        ysb = ttk.Scrollbar(logbox, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")

    # ---------- actions ----------
    def start_scheduler(self) -> None:
        if read_pid() is not None:
            self._toast(_t("scheduler_already_running"))
            return
        if not opend_reachable():
            self._toast(_t("opend_unreachable"))
            return

        SCHEDULER_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_fd = open(SCHEDULER_LOG, "a")

        # Wrap with caffeinate on macOS so the system doesn't go to idle/sleep
        # while the bot is running. -i = prevent idle sleep, -s = prevent system
        # sleep (display can still sleep — best for laptops on power).
        # start_new_session=True puts caffeinate + python in one process group,
        # so stop_scheduler's killpg() takes both down cleanly.
        cmd: list[str] = []
        caffeinate = "/usr/bin/caffeinate"
        if Path(caffeinate).exists():
            cmd = [caffeinate, "-is"]
        cmd += [PYTHON, "-m", "src.main", "run"]

        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        wrapped = " (wrapped in caffeinate)" if cmd[0] == caffeinate else ""
        self._toast(_t("scheduler_started", pid=proc.pid) + wrapped)

    def stop_scheduler(self) -> None:
        pid = read_pid()
        if pid is None:
            self._toast(_t("scheduler_not_running"))
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(10):
            if not is_process_alive(pid):
                break
            time.sleep(0.3)
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        self._toast(_t("scheduler_stopped_toast"))

    def scan_now(self) -> None:
        if not opend_reachable():
            self._toast(_t("opend_unreachable"))
            return
        self.btn_scan.configure(state="disabled", text=_t("btn_scanning"))
        threading.Thread(target=self._do_scan, daemon=True).start()

    def refresh_watchlist(self) -> None:
        """Manually trigger the yfinance-based watchlist refresh."""
        self.btn_wl_refresh.configure(state="disabled", text="Refreshing…")
        self._toast("Refreshing watchlist from S&P 500…")
        threading.Thread(target=self._do_refresh_watchlist, daemon=True).start()

    def _do_refresh_watchlist(self) -> None:
        try:
            from src.watchlist_updater import refresh
            tickers = refresh()
            self.after(0, lambda: self._toast(
                f"✓ Watchlist refreshed: {len(tickers)} tickers"
            ))
        except Exception as e:
            self.after(0, lambda: self._toast(f"⚠ Refresh failed: {e}"))
        finally:
            self.after(0, lambda: self.btn_wl_refresh.configure(
                state="normal", text="🔄 Refresh WL"
            ))

    def _do_scan(self) -> None:
        try:
            subprocess.run(
                [PYTHON, "-m", "src.main", "scan"],
                cwd=str(ROOT),
                stdout=open(SCHEDULER_LOG, "a"),
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            pass
        self.after(0, lambda: self.btn_scan.configure(state="normal", text=_t("btn_scan")))

    def open_logs(self) -> None:
        subprocess.run(["open", str(LOG_FILE)])

    def open_history(self) -> None:
        win = tk.Toplevel(self)
        win.title("Trading History")
        win.geometry("780x440")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # All snapshots tab
        all_frame = ttk.Frame(nb)
        nb.add(all_frame, text="All snapshots")
        cols = ("ts", "invested", "unreal", "real", "total", "positions", "symbols")
        widths = (150, 80, 90, 90, 90, 70, 200)
        tree = ttk.Treeview(all_frame, columns=cols, show="headings", height=14)
        for c, w in zip(cols, widths):
            tree.heading(c, text=c.upper())
            tree.column(c, width=w, anchor="center")
        tree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(all_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        # Weekly tab
        week_frame = ttk.Frame(nb)
        nb.add(week_frame, text="Weekly")
        wcols = ("week", "last_ts", "invested", "unreal", "real", "total", "positions")
        wtree = ttk.Treeview(week_frame, columns=wcols, show="headings", height=14)
        for c, w in zip(wcols, (90, 150, 90, 90, 90, 90, 80)):
            wtree.heading(c, text=c.upper())
            wtree.column(c, width=w, anchor="center")
        wtree.pack(fill="both", expand=True, side="left")
        wsb = ttk.Scrollbar(week_frame, orient="vertical", command=wtree.yview)
        wtree.configure(yscrollcommand=wsb.set)
        wsb.pack(side="right", fill="y")

        records = []
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        for r in records[-200:]:  # last 200 to keep it snappy
            tree.insert("", "end", values=(
                r.get("ts", "")[-15:],
                f"${r.get('invested', 0):,.0f}",
                f"${r.get('unrealized_pnl', 0):+,.2f}",
                f"${r.get('realized_pnl_total', 0):+,.2f}",
                f"${r.get('total_pnl', 0):+,.2f}",
                r.get("positions_count", 0),
                ",".join(r.get("symbols", []))[:30],
            ))

        # Weekly digest: last record per ISO week
        weekly: dict[str, dict] = {}
        for r in records:
            weekly[r.get("week", "?")] = r
        for week, r in sorted(weekly.items()):
            wtree.insert("", "end", values=(
                week,
                r.get("ts", "")[-15:],
                f"${r.get('invested', 0):,.0f}",
                f"${r.get('unrealized_pnl', 0):+,.2f}",
                f"${r.get('realized_pnl_total', 0):+,.2f}",
                f"${r.get('total_pnl', 0):+,.2f}",
                r.get("positions_count", 0),
            ))

    # ---------- refresh ----------
    def _refresh(self) -> None:
        pid = read_pid()
        opend = opend_reachable()
        mkt = in_market_hours()
        self.lbl_scheduler.configure(
            text=f"{_t('scheduler')}: {_t('scheduler_running') if pid else _t('scheduler_stopped')}"
        )
        self.lbl_opend.configure(text=f"{_t('opend')}: {'🟢' if opend else '🔴'}")
        # Trade unlock is inferred from a fresh account.json — threshold
        # tracks scan_interval so it doesn't false-yellow between scans.
        account = read_json(ACCOUNT_FILE)
        unlocked = False
        if account.get("ts"):
            try:
                ts = datetime.fromisoformat(account["ts"])
                age_min = (datetime.now(ts.tzinfo) - ts).total_seconds() / 60
                interval = float(account.get("scan_interval_min", 30) or 30)
                unlocked = age_min < interval + 5     # one interval + small buffer
            except (ValueError, TypeError):
                pass
        self.lbl_unlock.configure(
            text=f"{_t('trade')}: {_t('trade_unlocked') if unlocked else _t('trade_locked')}"
        )
        self.lbl_market.configure(text=f"{_t('market')}: {_t('market_open') if mkt else _t('market_closed')}")
        # Clock badge — short text so the status row fits in 1180px width.
        # Full drift / source info appears in the tooltip on click.
        clk = account.get("clock") or {}
        drift = clk.get("last_drift_sec", 0)
        offset = clk.get("offset_sec", 0)
        if abs(drift) > 30:
            badge = "🔴"
        elif abs(drift) > 5 or abs(offset) > 5:
            badge = "🟡"
        else:
            badge = "🟢"
        self.lbl_clock.configure(
            text=f"{badge} {_t('et_prefix')} {ny_now().strftime('%H:%M')}"
        )

        # Heartbeat: derive thresholds from scan_interval_min so the badge
        # doesn't flicker yellow between two normal scans. Also: when the
        # market is closed AND scheduler is alive, swap the red badge for
        # a "🌙 market closed" badge — staleness is expected, not a bug.
        interval = float(account.get("scan_interval_min", 30) or 30)
        last_scan = account.get("last_scan_utc", "")
        if last_scan:
            try:
                t = datetime.fromisoformat(last_scan)
                from datetime import timezone
                now_ref = datetime.now(timezone.utc) if t.tzinfo else datetime.utcnow()
                age = (now_ref - t).total_seconds() / 60
                green_max = interval + 5
                yellow_max = interval * 3
                if age >= yellow_max and not mkt and pid:
                    # Market closed + scheduler alive → expected staleness
                    self.lbl_heart.configure(text=_t("market_closed_badge", ago=int(age)))
                else:
                    badge = "🟢" if age < green_max else ("🟡" if age < yellow_max else "🔴")
                    self.lbl_heart.configure(text=f"{badge} {age:.0f}m {_t('ago')}")
            except (ValueError, TypeError):
                self.lbl_heart.configure(text=f"{_t('last_scan')}: ?")
        else:
            self.lbl_heart.configure(text=_t("last_scan_none"))

        # Market regime
        reg = account.get("regime", "")
        if reg:
            badge = "🟢" if reg == "BULL" else ("🟡" if reg == "NEUTRAL" else "🔴")
            self.lbl_regime.configure(text=f"{_t('regime')}: {badge} {reg}")
        else:
            self.lbl_regime.configure(text=_t("regime_none"))

        trades = read_json(OPEN_TRADES)
        per_pos = account.get("per_position") or {}
        self.lbl_positions.configure(text=f"{_t('positions')}: {len(trades)}")
        self.tree.delete(*self.tree.get_children())
        for sym, t in trades.items():
            live = per_pos.get(sym, {})
            last = live.get("last") or 0
            pl_val = live.get("pl_val") or 0
            pl_ratio = live.get("pl_ratio") or 0
            tag = "win" if pl_val > 0 else ("loss" if pl_val < 0 else "")
            self.tree.insert(
                "", "end",
                values=(
                    sym,
                    t.get("qty", "—"),
                    f"${t.get('entry_price', 0):.2f}",
                    f"${last:.2f}" if last else "—",
                    f"${t.get('stop_loss', 0):.2f}",
                    f"${t.get('take_profit', 0):.2f}",
                    f"{t.get('atr', 0):.2f}",
                    f"${pl_val:+.2f} ({pl_ratio:+.1f}%)" if last else "—",
                ),
                tags=(tag,) if tag else (),
            )

        state = read_json(STATE_FILE)
        account = read_json(ACCOUNT_FILE)

        # Budget — the user's allocated capital cap
        invested = account.get("invested", 0)
        budget = account.get("budget", 0)
        used_pct = account.get("budget_used_pct", 0)
        self.bar["value"] = min(100, used_pct)
        self.lbl_budget.configure(
            text=_t("budget_template", invested=invested, budget=budget, pct=used_pct)
        )
        total = account.get("total_pnl", 0)
        unreal = account.get("unrealized_pnl", 0)
        realiz = account.get("realized_pnl_total", 0)
        today = account.get("realized_pnl_today", 0)
        pnl_color = "#0a0" if total >= 0 else "#c00"
        # Show today's realized PnL alongside the lifetime total — "did I make
        # money today?" is the single most-asked question by a trader staring
        # at the dashboard mid-session.
        self.lbl_pnl.configure(
            text=(_t("pnl_template", total=total, unreal=unreal, realiz=realiz)
                  + "  ·  " + _t("today_pnl", today=today)),
            foreground=pnl_color,
        )

        cash = account.get("cash") or state.get("starting_cash")
        if cash:
            ts = account.get("ts", "")[-8:-3] if account.get("ts") else ""
            self.lbl_cash.configure(text=f"{_t('broker_cash')}: ${cash:,.2f}" + (f"  ({ts})" if ts else ""))
        else:
            self.lbl_cash.configure(text=_t("broker_cash_waiting"))
        self.lbl_streak.configure(text=f"{_t('loss_streak')}: {state.get('loss_streak_days', 0)}d")
        if state.get("halted"):
            self.lbl_halted.configure(
                text=f"{_t('halted')}: {_t('halted_yes')}  ({_t('halted_reset_label')})"
            )
        else:
            self.lbl_halted.configure(text=f"{_t('halted')}: {_t('halted_no')}")
        recon = read_json(RECONCILE_FILE)
        if recon:
            badge = "🟢" if recon.get("ok") else "⚠️"
            self.lbl_recon.configure(text=f"{_t('reconcile')}: {badge} {recon.get('summary', '—')}")
        else:
            self.lbl_recon.configure(text=_t("reconcile_none"))

        # Strategy / config panel
        self.lbl_ai.configure(text=f"{_t('ai_model')}: {account.get('ai_model', '—')}")
        self.lbl_thresh.configure(text=f"{_t('entry_threshold')} {account.get('entry_threshold', '—')}")
        self.lbl_interval.configure(text=f"{_t('scan_every')} {account.get('scan_interval_min', '—')}m")
        self.lbl_hold.configure(text=f"{_t('max_hold')} {account.get('max_hold_days', '—')}d")
        vix_val = account.get("vix", 0)
        if vix_val and vix_val > 0:
            vix_badge = "🔴" if vix_val > 35 else ("🟡" if vix_val > 25 else "🟢")
            self.lbl_vix.configure(text=f"VIX: {vix_badge} {vix_val:.1f}")
        else:
            self.lbl_vix.configure(text="VIX: —")

        ml_enabled = account.get("ml_enabled", False)
        ml_avail = account.get("ml_available", False)
        ml_w = account.get("ml_blend_weight", 0)
        if ml_enabled and ml_avail:
            self.lbl_ml.configure(text=_t("ml_on", weight=ml_w))
        elif ml_enabled and not ml_avail:
            self.lbl_ml.configure(text=_t("ml_no_model"))
        else:
            self.lbl_ml.configure(text=_t("ml_off"))

        # Strategy breakdown (last 30 closed trades by strategy tag)
        ts = account.get("trade_stats", {}) or {}
        strats = ts.get("by_strategy") or {}
        if strats:
            parts = []
            for name in ("trend", "mean_revert"):
                if name in strats:
                    s = strats[name]
                    label = _t(f"strategy_{name if name=='trend' else 'mr'}")
                    parts.append(f"{label} {s.get('n', 0)}/{s.get('wins', 0)}W")
            self.lbl_strat.configure(text="🎯 " + " | ".join(parts) if parts else "🎯 —")
        else:
            self.lbl_strat.configure(text="🎯 —")
        env = account.get("trade_env", "—")
        env_badge = _t("env_paper") if env == "SIMULATE" else (_t("env_real") if env == "REAL" else "—")
        self.lbl_env.configure(text=f"{_t('env')}: {env_badge}")

        self._tail_log()
        self.after(REFRESH_MS, self._refresh)

    def _tail_log(self) -> None:
        if not LOG_FILE.exists():
            return
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8000))
                tail = f.read().decode(errors="replace")
        except OSError:
            return
        # Strip noisy moomoo SDK lines so the tail stays readable.
        keep = [
            ln for ln in tail.splitlines()[-200:]
            if not any(s in ln for s in (
                "open_context_base", "on_disconnect", "New connect",
                "quota_metric", "GenerateRequests", "GenerateContent",
                "violations", "key:", "value:", "retry_delay",
                "Please retry", "description:", "url:", "links {",
            ))
        ]
        body = "\n".join(keep[-80:])
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", body)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _toast(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"\n[GUI] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _reset_halt(self) -> None:
        """Click the Halted label → confirm + clear halted state."""
        # Only proceed if currently halted (no-op when ✓ no)
        state = read_json(STATE_FILE)
        if not state.get("halted"):
            return
        if not _confirm(self,
                        _t("halted_reset_confirm_title"),
                        _t("halted_reset_confirm_body")):
            return
        try:
            from src import db
            db.atomic_state(lambda s: {"halted": False, "loss_streak_days": 0})
            self._toast(_t("halted_reset_done"))
        except Exception as e:
            self._toast(f"reset halt failed: {e}")

    def _force_clock_sync(self) -> None:
        """Click the clock label → force a fresh network time check."""
        threading.Thread(target=self._do_clock_sync, daemon=True).start()

    def _do_clock_sync(self) -> None:
        try:
            from src import clock
            status = clock.force_refresh()
            msg = (f"Clock sync: src={status['source']}  "
                   f"drift={status['last_drift_sec']:+.1f}s  "
                   f"offset_applied={status['offset_sec']:+.1f}s")
            self.after(0, lambda: self._toast(msg))
        except Exception as e:
            self.after(0, lambda: self._toast(f"Clock sync failed: {e}"))

    def open_backtest(self) -> None:
        BacktestWindow(self)

    def open_equity(self) -> None:
        EquityWindow(self)

    def open_audit(self) -> None:
        AuditWindow(self)

    def open_watchlist(self) -> None:
        WatchlistWindow(self)

    def open_sectors(self) -> None:
        SectorHeatmapWindow(self)

    def open_ml(self) -> None:
        MLWindow(self)

    # ---------- right-click context menu actions ----------
    def _on_pos_right_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        try:
            self.pos_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.pos_menu.grab_release()

    def _selected_symbol(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0], "values")
        return vals[0] if vals else None

    def _ctx_close(self) -> None:
        sym = self._selected_symbol()
        if not sym:
            return
        if not _confirm(self, _t("confirm_close_title", symbol=sym),
                        _t("confirm_close_body")):
            return
        self.btn_scan.configure(state="disabled")
        threading.Thread(target=self._do_manual_close, args=(sym,), daemon=True).start()

    def _do_manual_close(self, sym: str) -> None:
        try:
            from src.moomoo_client import client
            from src.executor import manual_close
            with client() as c:
                res = manual_close(c, sym)
            self.after(0, lambda: self._toast(
                _t("closed_toast", symbol=res["symbol"], price=res["price"], pnl=res["pnl"])
            ))
        except Exception as e:
            self.after(0, lambda: self._toast(_t("close_failed_toast", symbol=sym, error=e)))
        finally:
            self.after(0, lambda: self.btn_scan.configure(state="normal"))

    def _ctx_edit_stop(self) -> None:
        sym = self._selected_symbol()
        if not sym:
            return
        trades = read_json(OPEN_TRADES)
        if sym not in trades:
            self._toast(_t("not_in_open_trades", symbol=sym))
            return
        current = trades[sym].get("stop_loss", 0)
        new_val = _prompt_float(self, _t("edit_stop_title", symbol=sym),
                                _t("edit_stop_body", current=current), current)
        if new_val is None or new_val <= 0:
            return
        threading.Thread(target=self._do_edit_stop, args=(sym, new_val), daemon=True).start()

    def _do_edit_stop(self, sym: str, new_val: float) -> None:
        try:
            from src.moomoo_client import client
            from src.executor import edit_stop
            with client() as c:
                edit_stop(c, sym, new_val)
            self.after(0, lambda: self._toast(_t("stop_set_toast", symbol=sym, value=new_val)))
        except Exception as e:
            self.after(0, lambda: self._toast(_t("stop_failed_toast", error=e)))


# ---------- Backtest window ----------

class BacktestWindow(tk.Toplevel):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Backtest")
        self.geometry("820x620")
        self.resizable(True, True)
        self._running = False
        self._build()
        # Auto-load last results if available
        if BACKTEST_FILE.exists():
            self._load_saved()

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Config bar
        cfg = ttk.LabelFrame(self, text="Configuration")
        cfg.pack(fill="x", **pad)

        ttk.Label(cfg, text="Days:").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        self.var_days = tk.StringVar(value="180")
        days_cb = ttk.Combobox(cfg, textvariable=self.var_days, values=["60", "90", "180", "365"], width=6, state="readonly")
        days_cb.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        ttk.Label(cfg, text="Timeframe:").grid(row=0, column=2, padx=6, sticky="e")
        self.var_tf = tk.StringVar(value="HOUR_1")
        tf_cb = ttk.Combobox(cfg, textvariable=self.var_tf, values=["HOUR_1", "DAILY"], width=8, state="readonly")
        tf_cb.grid(row=0, column=3, padx=4, pady=4, sticky="w")

        ttk.Label(cfg, text="Threshold:").grid(row=0, column=4, padx=6, sticky="e")
        self.var_thresh = tk.StringVar(value="70")
        ttk.Entry(cfg, textvariable=self.var_thresh, width=5).grid(row=0, column=5, padx=4, pady=4, sticky="w")

        ttk.Label(cfg, text="Tickers (comma, blank=watchlist):").grid(row=0, column=6, padx=6, sticky="e")
        self.var_tickers = tk.StringVar(value="")
        ttk.Entry(cfg, textvariable=self.var_tickers, width=22).grid(row=0, column=7, padx=4, pady=4, sticky="w")

        self.btn_run = ttk.Button(cfg, text="▶ Run Backtest", command=self._run)
        self.btn_run.grid(row=0, column=8, padx=10, pady=4)

        # Progress
        prog = ttk.Frame(self)
        prog.pack(fill="x", padx=8, pady=2)
        self.lbl_prog = ttk.Label(prog, text="Ready. (Load previous results or click Run.)")
        self.lbl_prog.pack(side="left")
        self.progressbar = ttk.Progressbar(prog, mode="indeterminate", length=180)
        self.progressbar.pack(side="right", padx=6)

        # Notebook for results
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)

        # Tab 1: Summary
        self.tab_summary = ttk.Frame(self.nb)
        self.nb.add(self.tab_summary, text="Summary")
        self.txt_summary = tk.Text(self.tab_summary, wrap="word", font=("Menlo", 12), state="disabled")
        sb = ttk.Scrollbar(self.tab_summary, orient="vertical", command=self.txt_summary.yview)
        self.txt_summary.configure(yscrollcommand=sb.set)
        self.txt_summary.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Tab 2: Monthly PnL
        self.tab_monthly = ttk.Frame(self.nb)
        self.nb.add(self.tab_monthly, text="Monthly PnL")
        mcols = ("month", "pnl")
        self.mtree = ttk.Treeview(self.tab_monthly, columns=mcols, show="headings", height=20)
        for c, w in zip(mcols, (120, 140)):
            self.mtree.heading(c, text=c.upper())
            self.mtree.column(c, width=w, anchor="center")
        msb = ttk.Scrollbar(self.tab_monthly, orient="vertical", command=self.mtree.yview)
        self.mtree.configure(yscrollcommand=msb.set)
        self.mtree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        msb.pack(side="right", fill="y", pady=4)

        # Tab 3: Trade log
        self.tab_trades = ttk.Frame(self.nb)
        self.nb.add(self.tab_trades, text="All Trades")
        tcols = ("date", "symbol", "entry", "exit", "pnl%", "pnl$", "reason", "score")
        twidths = (90, 65, 75, 75, 65, 75, 80, 55)
        self.ttree = ttk.Treeview(self.tab_trades, columns=tcols, show="headings", height=20)
        for c, w in zip(tcols, twidths):
            self.ttree.heading(c, text=c.upper())
            self.ttree.column(c, width=w, anchor="center")
        tsb = ttk.Scrollbar(self.tab_trades, orient="vertical", command=self.ttree.yview)
        self.ttree.configure(yscrollcommand=tsb.set)
        self.ttree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        tsb.pack(side="right", fill="y", pady=4)

        # Tab 4: Per-symbol
        self.tab_sym = ttk.Frame(self.nb)
        self.nb.add(self.tab_sym, text="By Symbol")
        scols = ("symbol", "trades", "wins", "win_rate", "pnl")
        swidths = (90, 70, 60, 80, 100)
        self.stree = ttk.Treeview(self.tab_sym, columns=scols, show="headings", height=20)
        for c, w in zip(scols, swidths):
            self.stree.heading(c, text=c.upper())
            self.stree.column(c, width=w, anchor="center")
        ssb = ttk.Scrollbar(self.tab_sym, orient="vertical", command=self.stree.yview)
        self.stree.configure(yscrollcommand=ssb.set)
        self.stree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        ssb.pack(side="right", fill="y", pady=4)

    def _run(self) -> None:
        if self._running:
            return
        if not opend_reachable():
            self.lbl_prog.configure(text="OpenD not reachable — start OpenD first.")
            return
        self._running = True
        self.btn_run.configure(state="disabled", text="Running…")
        self.progressbar.start(12)
        self._clear_results()
        threading.Thread(target=self._do_run, daemon=True).start()

    def _do_run(self) -> None:
        try:
            days = int(self.var_days.get())
            tf = self.var_tf.get()
            thresh = float(self.var_thresh.get())
            raw = self.var_tickers.get().strip()
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()] if raw else []

            from src.backtest import BacktestConfig, run_backtest
            from src.config import settings

            cfg = BacktestConfig(
                days=days,
                timeframe=tf,
                threshold=thresh,
                tickers=tickers,
                account_usd=settings.account_usd,
                risk_per_trade=settings.risk_per_trade,
                max_position_pct=settings.max_position_pct,
                max_hold_days=settings.max_hold_days,
            )

            n_tickers = len(tickers) or len(json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"])

            def progress(cur, total, sym):
                self.after(0, lambda c=cur, t=total, s=sym: self.lbl_prog.configure(
                    text=f"Fetching {s}… ({c+1}/{t})"
                ))

            result = run_backtest(cfg, progress_cb=progress)
            self.after(0, lambda: self._show_results(result))
        except Exception as e:
            self.after(0, lambda: self.lbl_prog.configure(text=f"Error: {e}"))
        finally:
            self.after(0, self._finish_run)

    def _finish_run(self) -> None:
        self._running = False
        self.progressbar.stop()
        self.btn_run.configure(state="normal", text="▶ Run Backtest")

    def _load_saved(self) -> None:
        try:
            result = json.loads(BACKTEST_FILE.read_text())
            self._show_results(result)
            cfg = result.get("config", {})
            self.lbl_prog.configure(
                text=f"Loaded saved results from {result.get('generated_at', '?')} "
                     f"({cfg.get('timeframe','?')}, {cfg.get('days','?')} days)"
            )
        except Exception:
            pass

    def _clear_results(self) -> None:
        self.txt_summary.configure(state="normal")
        self.txt_summary.delete("1.0", "end")
        self.txt_summary.configure(state="disabled")
        self.mtree.delete(*self.mtree.get_children())
        self.ttree.delete(*self.ttree.get_children())
        self.stree.delete(*self.stree.get_children())

    def _show_results(self, result: dict) -> None:
        m = result.get("metrics", {})
        cfg = result.get("config", {})
        trades = result.get("trades", [])

        # Summary text
        lines = [
            f"Backtest: {cfg.get('timeframe')}  |  {cfg.get('days')} days  |  threshold={cfg.get('threshold')}",
            f"Tickers: {len(cfg.get('tickers', []))} (or watchlist)",
            f"Generated: {result.get('generated_at', '?')}",
            "",
        ]
        if m.get("total_trades", 0) == 0:
            lines.append("No trades generated. Try lowering the threshold.")
        else:
            lines += [
                f"Total trades      : {m['total_trades']}",
                f"Win rate          : {m['win_rate_pct']}%",
                f"Profit factor     : {m['profit_factor']}",
                f"Net PnL           : ${m['net_pnl_usd']:+,.2f}",
                f"Gross profit      : ${m['gross_profit_usd']:,.2f}",
                f"Gross loss        : ${m['gross_loss_usd']:,.2f}",
                f"Avg win           : {m['avg_win_pct']:+.2f}%",
                f"Avg loss          : {m['avg_loss_pct']:+.2f}%",
                f"Max drawdown      : ${m['max_drawdown_usd']:,.2f}",
                f"Sharpe ratio      : {m['sharpe_ratio']}",
                "",
                "Exit breakdown:",
            ]
            for reason, count in sorted(m.get("exit_reasons", {}).items()):
                lines.append(f"  {reason:<12}: {count}")
            if result.get("errors"):
                lines += ["", f"Errors ({len(result['errors'])}):", *result["errors"][:5]]

        self.txt_summary.configure(state="normal")
        self.txt_summary.delete("1.0", "end")
        self.txt_summary.insert("1.0", "\n".join(lines))
        self.txt_summary.configure(state="disabled")

        # Monthly PnL
        self.mtree.delete(*self.mtree.get_children())
        for month, pnl in m.get("monthly_pnl", {}).items():
            bar = "+" * min(15, max(0, int(abs(pnl) / 5))) if pnl >= 0 else "-" * min(15, max(0, int(abs(pnl) / 5)))
            self.mtree.insert("", "end", values=(month, f"${pnl:+,.2f}  {bar}"))

        # Trade log
        self.ttree.delete(*self.ttree.get_children())
        for t in trades:
            pnl_color = "green" if t["pnl"] >= 0 else "red"
            self.ttree.insert("", "end", values=(
                t["entry_date"],
                t["symbol"],
                f"${t['entry_price']:.2f}",
                f"${t['exit_price']:.2f}",
                f"{t['pnl_pct']:+.2f}%",
                f"${t['pnl']:+.2f}",
                t["exit_reason"],
                f"{t['score']:.0f}",
            ), tags=(pnl_color,))
        self.ttree.tag_configure("green", foreground="#006600")
        self.ttree.tag_configure("red", foreground="#aa0000")

        # Per-symbol
        self.stree.delete(*self.stree.get_children())
        by_sym = m.get("by_symbol", {})
        ranked = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)
        for sym, s in ranked:
            wr = round(s["wins"] / s["trades"] * 100) if s["trades"] else 0
            self.stree.insert("", "end", values=(
                sym, s["trades"], s["wins"], f"{wr}%", f"${s['pnl']:+,.2f}"
            ))

        self.lbl_prog.configure(
            text=f"Done. {m.get('total_trades', 0)} trades, "
                 f"win rate {m.get('win_rate_pct', 0)}%, "
                 f"net PnL ${m.get('net_pnl_usd', 0):+,.2f}"
        )


# ---------- helpers ----------

def _confirm(parent, title: str, msg: str) -> bool:
    from tkinter import messagebox
    return messagebox.askyesno(title, msg, parent=parent)


def _prompt_float(parent, title: str, msg: str, default: float) -> float | None:
    from tkinter import simpledialog
    return simpledialog.askfloat(title, msg, parent=parent, initialvalue=default)


def _load_trades_jsonl() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    rows = []
    for line in TRADES_FILE.read_text().strip().split("\n"):
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _load_audit_jsonl(limit: int = 500) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    rows = []
    for line in AUDIT_FILE.read_text().strip().split("\n")[-limit:]:
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


# ---------- Equity curve window (matplotlib) ----------

class EquityWindow(tk.Toplevel):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Equity Curve")
        self.geometry("900x600")
        self._build()
        self._refresh()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="Source:").pack(side="left")
        self.var_src = tk.StringVar(value="closed_trades")
        ttk.Combobox(top, textvariable=self.var_src,
                     values=["closed_trades", "snapshot_history"],
                     width=20, state="readonly").pack(side="left", padx=6)
        ttk.Button(top, text="🔄 Refresh", command=self._refresh).pack(side="left", padx=4)
        self.lbl_stats = ttk.Label(top, text="—", font=("SF Pro", 11))
        self.lbl_stats.pack(side="right", padx=8)

        # Embed matplotlib figure
        import matplotlib
        matplotlib.use("TkAgg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self.fig = Figure(figsize=(8, 4.8), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

    def _refresh(self) -> None:
        src = self.var_src.get()
        self.fig.clear()
        if src == "closed_trades":
            trades = _load_trades_jsonl()
            if not trades:
                self.lbl_stats.configure(text="No closed trades yet")
                self.canvas.draw()
                return
            trades.sort(key=lambda r: r.get("ts", ""))
            equity = [0.0]
            for t in trades:
                equity.append(equity[-1] + t.get("pnl", 0))
            self._plot_equity(equity, [t["ts"][:10] for t in trades])
            wins = sum(1 for t in trades if t["pnl"] > 0)
            wr = round(wins / len(trades) * 100, 1)
            tot = round(sum(t["pnl"] for t in trades), 2)
            avg_r = round(sum(t.get("r_multiple", 0) for t in trades) / len(trades), 2)
            self.lbl_stats.configure(
                text=f"{len(trades)} trades  |  WR {wr}%  |  avg R {avg_r:+.2f}  |  net ${tot:+,.2f}"
            )
        else:
            # Snapshot history (history.jsonl)
            if not HISTORY_FILE.exists():
                self.lbl_stats.configure(text="No snapshot history")
                self.canvas.draw()
                return
            rows = []
            for line in HISTORY_FILE.read_text().strip().split("\n"):
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            if not rows:
                self.lbl_stats.configure(text="No snapshot history")
                self.canvas.draw()
                return
            equity = [r.get("total_pnl", 0) for r in rows]
            labels = [r.get("ts", "")[:10] for r in rows]
            self._plot_equity(equity, labels)
            last = equity[-1] if equity else 0
            self.lbl_stats.configure(text=f"{len(rows)} snapshots  |  last total PnL ${last:+,.2f}")
        self.canvas.draw()

    def _plot_equity(self, equity: list[float], labels: list[str]) -> None:
        ax = self.fig.add_subplot(111)
        ax.plot(equity, linewidth=1.6, color="#1f6feb")
        ax.fill_between(range(len(equity)), equity, 0,
                        where=[e >= 0 for e in equity],
                        color="#1f6feb", alpha=0.10)
        # Drawdown shading
        peak = 0
        dd = []
        for e in equity:
            peak = max(peak, e)
            dd.append(e - peak)
        ax2 = ax.twinx()
        ax2.fill_between(range(len(dd)), dd, 0, color="#d33", alpha=0.18, label="Drawdown")
        ax2.set_ylabel("Drawdown ($)", color="#aa3333")
        ax2.tick_params(axis="y", labelcolor="#aa3333")

        ax.set_ylabel("Cumulative PnL ($)")
        ax.set_xlabel("Trades / snapshots →")
        ax.grid(True, alpha=0.3)
        ax.axhline(0, color="#888", linewidth=0.7)
        if labels and len(labels) > 8:
            step = max(1, len(labels) // 8)
            ax.set_xticks(range(0, len(labels), step))
            ax.set_xticklabels([labels[i] for i in range(0, len(labels), step)],
                               rotation=30, fontsize=8)
        self.fig.tight_layout()


# ---------- AI / Audit panel ----------

class AuditWindow(tk.Toplevel):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("AI Decisions & Audit Log")
        self.geometry("900x600")
        self._build()
        self._refresh()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Button(top, text="🔄 Refresh", command=self._refresh).pack(side="left", padx=4)
        ttk.Label(top, text="  Filter:").pack(side="left")
        self.var_filter = tk.StringVar(value="all")
        ttk.Combobox(top, textvariable=self.var_filter,
                     values=["all", "buy", "skip", "error"],
                     width=10, state="readonly").pack(side="left", padx=4)
        self.var_filter.trace_add("write", lambda *_: self._refresh())
        self.lbl_summary = ttk.Label(top, text="—", font=("SF Pro", 11))
        self.lbl_summary.pack(side="right", padx=8)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=4)

        # Tab 1: Decisions table
        t1 = ttk.Frame(nb)
        nb.add(t1, text="Decisions")
        cols = ("ts", "action", "symbol", "gate", "reason", "score")
        widths = (130, 60, 70, 80, 360, 60)
        self.tree = ttk.Treeview(t1, columns=cols, show="headings", height=18)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c.upper())
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(t1, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("buy", foreground="#006600")
        self.tree.tag_configure("skip", foreground="#666666")
        self.tree.tag_configure("error", foreground="#aa0000")

        # Tab 2: Skip-gate summary
        t2 = ttk.Frame(nb)
        nb.add(t2, text="Skip Gate Stats")
        cols2 = ("gate", "count", "pct")
        self.gtree = ttk.Treeview(t2, columns=cols2, show="headings", height=18)
        for c, w in zip(cols2, (140, 100, 100)):
            self.gtree.heading(c, text=c.upper())
            self.gtree.column(c, width=w, anchor="center")
        self.gtree.pack(fill="both", expand=True, padx=6, pady=6)

    def _refresh(self) -> None:
        rows = _load_audit_jsonl(1000)
        f = self.var_filter.get()
        filtered = rows if f == "all" else [r for r in rows if r.get("action") == f]

        self.tree.delete(*self.tree.get_children())
        for r in filtered[-300:]:
            self.tree.insert(
                "", "end",
                values=(
                    r.get("ts", "")[:19].replace("T", " "),
                    r.get("action", ""),
                    r.get("symbol", ""),
                    r.get("gate", ""),
                    r.get("reason", "")[:60],
                    r.get("score", 0),
                ),
                tags=(r.get("action", ""),),
            )

        # Summary
        buys = [r for r in rows if r.get("action") == "buy"]
        skips = [r for r in rows if r.get("action") == "skip"]
        errs = [r for r in rows if r.get("action") == "error"]
        self.lbl_summary.configure(
            text=f"{len(rows)} events  |  {len(buys)} buys  |  {len(skips)} skips  |  {len(errs)} errors"
        )

        # Skip-gate stats
        self.gtree.delete(*self.gtree.get_children())
        gate_counts: dict = {}
        for r in skips:
            g = r.get("gate", "?")
            gate_counts[g] = gate_counts.get(g, 0) + 1
        total_skips = sum(gate_counts.values()) or 1
        for g, c in sorted(gate_counts.items(), key=lambda x: -x[1]):
            self.gtree.insert("", "end", values=(g, c, f"{c/total_skips*100:.0f}%"))


# ---------- Watchlist editor ----------

class WatchlistWindow(tk.Toplevel):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Watchlist Editor")
        self.geometry("420x540")
        self._build()
        self._load()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Symbol:").pack(side="left")
        self.var_new = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.var_new, width=12)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _: self._add())
        ttk.Button(top, text="➕ Add", command=self._add).pack(side="left", padx=4)
        ttk.Button(top, text="🗑 Remove", command=self._remove).pack(side="left", padx=4)
        ttk.Button(top, text="💾 Save", command=self._save).pack(side="right", padx=4)

        self.list = tk.Listbox(self, font=("Menlo", 12), selectmode="extended")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=sb.set)
        self.list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        sb.pack(side="right", fill="y", pady=4)

        self.lbl_status = ttk.Label(self, text="", font=("SF Pro", 10))
        self.lbl_status.pack(side="bottom", fill="x", padx=8, pady=2)

    def _load(self) -> None:
        if not WATCHLIST_FILE.exists():
            return
        try:
            data = json.loads(WATCHLIST_FILE.read_text())
            for t in data.get("tickers", []):
                self.list.insert("end", t)
            self.lbl_status.configure(text=f"Loaded {self.list.size()} tickers")
        except Exception as e:
            self.lbl_status.configure(text=f"Load error: {e}")

    def _add(self) -> None:
        sym = self.var_new.get().strip().upper()
        if not sym:
            return
        existing = set(self.list.get(0, "end"))
        if sym in existing:
            self.lbl_status.configure(text=f"{sym} already in list")
            return
        self.list.insert("end", sym)
        self.var_new.set("")
        self.lbl_status.configure(text=f"Added {sym}")

    def _remove(self) -> None:
        sel = list(self.list.curselection())
        for i in reversed(sel):
            self.list.delete(i)
        self.lbl_status.configure(text=f"Removed {len(sel)} ticker(s)")

    def _save(self) -> None:
        tickers = list(self.list.get(0, "end"))
        try:
            WATCHLIST_FILE.write_text(json.dumps({"tickers": tickers}, indent=2))
            self.lbl_status.configure(text=f"✓ Saved {len(tickers)} tickers — restart scheduler to apply")
        except Exception as e:
            self.lbl_status.configure(text=f"Save failed: {e}")


# ---------- Sector heat-map ----------

class SectorHeatmapWindow(tk.Toplevel):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Sector Heat-map")
        self.geometry("760x480")
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Watchlist tickers grouped by sector — exposure = held positions.",
                  font=("SF Pro", 11)).pack(anchor="w", padx=12, pady=6)
        self.canvas = tk.Canvas(self, bg="white")
        self.canvas.pack(fill="both", expand=True, padx=10, pady=8)
        self.after(50, self._draw)

    def _draw(self) -> None:
        from src.sector import SECTOR_MAP, MAX_PER_SECTOR
        if not WATCHLIST_FILE.exists():
            return
        watchlist = json.loads(WATCHLIST_FILE.read_text())["tickers"]
        trades = read_json(OPEN_TRADES)
        held = set(trades.keys())

        groups: dict = {}
        for sym in watchlist:
            sect = SECTOR_MAP.get(sym.upper(), "unknown")
            groups.setdefault(sect, []).append(sym)

        self.canvas.delete("all")
        w = self.canvas.winfo_width() or 740
        col_w = max(140, w // max(1, len(groups)))
        x = 10
        for sect, syms in sorted(groups.items(), key=lambda x: -len(x[1])):
            in_sector = [s for s in syms if s in held]
            ratio = len(in_sector) / max(1, MAX_PER_SECTOR)
            # Colour: green low, yellow mid, red full
            if ratio == 0:
                fill = "#e8f5e9"
            elif ratio < 1:
                fill = "#fff8e1"
            else:
                fill = "#ffcdd2"
            self.canvas.create_rectangle(x, 10, x + col_w - 10, 40, fill=fill, outline="#888")
            self.canvas.create_text(x + 6, 22, anchor="w",
                                    text=f"{sect}  ({len(in_sector)}/{MAX_PER_SECTOR})",
                                    font=("SF Pro", 11, "bold"))
            y = 56
            for sym in syms:
                color = "#0a5" if sym in held else "#555"
                weight = "bold" if sym in held else "normal"
                self.canvas.create_text(x + 6, y, anchor="w", text=sym,
                                        fill=color, font=("Menlo", 11, weight))
                y += 18
            x += col_w


# ---------- ML model window ----------

class MLWindow(tk.Toplevel):
    META_FILE = ROOT / "data" / "ml" / "model_meta.json"

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("ML Model")
        self.geometry("760x600")
        self._build()
        self._refresh()

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        self.lbl_status = ttk.Label(top, text="—", font=("SF Pro", 12, "bold"))
        self.lbl_status.pack(side="left")
        ttk.Button(top, text="🔄 Refresh", command=self._refresh).pack(side="right", padx=4)
        ttk.Button(top, text="🧠 Train Now", command=self._train).pack(side="right", padx=4)

        # Notebook: Overview + Calibration
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=4)

        # Tab 1: Overview (metrics + feature importance)
        tab_ov = ttk.Frame(nb)
        nb.add(tab_ov, text=_t("ml_tab_overview"))
        self.txt_metrics = tk.Text(tab_ov, height=8, font=("Menlo", 11), wrap="word")
        self.txt_metrics.pack(fill="x", padx=4, pady=4)
        self.txt_metrics.configure(state="disabled")
        ttk.Label(tab_ov, text="Feature importance (top 20)",
                  font=("SF Pro", 11, "bold")).pack(anchor="w", padx=8, pady=(8, 0))
        self.fi_canvas = tk.Canvas(tab_ov, bg="white", height=380)
        self.fi_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        # Tab 2: Calibration (predicted proba vs actual outcome)
        tab_cal = ttk.Frame(nb)
        nb.add(tab_cal, text=_t("ml_tab_calibration"))
        self.lbl_calib_brier = ttk.Label(tab_cal, text="—", font=("SF Pro", 11, "bold"))
        self.lbl_calib_brier.pack(anchor="w", padx=8, pady=(8, 0))
        ttk.Label(tab_cal, text=_t("ml_calibration_title"),
                  font=("SF Pro", 11)).pack(anchor="w", padx=8)
        cal_cols = ("range", "n", "mean_proba", "winrate", "diff", "mean_r")
        cal_widths = (110, 60, 100, 90, 80, 80)
        self.cal_tree = ttk.Treeview(tab_cal, columns=cal_cols, show="headings", height=8)
        for c, w in zip(cal_cols, cal_widths):
            self.cal_tree.heading(c, text=c.upper())
            self.cal_tree.column(c, width=w, anchor="center")
        self.cal_tree.pack(fill="x", padx=8, pady=8)
        self.lbl_calib_note = ttk.Label(tab_cal, text="", wraplength=720,
                                         font=("SF Pro", 10), foreground="#555")
        self.lbl_calib_note.pack(anchor="w", padx=8, pady=8)

    def _refresh(self) -> None:
        if not self.META_FILE.exists():
            self._set_status("⚪ No trained model. Click 'Train Now' to build one.", warn=True)
            self._set_metrics("Train command: python -m src.ml.train --days 365")
            self.fi_canvas.delete("all")
            return
        try:
            meta = json.loads(self.META_FILE.read_text())
        except json.JSONDecodeError as e:
            self._set_status(f"⚠ meta corrupt: {e}", warn=True)
            return

        trained = meta.get("trained_at", "?")
        acc = meta.get("accuracy_holdout", 0)
        auc = meta.get("auc_holdout", 0)
        n = meta.get("n_rows_total", 0)
        pr = meta.get("positive_rate_holdout", 0)
        lc = meta.get("label_config", {})
        self._set_status(f"🟢 Trained {trained[:19]}   ({n:,} rows)")
        lines = [
            f"Holdout accuracy : {acc*100:.1f}%",
            f"Holdout AUC      : {auc:.3f}",
            f"Positive rate    : {pr*100:.1f}%",
            f"Logloss          : {meta.get('logloss_holdout', 0):.3f}",
            f"Best iteration   : {meta.get('best_iteration', '?')}",
            f"Label config     : horizon={lc.get('horizon_bars','?')} bars, "
            f"TP={lc.get('tp_pct',0)*100:.1f}%, SL={lc.get('sl_pct',0)*100:.1f}%",
        ]
        self._set_metrics("\n".join(lines))
        self._draw_feature_importance(meta.get("feature_importance", {}))
        self._refresh_calibration()

    def _refresh_calibration(self) -> None:
        try:
            from src.ml.calibration import load as load_calib, compute as compute_calib
            data = load_calib()
            if not data.get("overall", {}).get("total_trades_with_proba", 0):
                # Try recomputing once in case the file is stale
                data = compute_calib()
        except Exception:
            data = {"overall": {"total_trades_with_proba": 0}, "buckets": []}

        overall = data.get("overall", {})
        n = overall.get("total_trades_with_proba", 0)
        brier = overall.get("brier_score")
        if n == 0:
            self.lbl_calib_brier.configure(text=f"n=0")
            self.lbl_calib_note.configure(text=_t("ml_calibration_no_data"))
        else:
            brier_str = f"{brier:.3f}" if brier is not None else "—"
            self.lbl_calib_brier.configure(
                text=f"{_t('ml_brier_score')}: {brier_str}  {_t('ml_brier_hint')}  |  n={n}"
            )
            self.lbl_calib_note.configure(text="")

        self.cal_tree.delete(*self.cal_tree.get_children())
        for b in data.get("buckets", []):
            wr = b.get("winrate")
            mp = b.get("mean_proba")
            diff = (wr - mp) if (wr is not None and mp is not None) else None
            self.cal_tree.insert(
                "", "end",
                values=(
                    b.get("range", "—"),
                    b.get("n", 0),
                    f"{mp:.2f}" if mp is not None else "—",
                    f"{wr*100:.0f}%" if wr is not None else "—",
                    f"{diff*100:+.0f}%" if diff is not None else "—",
                    f"{b.get('mean_r', 0):+.2f}" if b.get('mean_r') is not None else "—",
                ),
            )

    def _set_status(self, msg: str, warn: bool = False) -> None:
        self.lbl_status.configure(text=msg)

    def _set_metrics(self, msg: str) -> None:
        self.txt_metrics.configure(state="normal")
        self.txt_metrics.delete("1.0", "end")
        self.txt_metrics.insert("1.0", msg)
        self.txt_metrics.configure(state="disabled")

    def _draw_feature_importance(self, fi: dict) -> None:
        self.fi_canvas.delete("all")
        items = list(fi.items())[:20]
        if not items:
            return
        max_imp = max(v for _, v in items) or 1
        w = self.fi_canvas.winfo_width() or 740
        bar_w = w - 200
        y = 8
        for name, imp in items:
            self.fi_canvas.create_text(5, y + 8, anchor="w", text=name,
                                       font=("Menlo", 10))
            bw = int(bar_w * imp / max_imp)
            self.fi_canvas.create_rectangle(180, y, 180 + bw, y + 16,
                                            fill="#1f6feb", outline="")
            self.fi_canvas.create_text(180 + bw + 6, y + 8, anchor="w",
                                       text=f"{imp:.3f}", font=("Menlo", 10),
                                       fill="#555")
            y += 20

    def _train(self) -> None:
        if not _confirm(self, "Train ML model now?",
                        "This fetches 1 year of klines for the watchlist + "
                        "trains XGBoost. Takes ~3-5 min. Continue?"):
            return
        self._set_status("⏳ training… (see GUI log)")
        threading.Thread(target=self._do_train, daemon=True).start()

    def _do_train(self) -> None:
        try:
            proc = subprocess.run(
                [PYTHON, "-m", "src.ml.train", "--days", "365"],
                cwd=str(ROOT),
                capture_output=True, text=True, timeout=900,
            )
            ok = proc.returncode == 0
            tail = (proc.stdout + proc.stderr)[-1200:]
            self.after(0, lambda: self._toast_train(ok, tail))
        except Exception as e:
            self.after(0, lambda: self._toast_train(False, str(e)))

    def _toast_train(self, ok: bool, tail: str) -> None:
        self._set_status("🟢 trained OK" if ok else "⚠ training failed")
        # Append to metrics box so user can see the report
        self.txt_metrics.configure(state="normal")
        self.txt_metrics.insert("end", "\n\n--- train output ---\n" + tail[-1000:])
        self.txt_metrics.configure(state="disabled")
        if ok:
            self._refresh()


if __name__ == "__main__":
    App().mainloop()
