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
SIGNAL_WL_FILE = ROOT / "config" / "signal_watchlist.json"
SIGNAL_PID_FILE = ROOT / "logs" / "signal_reporter.pid"
SIGNAL_LOG_FILE = ROOT / "logs" / "signal_reporter.log"
PYTHON = str(ROOT / ".venv" / "bin" / "python")
PREFS_FILE = ROOT / "data" / "prefs.json"

REFRESH_MS = 5000

# ====== Theme palettes ======
# Two complete colour sets. The active palette is copied into THEME (below)
# at startup and whenever the user switches. Sub-windows read THEME live,
# so updating THEME in-place is enough for new widgets — existing widgets
# get repainted by _refresh_widget_tree().
LIGHT_PALETTE = {
    "bg":            "#f1f5f9",
    "sidebar_bg":    "#e8edf4",
    "card_bg":       "#ffffff",
    "border":        "#e2e8f0",
    "border2":       "#cbd5e1",
    "primary":       "#0891b2",
    "secondary":     "#7c3aed",
    "success":       "#059669",
    "danger":        "#dc2626",
    "warning":       "#d97706",
    "muted":         "#64748b",
    "text":          "#334155",
    "text_strong":   "#0f172a",
    "tree_head_bg":  "#f1f5f9",
    "select_fg":     "#ffffff",
}

DARK_PALETTE = {
    "bg":            "#0a0f1e",
    "sidebar_bg":    "#060b14",
    "card_bg":       "#111827",
    "border":        "#1f2937",
    "border2":       "#374151",
    "primary":       "#06b6d4",
    "secondary":     "#7c3aed",
    "success":       "#10b981",
    "danger":        "#ef4444",
    "warning":       "#f59e0b",
    "muted":         "#6b7280",
    "text":          "#9ca3af",
    "text_strong":   "#f9fafb",
    "tree_head_bg":  "#1f2937",
    "select_fg":     "#ffffff",
}

# Mutable — never reassigned. All code references THEME["key"] which reads
# the *current* value, so an in-place update propagates everywhere.
THEME: dict = {}
THEME.update(LIGHT_PALETTE)   # boot default

_current_theme_pref = "auto"  # "light" | "dark" | "auto"
_resolved_theme = "light"     # what's actually displayed


# ---------- theme detection ----------

def _detect_macos_dark() -> bool:
    """True if macOS is currently in dark appearance.

    `defaults read -g AppleInterfaceStyle` returns "Dark" in dark mode and
    exits non-zero in light mode. Sub-second cost; safe to call on every
    "auto" refresh.
    """
    import sys as _sys
    if _sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0 and "Dark" in r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def detect_system_theme() -> str:
    """Returns 'dark' or 'light' based on OS preference."""
    return "dark" if _detect_macos_dark() else "light"


# ---------- theme preference (persisted in data/prefs.json) ----------

def load_theme_pref() -> str:
    """Read theme pref from prefs.json. 'auto' is the default."""
    try:
        prefs = json.loads(PREFS_FILE.read_text())
        pref = prefs.get("theme", "auto")
        if pref not in ("light", "dark", "auto"):
            pref = "auto"
        return pref
    except (FileNotFoundError, json.JSONDecodeError):
        return "auto"


def save_theme_pref(name: str) -> None:
    """Write theme pref to prefs.json (preserves other keys like `lang`)."""
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(PREFS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        prefs = {}
    prefs["theme"] = name
    PREFS_FILE.write_text(json.dumps(prefs, indent=2, ensure_ascii=False))


# ---------- theme application ----------

def _resolve_theme(pref: str) -> str:
    return detect_system_theme() if pref == "auto" else pref


def apply_theme(pref: str, app=None) -> str:
    """Apply theme `pref` globally. Returns the resolved theme name.

    Mutates THEME in-place so all code reading THEME["..."] sees new values
    on the next paint. When `app` is provided, re-runs ttk style setup +
    walks the widget tree to repaint Tk-native widgets that don't follow
    styles (Toplevel / Text / Listbox / Canvas).
    """
    global _current_theme_pref, _resolved_theme
    _current_theme_pref = pref
    resolved = _resolve_theme(pref)
    _resolved_theme = resolved
    palette = DARK_PALETTE if resolved == "dark" else LIGHT_PALETTE
    THEME.clear()
    THEME.update(palette)

    if app is not None:
        app._setup_theme()
        _refresh_widget_tree(app)
    return resolved


def _refresh_widget_tree(root) -> None:
    """Walk root + all descendants and update colours for non-ttk widgets.

    ttk widgets follow `ttk.Style` so they refresh automatically when
    `_setup_theme()` re-runs. Plain Tk widgets (Toplevel, Text, Listbox,
    Canvas, Menu) need explicit configure() — that's what this function does.
    """
    queue = [root]
    visited = set()
    while queue:
        w = queue.pop()
        if id(w) in visited:
            continue
        visited.add(id(w))
        cls = w.winfo_class()
        try:
            if cls in ("Toplevel", "Tk"):
                w.configure(bg=THEME["bg"])
            elif cls == "Frame":
                if not getattr(w, '_is_accent', False):
                    w.configure(bg=THEME["bg"])
            elif cls == "Text":
                w.configure(bg=THEME["card_bg"], fg=THEME["text"],
                            insertbackground=THEME["text"])
            elif cls == "Listbox":
                w.configure(bg=THEME["card_bg"], fg=THEME["text"],
                            selectbackground=THEME["primary"],
                            selectforeground=THEME["select_fg"])
            elif cls == "Canvas":
                w.configure(bg=THEME["card_bg"])
            elif cls == "Menu":
                w.configure(bg=THEME["card_bg"], fg=THEME["text"],
                            activebackground=THEME["primary"],
                            activeforeground=THEME["select_fg"])
        except tk.TclError:
            pass
        try:
            queue.extend(w.winfo_children())
        except tk.TclError:
            pass


def _apply_theme(toplevel: tk.Toplevel) -> None:
    """Apply the shared visual theme to a NEW sub-window.

    Called from every Toplevel `__init__`. Sets the background + default
    options for tk-native widgets created later in that window's _build().
    """
    toplevel.configure(bg=THEME["bg"])
    try:
        toplevel.option_add("*Text.background",      THEME["card_bg"])
        toplevel.option_add("*Text.foreground",      THEME["text"])
        toplevel.option_add("*Text.borderWidth",     0)
        toplevel.option_add("*Text.highlightThickness", 0)
        toplevel.option_add("*Text.relief",          "flat")
        toplevel.option_add("*Listbox.background",   THEME["card_bg"])
        toplevel.option_add("*Listbox.foreground",   THEME["text"])
        toplevel.option_add("*Listbox.borderWidth",  0)
        toplevel.option_add("*Listbox.highlightThickness", 0)
        toplevel.option_add("*Listbox.relief",       "flat")
        toplevel.option_add("*Listbox.font",         "Menlo 12")
    except tk.TclError:
        pass


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
    # `App.COLORS` is the live theme reference. It points at the same dict
    # as the module-level THEME, so app.COLORS["bg"] always reads the
    # current value after a theme switch.
    COLORS = THEME

    def __init__(self) -> None:
        super().__init__()
        self.title(_t("app_title"))
        # Sized for 13" MacBook (1280×800 logical) — fits with menu bar + dock.
        self.geometry("1200x740")
        self.minsize(1024, 640)
        # Boot the theme from prefs.json BEFORE we build any widget — that
        # way the first paint already uses the user's preferred colours.
        apply_theme(load_theme_pref(), app=None)
        self.configure(bg=THEME["bg"])
        self._setup_theme()
        self._build_menu()
        self._build()
        self._refresh()

    # ---------- visual theme ----------
    def _setup_theme(self) -> None:
        """Configure ttk Style for the current THEME palette."""
        style = ttk.Style()
        prefer_clam = _resolved_theme == "dark" or style.theme_use() != "aqua"
        if prefer_clam:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

        c = THEME

        # ── Sidebar ──────────────────────────────────────────────
        style.configure("Sidebar.TFrame",   background=c["sidebar_bg"])
        style.configure("Divider.TFrame",   background=c["border"])
        style.configure("SidebarTitle.TLabel",
                        background=c["sidebar_bg"], foreground=c["primary"],
                        font=("SF Pro", 13, "bold"))
        style.configure("SidebarSub.TLabel",
                        background=c["sidebar_bg"], foreground=c["muted"],
                        font=("SF Pro", 9))
        style.configure("SidebarSection.TLabel",
                        background=c["sidebar_bg"], foreground=c["muted"],
                        font=("SF Pro", 9, "bold"), padding=(16, 4, 0, 2))

        # ── Nav buttons ──────────────────────────────────────────
        style.configure("Nav.TButton",
                        padding=(10, 8), font=("SF Pro", 11),
                        relief="flat", anchor="w",
                        background=c["sidebar_bg"], foreground=c["text"],
                        borderwidth=0, focusthickness=0)
        style.map("Nav.TButton",
                  background=[("active", c["card_bg"]), ("pressed", c["border"])],
                  foreground=[("active", c["text_strong"])])
        style.configure("NavAccent.TButton",
                        padding=(10, 8), font=("SF Pro", 11, "bold"),
                        relief="flat", anchor="w",
                        background=c["primary"], foreground="#000000",
                        borderwidth=0, focusthickness=0)
        style.map("NavAccent.TButton",
                  background=[("active", c["success"]), ("pressed", c["success"])],
                  foreground=[("active", "#000000")])

        # ── Cards ────────────────────────────────────────────────
        style.configure("Card.TFrame",      background=c["card_bg"])
        style.configure("CardTitle.TLabel",
                        background=c["card_bg"], foreground=c["muted"],
                        font=("SF Pro", 10))
        style.configure("CardValue.TLabel",
                        background=c["card_bg"], foreground=c["text_strong"],
                        font=("SF Pro", 22, "bold"))
        style.configure("CardMid.TLabel",
                        background=c["card_bg"], foreground=c["text"],
                        font=("Menlo", 12, "bold"))
        style.configure("StatusPill.TLabel",
                        background=c["card_bg"], foreground=c["text"],
                        font=("SF Pro", 10))
        style.configure("InfoPill.TLabel",
                        background=c["card_bg"], foreground=c["text"],
                        font=("SF Pro", 10))

        # ── Section LabelFrames (popup windows) ──────────────────
        style.configure("Section.TLabelframe",
                        background=c["bg"], padding=6)
        style.configure("Section.TLabelframe.Label",
                        background=c["bg"], font=("SF Pro", 11, "bold"),
                        foreground=c["muted"])

        # ── Plain label variants ──────────────────────────────────
        style.configure("Muted.TLabel",
                        background=c["bg"], foreground=c["muted"],
                        font=("SF Pro", 10))
        style.configure("Stat.TLabel",
                        background=c["bg"], foreground=c["text"],
                        font=("Menlo", 11))
        style.configure("StatBold.TLabel",
                        background=c["bg"], foreground=c["text_strong"],
                        font=("Menlo", 11, "bold"))
        style.configure("Money.TLabel",
                        background=c["bg"], foreground=c["text_strong"],
                        font=("SF Pro", 22, "bold"))
        style.configure("MoneyMid.TLabel",
                        background=c["bg"], foreground=c["text"],
                        font=("Menlo", 13, "bold"))
        style.configure("Win.TLabel",
                        background=c["card_bg"], foreground=c["success"],
                        font=("SF Pro", 20, "bold"))
        style.configure("Loss.TLabel",
                        background=c["card_bg"], foreground=c["danger"],
                        font=("SF Pro", 20, "bold"))

        # ── Buttons ───────────────────────────────────────────────
        style.configure("TButton",         padding=(10, 6), font=("SF Pro", 11))
        style.configure("Primary.TButton", padding=(12, 7), font=("SF Pro", 11, "bold"))
        style.configure("Icon.TButton",    padding=(4, 2),  font=("SF Pro", 11))

        # ── Progressbar ───────────────────────────────────────────
        style.configure("Budget.Horizontal.TProgressbar",
                        thickness=8, troughcolor=c["border"],
                        background=c["secondary"])

        # ── Treeview ──────────────────────────────────────────────
        style.configure("Treeview",
                        font=("Menlo", 11), rowheight=24,
                        background=c["card_bg"], foreground=c["text"],
                        fieldbackground=c["card_bg"])
        style.configure("Treeview.Heading",
                        font=("SF Pro", 11, "bold"),
                        background=c["tree_head_bg"],
                        foreground=c["text_strong"])
        style.map("Treeview",
                  background=[("selected", c["primary"])],
                  foreground=[("selected", c["select_fg"])])

        # ── Base widget styles ────────────────────────────────────
        style.configure("TFrame",      background=c["bg"])
        style.configure("TLabel",      background=c["bg"], foreground=c["text"])
        style.configure("TLabelframe", background=c["bg"])
        style.configure("TLabelframe.Label",
                        background=c["bg"], foreground=c["muted"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab",
                        padding=(14, 6),
                        background=c["card_bg"], foreground=c["text"])
        style.map("TNotebook.Tab",
                  background=[("selected", c["primary"])],
                  foreground=[("selected", c["select_fg"])])
        style.configure("TEntry",
                        fieldbackground=c["card_bg"],
                        foreground=c["text"], insertcolor=c["text"])
        style.configure("TCombobox",
                        fieldbackground=c["card_bg"],
                        background=c["card_bg"], foreground=c["text"])

    # ---------- menu bar ----------
    def _build_menu(self) -> None:
        bar = tk.Menu(self)

        # Language submenu
        lang_menu = tk.Menu(bar, tearoff=0)
        lang_menu.add_command(label=_t("menu_lang_zh"),
                              command=lambda: self._switch_lang("zh"))
        lang_menu.add_command(label=_t("menu_lang_en"),
                              command=lambda: self._switch_lang("en"))
        bar.add_cascade(label=_t("menu_language"), menu=lang_menu)

        # Theme submenu — radio button so the current pref is visible.
        theme_menu = tk.Menu(bar, tearoff=0)
        self._theme_var = tk.StringVar(value=_current_theme_pref)
        theme_menu.add_radiobutton(label=_t("menu_theme_light"),
                                    variable=self._theme_var, value="light",
                                    command=lambda: self._switch_theme("light"))
        theme_menu.add_radiobutton(label=_t("menu_theme_dark"),
                                    variable=self._theme_var, value="dark",
                                    command=lambda: self._switch_theme("dark"))
        theme_menu.add_radiobutton(label=_t("menu_theme_auto"),
                                    variable=self._theme_var, value="auto",
                                    command=lambda: self._switch_theme("auto"))
        bar.add_cascade(label=_t("menu_theme"), menu=theme_menu)

        # Help — opens the in-app glossary / term & feature explainer.
        bar.add_command(label=_t("menu_help"), command=self.open_help)

        self.config(menu=bar)

    def open_help(self) -> None:
        HelpWindow(self)

    def _switch_theme(self, pref: str) -> None:
        """Switch theme live + persist preference."""
        if pref not in ("light", "dark", "auto"):
            return
        # Persist BEFORE applying so a crash mid-apply doesn't lose the choice.
        save_theme_pref(pref)
        resolved = apply_theme(pref, app=self)
        self._theme_var.set(pref)
        # Force a data refresh so colour-coded labels (PnL etc.) recompute
        # with the new Win/Loss styles.
        self.after(50, self._refresh)
        label = _t("menu_theme_auto") if pref == "auto" else (
            _t("menu_theme_dark") if pref == "dark" else _t("menu_theme_light")
        )
        self._toast(_t("theme_switched_toast", theme=label, resolved=resolved))

    def _switch_lang(self, lang: str) -> None:
        if lang == current_lang():
            return
        set_lang(lang)
        from tkinter import messagebox
        messagebox.showinfo(_t("lang_switched_title"),
                            _t("lang_switched_body"),
                            parent=self)

    # ---------- editable budget ----------
    def _edit_budget(self) -> None:
        """Edit ACCOUNT_USD live without leaving the GUI.

        Rewrites the matching line in `.env` so the change survives restarts.
        The running scheduler subprocess won't pick it up until restart —
        the toast reminds the user.
        """
        from src.config import settings as _settings
        current = float(_settings.account_usd)
        new_val = _prompt_float(
            self, "Edit Budget",
            f"New ACCOUNT_USD (currently ${current:,.2f}):\n\n"
            f"This is the total capital the bot may deploy.\n"
            f"Bot will never invest more than this, even if your\n"
            f"broker cash is higher.\n\n"
            f"Increase when you add funds; decrease to derisk.",
            current,
        )
        if new_val is None or new_val <= 0:
            return
        new_val = round(new_val, 2)
        if abs(new_val - current) < 1:
            return  # no real change

        env_path = ROOT / ".env"
        if not env_path.exists():
            self._toast("⚠ .env not found — cannot save")
            return

        # Rewrite the matching line in-place, keeping any inline comment.
        original = env_path.read_text()
        lines = original.splitlines()
        found = False
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("ACCOUNT_USD=") or stripped.startswith("#ACCOUNT_USD="):
                comment = ""
                if "#" in line:
                    # Preserve any trailing comment so we don't lose context.
                    hash_idx = line.index("#")
                    # Only preserve comments AFTER the value (not commented-out lines)
                    if hash_idx > line.find("="):
                        comment = "  " + line[hash_idx:]
                lines[i] = f"ACCOUNT_USD={new_val:.0f}{comment}"
                found = True
                break
        if not found:
            lines.append(f"ACCOUNT_USD={new_val:.0f}")
        env_path.write_text("\n".join(lines) + "\n")

        scheduler_running = read_pid() is not None
        msg = (f"💰 Budget updated to ${new_val:,.0f}\n"
               + ("⚠ Restart scheduler for it to take effect"
                  if scheduler_running
                  else "✓ Will apply on next scheduler start"))
        self._toast(msg)
        # Refresh display — the snapshot file still shows old value until
        # next scan writes, so just update the on-screen number now.
        self.after(50, self._refresh)

    # ---------- layout ----------
    def _build(self) -> None:
        outer = ttk.Frame(self, style="Sidebar.TFrame")
        outer.pack(fill="both", expand=True)

        # Left sidebar
        self._sidebar = ttk.Frame(outer, style="Sidebar.TFrame", width=192)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        # 1-px divider between sidebar and content
        ttk.Frame(outer, style="Divider.TFrame", width=1).pack(side="left", fill="y")

        # Right content area
        content = ttk.Frame(outer)
        content.pack(side="left", fill="both", expand=True)

        self._build_sidebar()
        self._build_content(content)

    def _nav_btn(self, parent, text: str, command, accent: bool = False) -> ttk.Button:
        style = "NavAccent.TButton" if accent else "Nav.TButton"
        btn = ttk.Button(parent, text=text, style=style, command=command)
        btn.pack(fill="x", padx=8, pady=2)
        return btn

    def _accent_frame(self, parent, color: str) -> tk.Frame:
        bar = tk.Frame(parent, height=3, bg=color)
        bar._is_accent = True
        bar.pack(fill="x")
        return bar

    def _build_sidebar(self) -> None:
        sb = self._sidebar

        # Logo block
        ttk.Label(sb, text="MooMoo Trader", style="SidebarTitle.TLabel").pack(
            anchor="w", padx=16, pady=(20, 2))
        ttk.Label(sb, text="Auto Trading Bot", style="SidebarSub.TLabel").pack(
            anchor="w", padx=16, pady=(0, 14))
        ttk.Frame(sb, style="Divider.TFrame", height=1).pack(fill="x")

        # Primary controls
        ttk.Frame(sb, style="Sidebar.TFrame", height=10).pack()
        self.btn_start      = self._nav_btn(sb, "▶  " + _t("btn_start"),    self.start_scheduler, accent=True)
        self.btn_stop       = self._nav_btn(sb, "■  " + _t("btn_stop"),      self.stop_scheduler)
        self.btn_scan       = self._nav_btn(sb, "⚡ " + _t("btn_scan"),      self.scan_now)
        self.btn_logs       = self._nav_btn(sb, "📋 " + _t("btn_log"),       self.open_logs)

        ttk.Frame(sb, style="Sidebar.TFrame", height=6).pack()
        ttk.Frame(sb, style="Divider.TFrame", height=1).pack(fill="x")

        # Views section
        ttk.Label(sb, text="  VIEWS", style="SidebarSection.TLabel").pack(
            anchor="w", pady=(8, 2))
        self.btn_hist       = self._nav_btn(sb, "📈 " + _t("btn_history"),   self.open_history)
        self.btn_bt         = self._nav_btn(sb, "🔬 " + _t("btn_backtest"),  self.open_backtest)
        self.btn_equity     = self._nav_btn(sb, "📊 " + _t("btn_equity"),    self.open_equity)
        self.btn_audit      = self._nav_btn(sb, "🔍 " + _t("btn_audit"),     self.open_audit)
        self.btn_wl         = self._nav_btn(sb, "📋 " + _t("btn_watchlist"), self.open_watchlist)
        self.btn_wl_refresh = self._nav_btn(sb, "🔄 Refresh WL",             self.refresh_watchlist)
        self.btn_sect       = self._nav_btn(sb, "🗺  " + _t("btn_sectors"),  self.open_sectors)
        self.btn_ml         = self._nav_btn(sb, "🤖 " + _t("btn_ml"),        self.open_ml)

        self.btn_signal     = self._nav_btn(sb, "📡 Signal Reporter",         self.open_signal_reporter)

        ttk.Frame(sb, style="Divider.TFrame", height=1).pack(fill="x", pady=(6, 0))

    def _build_content(self, parent) -> None:
        px, py = 12, 5

        # ── Status strip ─────────────────────────────────────────
        sbar = ttk.Frame(parent, style="Card.TFrame")
        sbar.pack(fill="x", padx=px, pady=(py, 4))
        self.lbl_scheduler = ttk.Label(sbar, text=f"{_t('scheduler')}: ?",    style="StatusPill.TLabel")
        self.lbl_opend     = ttk.Label(sbar, text=f"{_t('opend')}: ?",        style="StatusPill.TLabel")
        self.lbl_unlock    = ttk.Label(sbar, text=f"{_t('trade')}: ?",        style="StatusPill.TLabel")
        self.lbl_market    = ttk.Label(sbar, text=f"{_t('market')}: ?",       style="StatusPill.TLabel")
        self.lbl_clock     = ttk.Label(sbar, text=f"{_t('et_prefix')} --:--", style="StatusPill.TLabel", cursor="hand2")
        self.lbl_clock.bind("<Button-1>", lambda _: self._force_clock_sync())
        self.lbl_heart  = ttk.Label(sbar, text=_t("last_scan_none"), style="StatusPill.TLabel")
        self.lbl_regime = ttk.Label(sbar, text=_t("regime_none"),    style="StatusPill.TLabel")
        for i, w in enumerate([self.lbl_scheduler, self.lbl_opend, self.lbl_unlock,
                                self.lbl_market, self.lbl_clock, self.lbl_heart, self.lbl_regime]):
            w.grid(row=0, column=i, padx=10, pady=8, sticky="w")

        # ── 4 Stat cards ─────────────────────────────────────────
        crd = ttk.Frame(parent)
        crd.pack(fill="x", padx=px, pady=4)
        for i in range(4):
            crd.columnconfigure(i, weight=1, uniform="card")

        # Card 0: Budget
        c0 = ttk.Frame(crd, style="Card.TFrame")
        c0.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self._accent_frame(c0, "#06b6d4")
        i0 = ttk.Frame(c0, style="Card.TFrame")
        i0.pack(fill="both", padx=14, pady=10)
        ttk.Label(i0, text=_t("budget_label_total"), style="CardTitle.TLabel").pack(anchor="w")
        budget_row = ttk.Frame(i0, style="Card.TFrame")
        budget_row.pack(anchor="w", pady=(2, 0))
        self.lbl_budget = ttk.Label(budget_row, text="$—", style="CardValue.TLabel", cursor="hand2")
        self.lbl_budget.pack(side="left")
        self.lbl_budget.bind("<Button-1>", lambda _: self._edit_budget())
        ttk.Button(budget_row, text="✎", style="Icon.TButton", width=3,
                   command=self._edit_budget).pack(side="left", padx=(6, 0))

        # Card 1: Deployed
        c1 = ttk.Frame(crd, style="Card.TFrame")
        c1.grid(row=0, column=1, sticky="ew", padx=(0, 5))
        self._accent_frame(c1, "#7c3aed")
        i1 = ttk.Frame(c1, style="Card.TFrame")
        i1.pack(fill="both", padx=14, pady=10)
        ttk.Label(i1, text=_t("budget_label_deployed"), style="CardTitle.TLabel").pack(anchor="w")
        self.lbl_invested = ttk.Label(i1, text="$— / $—  (—%)", style="CardMid.TLabel")
        self.lbl_invested.pack(anchor="w", pady=(2, 4))
        self.bar = ttk.Progressbar(i1, mode="determinate", maximum=100, length=170,
                                   style="Budget.Horizontal.TProgressbar")
        self.bar.pack(anchor="w")

        # Card 2: Today P&L
        c2 = ttk.Frame(crd, style="Card.TFrame")
        c2.grid(row=0, column=2, sticky="ew", padx=(0, 5))
        self._accent_frame(c2, "#10b981")
        i2 = ttk.Frame(c2, style="Card.TFrame")
        i2.pack(fill="both", padx=14, pady=10)
        ttk.Label(i2, text=_t("budget_label_today_pnl"), style="CardTitle.TLabel").pack(anchor="w")
        self.lbl_pnl_today = ttk.Label(i2, text="—", style="CardValue.TLabel")
        self.lbl_pnl_today.pack(anchor="w", pady=(2, 0))

        # Card 3: Total P&L
        c3 = ttk.Frame(crd, style="Card.TFrame")
        c3.grid(row=0, column=3, sticky="ew")
        self._accent_frame(c3, "#f59e0b")
        i3 = ttk.Frame(c3, style="Card.TFrame")
        i3.pack(fill="both", padx=14, pady=10)
        ttk.Label(i3, text=_t("budget_label_total_pnl"), style="CardTitle.TLabel").pack(anchor="w")
        self.lbl_pnl = ttk.Label(i3, text="—", style="CardValue.TLabel")
        self.lbl_pnl.pack(anchor="w", pady=(2, 0))

        # ── Account / status info row ─────────────────────────────
        info = ttk.Frame(parent, style="Card.TFrame")
        info.pack(fill="x", padx=px, pady=4)
        self.lbl_cash      = ttk.Label(info, text=f"{_t('broker_cash')}: —",  style="InfoPill.TLabel")
        self.lbl_positions = ttk.Label(info, text=f"{_t('positions')}: —",    style="InfoPill.TLabel")
        self.lbl_streak    = ttk.Label(info, text=f"{_t('loss_streak')}: —",  style="InfoPill.TLabel")
        self.lbl_halted    = ttk.Label(info, text=f"{_t('halted')}: —",       style="InfoPill.TLabel", cursor="hand2")
        self.lbl_halted.bind("<Button-1>", lambda _: self._reset_halt())
        self.lbl_recon     = ttk.Label(info, text=f"{_t('reconcile')}: —",    style="InfoPill.TLabel")
        self.lbl_ai        = ttk.Label(info, text=f"{_t('ai_model')}: —",     style="InfoPill.TLabel")
        self.lbl_vix       = ttk.Label(info, text="VIX: —",                   style="InfoPill.TLabel")
        self.lbl_env       = ttk.Label(info, text=f"{_t('env')}: —",          style="InfoPill.TLabel")
        for i, w in enumerate([self.lbl_cash, self.lbl_positions, self.lbl_streak,
                                self.lbl_halted, self.lbl_recon, self.lbl_ai,
                                self.lbl_vix, self.lbl_env]):
            w.grid(row=0, column=i, padx=8, pady=6, sticky="w")

        # ── Strategy / config row ─────────────────────────────────
        cfg2 = ttk.Frame(parent)
        cfg2.pack(fill="x", padx=px, pady=(0, 4))
        self.lbl_thresh   = ttk.Label(cfg2, text=f"{_t('entry_threshold')} —", style="Muted.TLabel")
        self.lbl_interval = ttk.Label(cfg2, text=f"{_t('scan_every')} —",      style="Muted.TLabel")
        self.lbl_hold     = ttk.Label(cfg2, text=f"{_t('max_hold')} —",        style="Muted.TLabel")
        self.lbl_ml       = ttk.Label(cfg2, text="ML: —",                      style="Muted.TLabel")
        self.lbl_strat    = ttk.Label(cfg2, text="🎯 —",                       style="Muted.TLabel")
        for i, w in enumerate([self.lbl_thresh, self.lbl_interval, self.lbl_hold,
                                self.lbl_ml, self.lbl_strat]):
            w.grid(row=0, column=i, padx=8, pady=2, sticky="w")

        # ── Positions table ───────────────────────────────────────
        pos = ttk.LabelFrame(parent, text=_t("positions_section"),
                             style="Section.TLabelframe")
        pos.pack(fill="x", padx=px, pady=4)
        cols = ("symbol", "qty", "entry", "last", "stop", "tp", "atr", "pnl")
        col_headers = [_t("col_symbol"), _t("col_qty"), _t("col_entry"),
                       _t("col_last"), _t("col_stop"), _t("col_tp"),
                       _t("col_atr"), _t("col_pnl")]
        self.tree = ttk.Treeview(pos, columns=cols, show="headings", height=4)
        widths = (75, 55, 85, 85, 85, 85, 60, 110)
        for c, h, w in zip(cols, col_headers, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.tag_configure("win",  foreground=THEME["success"])
        self.tree.tag_configure("loss", foreground=THEME["danger"])
        self.tree.pack(fill="x", padx=6, pady=6)

        # Right-click context menu
        self.pos_menu = tk.Menu(self, tearoff=0)
        self.pos_menu.add_command(label=_t("menu_close_now"), command=self._ctx_close)
        self.pos_menu.add_command(label=_t("menu_edit_stop"), command=self._ctx_edit_stop)
        self.tree.bind("<Button-2>",          self._on_pos_right_click)
        self.tree.bind("<Button-3>",          self._on_pos_right_click)
        self.tree.bind("<Control-Button-1>",  self._on_pos_right_click)

        # ── Log tail ─────────────────────────────────────────────
        logbox = ttk.LabelFrame(parent, text=_t("log_section"),
                                style="Section.TLabelframe")
        logbox.pack(fill="both", expand=True, padx=px, pady=(4, py))
        self.log_text = tk.Text(logbox, wrap="word", height=12, font=("Menlo", 11))
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
        win.geometry("840x500")
        _apply_theme(win)
        ttk.Label(win, text="Per-scan + weekly digest of account state",
                  background=THEME["bg"], foreground=THEME["muted"],
                  font=("SF Pro", 11)).pack(anchor="w", padx=14, pady=(10, 4))
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))

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

        # ─── Budget card: big editable number + deployment bar + PnL ───
        # Pull the most-recent budget from .env in case the user just edited
        # it; account.json's `budget` field lags by one scan.
        from src.config import settings as _live_settings
        budget = _live_settings.account_usd or account.get("budget", 0) or 0
        invested = account.get("invested", 0)
        used_pct = (invested / budget * 100) if budget else 0
        self.bar["value"] = min(100, used_pct)
        self.lbl_budget.configure(text=f"${budget:,.0f}")
        self.lbl_invested.configure(
            text=f"${invested:,.2f} / ${budget:,.0f}  ({used_pct:.1f}%)"
        )

        # Today's PnL + total PnL — separately coloured.
        total = account.get("total_pnl", 0)
        today = account.get("realized_pnl_today", 0)
        unreal = account.get("unrealized_pnl", 0)
        realiz = account.get("realized_pnl_total", 0)
        self.lbl_pnl_today.configure(
            text=f"${today:+,.2f}",
            style="Win.TLabel" if today >= 0 else "Loss.TLabel",
        )
        self.lbl_pnl.configure(
            text=f"${total:+,.2f}  (R ${realiz:+,.0f} · U ${unreal:+,.0f})",
            style="Win.TLabel" if total >= 0 else "Loss.TLabel",
        )

        cash = account.get("cash") or state.get("starting_cash")
        if cash:
            ts = account.get("ts", "")[-8:-3] if account.get("ts") else ""
            self.lbl_cash.configure(text=f"{_t('broker_cash')}: ${cash:,.2f}" + (f"  ({ts})" if ts else ""))
        else:
            self.lbl_cash.configure(text=_t("broker_cash_waiting"))
        self.lbl_streak.configure(text=f"{_t('loss_streak')}: {state.get('loss_streak_days', 0)}d")
        if state.get("halted"):
            self.lbl_halted.configure(text=_t("halted_label_yes"))
        else:
            self.lbl_halted.configure(text=_t("halted_label_no"))
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

    def open_signal_reporter(self) -> None:
        SignalReporterWindow(self)

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
        self.geometry("880x660")
        self.resizable(True, True)
        _apply_theme(self)
        self._running = False
        self._build()
        # Auto-load last results if available
        if BACKTEST_FILE.exists():
            self._load_saved()

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}

        # Configuration card
        cfg = ttk.LabelFrame(self, text="  ⚙ Configuration  ",
                              style="Section.TLabelframe")
        cfg.pack(fill="x", **pad)

        # Use a Frame inside so we control padding precisely
        inner = ttk.Frame(cfg)
        inner.pack(fill="x", padx=8, pady=4)

        ttk.Label(inner, text="Days").grid(row=0, column=0, padx=4, pady=4, sticky="e")
        self.var_days = tk.StringVar(value="180")
        ttk.Combobox(inner, textvariable=self.var_days,
                     values=["60", "90", "180", "365"],
                     width=6, state="readonly").grid(row=0, column=1, padx=(2, 14), pady=4, sticky="w")

        ttk.Label(inner, text="Timeframe").grid(row=0, column=2, padx=4, sticky="e")
        self.var_tf = tk.StringVar(value="HOUR_1")
        ttk.Combobox(inner, textvariable=self.var_tf,
                     values=["HOUR_1", "DAILY"],
                     width=10, state="readonly").grid(row=0, column=3, padx=(2, 14), pady=4, sticky="w")

        ttk.Label(inner, text="Threshold").grid(row=0, column=4, padx=4, sticky="e")
        self.var_thresh = tk.StringVar(value="70")
        ttk.Entry(inner, textvariable=self.var_thresh, width=6).grid(row=0, column=5, padx=(2, 14), pady=4, sticky="w")

        ttk.Label(inner, text="Tickers").grid(row=0, column=6, padx=4, sticky="e")
        self.var_tickers = tk.StringVar(value="")
        e = ttk.Entry(inner, textvariable=self.var_tickers, width=24)
        e.grid(row=0, column=7, padx=(2, 14), pady=4, sticky="w")
        # Placeholder hint
        ttk.Label(inner, text="(blank = use watchlist)",
                  foreground=THEME["muted"], font=("SF Pro", 9)).grid(row=1, column=7, sticky="w", padx=2)

        self.btn_run = ttk.Button(inner, text="▶  Run Backtest",
                                   style="Primary.TButton", command=self._run)
        self.btn_run.grid(row=0, column=8, padx=8, pady=4)

        # Progress bar
        prog = ttk.Frame(self)
        prog.pack(fill="x", padx=10, pady=(0, 4))
        self.lbl_prog = ttk.Label(prog,
                                   text="Ready — load previous results or run a new test.",
                                   foreground=THEME["muted"], font=("SF Pro", 10))
        self.lbl_prog.pack(side="left")
        self.progressbar = ttk.Progressbar(prog, mode="indeterminate", length=180,
                                            style="Budget.Horizontal.TProgressbar")
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

            # progress fires from BOTH phases — prefetch (small total ≈ n_tickers)
            # and simulation (total = events ≈ n_tickers × bars, much larger).
            # Use the magnitude of `total` to pick the right label so the user
            # sees the phase shift instead of a frozen "Fetching X" during sim.
            def progress(cur, total, sym):
                pct = int((cur + 1) / max(total, 1) * 100)
                if total > n_tickers * 2:
                    label = f"Simulating {sym}… ({pct}%)"
                else:
                    label = f"Fetching {sym}… ({cur+1}/{total})"
                self.after(0, lambda l=label: self.lbl_prog.configure(text=l))

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


# ---------- Signal Reporter window ----------

class SignalReporterWindow(tk.Toplevel):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Signal Reporter")
        self.geometry("560x700")
        _apply_theme(self)
        self._build()
        self._load_watchlist()
        self._refresh_status()

    def _build(self) -> None:
        ttk.Label(
            self,
            text="Independent signal watchlist — sends Telegram signals every 30 min during peak hours.",
            background=THEME["bg"], foreground=THEME["muted"],
            wraplength=530, font=("SF Pro", 10),
        ).pack(anchor="w", padx=14, pady=(10, 0))

        # ── Scheduler controls ───────────────────────────────────────
        ctrl = ttk.LabelFrame(self, text="  Scheduler  ", style="Section.TLabelframe")
        ctrl.pack(fill="x", padx=12, pady=(8, 4))
        inner = ttk.Frame(ctrl)
        inner.pack(fill="x", padx=8, pady=8)

        self.lbl_status = ttk.Label(inner, text="⚪ Stopped",
                                     font=("SF Pro", 12, "bold"),
                                     foreground=THEME["muted"])
        self.lbl_status.pack(side="left", padx=(0, 12))

        self.btn_sr_start = ttk.Button(inner, text="▶  Start",
                                        style="Primary.TButton",
                                        command=self._start)
        self.btn_sr_start.pack(side="left", padx=(0, 4))
        self.btn_sr_stop = ttk.Button(inner, text="■  Stop", command=self._stop)
        self.btn_sr_stop.pack(side="left", padx=(0, 12))

        ttk.Button(inner, text="📊 Premarket Now",
                   command=self._run_premarket).pack(side="right", padx=(4, 0))
        ttk.Button(inner, text="⚡ Intraday Now",
                   command=self._run_intraday).pack(side="right", padx=4)

        # ── Watchlist editor ────────────────────────────────────────
        wl = ttk.LabelFrame(self, text="  Watchlist  ", style="Section.TLabelframe")
        wl.pack(fill="x", padx=12, pady=4)

        top = ttk.Frame(wl)
        top.pack(fill="x", padx=8, pady=(6, 4))
        ttk.Label(top, text="Symbol:", foreground=THEME["muted"]).pack(side="left", padx=(0, 4))
        self.var_new = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.var_new, width=10, font=("Menlo", 11))
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _: self._add_ticker())
        ttk.Button(top, text="➕  Add",   command=self._add_ticker).pack(side="left", padx=2)
        ttk.Button(top, text="🗑  Remove", command=self._remove_ticker).pack(side="left", padx=2)
        ttk.Button(top, text="💾  Save", style="Primary.TButton",
                   command=self._save_watchlist).pack(side="right")

        list_frame = ttk.Frame(wl)
        list_frame.pack(fill="x", padx=8, pady=(0, 4))
        self.listbox = tk.Listbox(
            list_frame, font=("Menlo", 12), selectmode="extended",
            bg=THEME["card_bg"], fg=THEME["text"],
            selectbackground=THEME["primary"], selectforeground="white",
            relief="solid", borderwidth=1, highlightthickness=0,
            activestyle="none", height=7,
        )
        lb_sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=lb_sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        lb_sb.pack(side="right", fill="y")

        self.lbl_wl_status = ttk.Label(wl, text="",
                                        background=THEME["bg"], foreground=THEME["muted"],
                                        font=("SF Pro", 10))
        self.lbl_wl_status.pack(anchor="w", padx=8, pady=(2, 8))

        # ── Log tail ────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="  Log  ", style="Section.TLabelframe")
        log_frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        log_top = ttk.Frame(log_frame)
        log_top.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Button(log_top, text="📂 Open File", style="Icon.TButton",
                   command=lambda: subprocess.run(["open", str(SIGNAL_LOG_FILE)])).pack(side="right", padx=4)
        ttk.Button(log_top, text="🔄", style="Icon.TButton",
                   command=self._tail_log).pack(side="right")

        self.log_text = tk.Text(
            log_frame, wrap="word", height=10, font=("Menlo", 10),
            bg=THEME["card_bg"], fg=THEME["text"],
            relief="solid", borderwidth=1, highlightthickness=0, padx=6, pady=4,
        )
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        log_sb.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))

        self._tail_log()
        self.after(5000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if not self.winfo_exists():
            return
        self._refresh_status()
        self._tail_log()
        self.after(5000, self._auto_refresh)

    def _read_signal_pid(self) -> int | None:
        try:
            pid = int(SIGNAL_PID_FILE.read_text().strip())
            return pid if is_process_alive(pid) else None
        except (FileNotFoundError, ValueError):
            return None

    def _refresh_status(self) -> None:
        pid = self._read_signal_pid()
        if pid:
            self.lbl_status.configure(text=f"🟢 Running (PID {pid})",
                                       foreground=THEME["success"])
        else:
            self.lbl_status.configure(text="⚪ Stopped", foreground=THEME["muted"])

    def _start(self) -> None:
        if self._read_signal_pid() is not None:
            self.lbl_wl_status.configure(text="Signal reporter is already running")
            return
        SIGNAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_fd = open(SIGNAL_LOG_FILE, "a")
        proc = subprocess.Popen(
            [PYTHON, "-m", "src.signal_reporter", "run"],
            cwd=str(ROOT), stdout=log_fd, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        SIGNAL_PID_FILE.write_text(str(proc.pid))
        self.lbl_wl_status.configure(text=f"Started (PID {proc.pid})")
        self._refresh_status()

    def _stop(self) -> None:
        pid = self._read_signal_pid()
        if pid is None:
            self.lbl_wl_status.configure(text="Signal reporter is not running")
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
            SIGNAL_PID_FILE.unlink()
        except FileNotFoundError:
            pass
        self.lbl_wl_status.configure(text="Stopped")
        self._refresh_status()

    def _run_premarket(self) -> None:
        self.lbl_wl_status.configure(text="Running premarket analysis…")
        threading.Thread(target=self._do_run, args=("premarket",), daemon=True).start()

    def _run_intraday(self) -> None:
        self.lbl_wl_status.configure(text="Running intraday scan…")
        threading.Thread(target=self._do_run, args=("intraday",), daemon=True).start()

    def _do_run(self, mode: str) -> None:
        SIGNAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [PYTHON, "-m", "src.signal_reporter", mode],
                cwd=str(ROOT),
                stdout=open(SIGNAL_LOG_FILE, "a"),
                stderr=subprocess.STDOUT,
                timeout=180,
            )
            self.after(0, lambda: self.lbl_wl_status.configure(
                text=f"✓ {mode.capitalize()} done — check Telegram"
            ))
        except subprocess.TimeoutExpired:
            self.after(0, lambda: self.lbl_wl_status.configure(text=f"⚠ {mode} timed out"))
        except Exception as e:
            self.after(0, lambda: self.lbl_wl_status.configure(text=f"⚠ {mode} error: {e}"))
        self.after(0, self._tail_log)

    def _load_watchlist(self) -> None:
        if not SIGNAL_WL_FILE.exists():
            self.lbl_wl_status.configure(text="No signal watchlist file found")
            return
        try:
            data = json.loads(SIGNAL_WL_FILE.read_text())
            for t in data.get("tickers", []):
                self.listbox.insert("end", t)
            self.lbl_wl_status.configure(text=f"Loaded {self.listbox.size()} tickers")
        except Exception as e:
            self.lbl_wl_status.configure(text=f"Load error: {e}")

    def _add_ticker(self) -> None:
        sym = self.var_new.get().strip().upper()
        if not sym:
            return
        if sym in set(self.listbox.get(0, "end")):
            self.lbl_wl_status.configure(text=f"{sym} already in list")
            return
        self.listbox.insert("end", sym)
        self.var_new.set("")
        self.lbl_wl_status.configure(text=f"Added {sym}  — click Save to persist")

    def _remove_ticker(self) -> None:
        sel = list(self.listbox.curselection())
        if not sel:
            return
        for i in reversed(sel):
            self.listbox.delete(i)
        self.lbl_wl_status.configure(text=f"Removed {len(sel)} ticker(s)  — click Save to persist")

    def _save_watchlist(self) -> None:
        tickers = list(self.listbox.get(0, "end"))
        try:
            SIGNAL_WL_FILE.write_text(json.dumps({"tickers": tickers}, indent=2))
            self.lbl_wl_status.configure(text=f"✓ Saved {len(tickers)} tickers")
        except Exception as e:
            self.lbl_wl_status.configure(text=f"Save failed: {e}")

    def _tail_log(self) -> None:
        if not SIGNAL_LOG_FILE.exists():
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", f"Log file not found: {SIGNAL_LOG_FILE}\nRun premarket or intraday to generate output.")
            self.log_text.configure(state="disabled")
            return
        try:
            with open(SIGNAL_LOG_FILE, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 6000))
                tail = f.read().decode(errors="replace")
        except OSError:
            return
        body = "\n".join(tail.splitlines()[-100:])
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", body)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


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
        self.geometry("960x640")
        _apply_theme(self)
        self._build()
        self._refresh()

    def _build(self) -> None:
        # Toolbar
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(top, text="Source:",
                  foreground=THEME["muted"]).pack(side="left", padx=(0, 4))
        self.var_src = tk.StringVar(value="closed_trades")
        ttk.Combobox(top, textvariable=self.var_src,
                     values=["closed_trades", "snapshot_history"],
                     width=20, state="readonly").pack(side="left", padx=4)
        ttk.Button(top, text="🔄 Refresh",
                   command=self._refresh).pack(side="left", padx=4)
        self.lbl_stats = ttk.Label(top, text="—",
                                    font=("Menlo", 11, "bold"),
                                    foreground=THEME["text_strong"])
        self.lbl_stats.pack(side="right", padx=8)

        # Embed matplotlib figure with our background colour so the chart
        # frame blends with the window theme.
        import matplotlib
        matplotlib.use("TkAgg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self.fig = Figure(figsize=(8, 4.8), dpi=100, facecolor=THEME["bg"])
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True,
                                          padx=12, pady=(0, 12))

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
        # White plotting area on the themed window background.
        ax.set_facecolor(THEME["card_bg"])
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.spines["left"].set_color(THEME["border"])
        ax.spines["bottom"].set_color(THEME["border"])

        ax.plot(equity, linewidth=2.0, color=THEME["primary"],
                solid_capstyle="round")
        ax.fill_between(range(len(equity)), equity, 0,
                        where=[e >= 0 for e in equity],
                        color=THEME["primary"], alpha=0.10)

        # Drawdown shading on a secondary axis.
        peak = 0.0
        dd = []
        for e in equity:
            peak = max(peak, e)
            dd.append(e - peak)
        ax2 = ax.twinx()
        for spine in ("top", "right", "left", "bottom"):
            ax2.spines[spine].set_visible(False)
        ax2.fill_between(range(len(dd)), dd, 0,
                         color=THEME["danger"], alpha=0.15, label="Drawdown")
        ax2.set_ylabel("Drawdown ($)", color=THEME["danger"], fontsize=10)
        ax2.tick_params(axis="y", labelcolor=THEME["danger"], labelsize=9)

        ax.set_ylabel("Cumulative PnL ($)", fontsize=10,
                       color=THEME["text_strong"])
        ax.set_xlabel("Trades / Snapshots →", fontsize=10,
                       color=THEME["muted"])
        ax.grid(True, alpha=0.25, linestyle="--", linewidth=0.7)
        ax.axhline(0, color=THEME["muted"], linewidth=0.8, alpha=0.6)
        ax.tick_params(colors=THEME["muted"], labelsize=9)

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
        self.geometry("960x640")
        _apply_theme(self)
        self._build()
        self._refresh()

    def _build(self) -> None:
        # Toolbar
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Button(top, text="🔄 Refresh",
                   command=self._refresh).pack(side="left", padx=(0, 8))
        ttk.Label(top, text="Filter:",
                  foreground=THEME["muted"]).pack(side="left", padx=(4, 4))
        self.var_filter = tk.StringVar(value="all")
        ttk.Combobox(top, textvariable=self.var_filter,
                     values=["all", "buy", "skip", "error"],
                     width=10, state="readonly").pack(side="left", padx=4)
        self.var_filter.trace_add("write", lambda *_: self._refresh())
        self.lbl_summary = ttk.Label(top, text="—",
                                      font=("Menlo", 11, "bold"),
                                      foreground=THEME["text_strong"])
        self.lbl_summary.pack(side="right", padx=8)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(4, 12))

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
        self.tree.tag_configure("buy",   foreground=THEME["success"])
        self.tree.tag_configure("skip",  foreground=THEME["muted"])
        self.tree.tag_configure("error", foreground=THEME["danger"])

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
        self.geometry("460x580")
        _apply_theme(self)
        self._build()
        self._load()

    def _build(self) -> None:
        # Header hint
        ttk.Label(self,
                  text="Edit candidate tickers. Auto-refreshed weekly from S&P 500.",
                  background=THEME["bg"], foreground=THEME["muted"],
                  font=("SF Pro", 10)).pack(anchor="w", padx=14, pady=(10, 0))

        # Toolbar
        top = ttk.Frame(self)
        top.pack(fill="x", padx=14, pady=(6, 4))
        ttk.Label(top, text="Symbol:",
                  foreground=THEME["muted"]).pack(side="left", padx=(0, 4))
        self.var_new = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.var_new, width=10,
                       font=("Menlo", 11))
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _: self._add())
        ttk.Button(top, text="➕  Add",
                   command=self._add).pack(side="left", padx=2)
        ttk.Button(top, text="🗑  Remove",
                   command=self._remove).pack(side="left", padx=2)
        ttk.Button(top, text="💾  Save",
                   style="Primary.TButton",
                   command=self._save).pack(side="right")

        # Listbox in its own frame for clean borders
        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=14, pady=(6, 6))
        self.list = tk.Listbox(list_frame, font=("Menlo", 12),
                                selectmode="extended",
                                bg=THEME["card_bg"], fg=THEME["text"],
                                selectbackground=THEME["primary"],
                                selectforeground="white",
                                relief="solid", borderwidth=1,
                                highlightthickness=0,
                                activestyle="none")
        sb = ttk.Scrollbar(list_frame, orient="vertical",
                            command=self.list.yview)
        self.list.configure(yscrollcommand=sb.set)
        self.list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.lbl_status = ttk.Label(self, text="",
                                     background=THEME["bg"],
                                     foreground=THEME["muted"],
                                     font=("SF Pro", 10))
        self.lbl_status.pack(side="bottom", fill="x", padx=14, pady=(2, 10))

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
    """Card-grid heat-map of watchlist by sector.

    Each sector is a card showing:
      - sector name + usage indicator (e.g. "2/3") in coloured header
      - a thin progress bar visualising sector heat
      - the tickers in that sector, with a green dot + bold for held positions

    The grid auto-flows into 1–4 columns based on window width, with rows
    sized to the tallest card in each row so nothing overlaps. Content that
    exceeds the visible area is scrollable; window resize re-draws.
    """

    # Layout constants (px)
    PADDING = 12
    CARD_GAP = 12
    CARD_MIN_W = 210
    HEADER_H = 42
    LINE_H = 19
    MAX_COLS = 4

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Sector Heat-map")
        self.geometry("860x580")
        _apply_theme(self)
        self._build()

    def _build(self) -> None:
        ttk.Label(
            self,
            text="Watchlist by sector — green dot = held position, bar = sector exposure.",
            font=("SF Pro", 11),
        ).pack(anchor="w", padx=14, pady=(10, 4))

        # Canvas + vertical scrollbar in a frame
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.canvas = tk.Canvas(frame, bg=THEME["card_bg"], highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        # Resize → redraw. Mouse wheel → scroll.
        self.canvas.bind("<Configure>", lambda _e: self._draw())
        # macOS sends fine-grained deltas; Windows sends ±120 multiples. The
        # /120-or-1 fallback works for both.
        self.canvas.bind(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(
                int(-1 * (e.delta / 120 if abs(e.delta) >= 120 else e.delta)),
                "units",
            ),
        )
        self.after(50, self._draw)

    # ---------- drawing helpers ----------

    @staticmethod
    def _ratio_colors(ratio: float) -> tuple[str, str, str]:
        """Returns (header_fill, border, bar_fill) tuned for green→yellow→red."""
        if ratio == 0:
            return "#e8f5e9", "#a5d6a7", "#4caf50"
        if ratio < 0.7:
            return "#e3f2fd", "#90caf9", "#2196f3"
        if ratio < 1:
            return "#fff3e0", "#ffcc80", "#ff9800"
        return "#ffebee", "#ef9a9a", "#f44336"

    def _draw(self) -> None:
        from src.sector import SECTOR_MAP, MAX_PER_SECTOR
        if not WATCHLIST_FILE.exists():
            return
        watchlist = json.loads(WATCHLIST_FILE.read_text())["tickers"]
        trades = read_json(OPEN_TRADES)
        held = set(trades.keys())

        # Group by sector → list of tickers
        groups: dict[str, list[str]] = {}
        for sym in watchlist:
            sect = SECTOR_MAP.get(sym.upper(), "unknown")
            groups.setdefault(sect, []).append(sym)

        # Sort: more-held sectors first, then larger ones
        sorted_groups = sorted(
            groups.items(),
            key=lambda kv: (-len([s for s in kv[1] if s in held]), -len(kv[1])),
        )
        n = len(sorted_groups)
        if n == 0:
            return

        self.canvas.delete("all")
        self.canvas.update_idletasks()
        cw = self.canvas.winfo_width()
        if cw < 300:
            cw = 800

        # Decide column count so each card is ≥ CARD_MIN_W wide.
        usable = cw - 2 * self.PADDING + self.CARD_GAP
        cols = max(1, min(self.MAX_COLS, usable // (self.CARD_MIN_W + self.CARD_GAP)))
        cols = min(cols, n)   # don't show empty trailing columns
        rows = (n + cols - 1) // cols
        card_w = (cw - 2 * self.PADDING - (cols - 1) * self.CARD_GAP) // cols

        # Per-row height = tallest card on that row.
        row_heights: list[int] = []
        for r in range(rows):
            tallest = 0
            for c in range(cols):
                idx = r * cols + c
                if idx >= n:
                    continue
                n_syms = len(sorted_groups[idx][1])
                tallest = max(tallest, self.HEADER_H + n_syms * self.LINE_H + 16)
            row_heights.append(tallest)

        # Draw each card.
        y_origin = self.PADDING
        for r in range(rows):
            x_origin = self.PADDING
            for c in range(cols):
                idx = r * cols + c
                if idx >= n:
                    break
                sect, syms = sorted_groups[idx]
                in_sector = [s for s in syms if s in held]
                ratio = len(in_sector) / max(1, MAX_PER_SECTOR)
                header_fill, border, bar_color = self._ratio_colors(ratio)

                card_x2 = x_origin + card_w
                card_y2 = y_origin + row_heights[r]

                # Card body (white) + border
                self.canvas.create_rectangle(
                    x_origin, y_origin, card_x2, card_y2,
                    fill="white", outline=border, width=1,
                )
                # Header band
                self.canvas.create_rectangle(
                    x_origin, y_origin, card_x2, y_origin + self.HEADER_H,
                    fill=header_fill, outline=border, width=1,
                )
                # Sector name (left)
                self.canvas.create_text(
                    x_origin + 10, y_origin + 14,
                    anchor="w", text=sect,
                    font=("SF Pro", 12, "bold"), fill="#222",
                )
                # Usage count (right)
                self.canvas.create_text(
                    card_x2 - 10, y_origin + 14,
                    anchor="e", text=f"{len(in_sector)}/{MAX_PER_SECTOR}",
                    font=("Menlo", 11, "bold"), fill="#333",
                )
                # Progress bar (under name+count, inside header)
                bar_y = y_origin + self.HEADER_H - 8
                bar_x1, bar_x2 = x_origin + 10, card_x2 - 10
                self.canvas.create_rectangle(
                    bar_x1, bar_y, bar_x2, bar_y + 4,
                    fill="#ddd", outline="",
                )
                if ratio > 0:
                    fill_x = bar_x1 + (bar_x2 - bar_x1) * min(1.0, ratio)
                    self.canvas.create_rectangle(
                        bar_x1, bar_y, fill_x, bar_y + 4,
                        fill=bar_color, outline="",
                    )

                # Symbol list — dot + ticker
                ty = y_origin + self.HEADER_H + 8
                for sym in syms:
                    is_held = sym in held
                    dot_color = bar_color if is_held else "#ccc"
                    self.canvas.create_oval(
                        x_origin + 12, ty + 4, x_origin + 18, ty + 10,
                        fill=dot_color, outline="",
                    )
                    self.canvas.create_text(
                        x_origin + 26, ty + 7, anchor="w", text=sym,
                        font=("Menlo", 11, "bold" if is_held else "normal"),
                        fill="#1b5e20" if is_held else "#555",
                    )
                    ty += self.LINE_H

                x_origin += card_w + self.CARD_GAP
            y_origin += row_heights[r] + self.CARD_GAP

        # Enable scroll if content taller than canvas viewport.
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))


# ---------- ML model window ----------

class MLWindow(tk.Toplevel):
    META_FILE = ROOT / "data" / "ml" / "model_meta.json"

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("ML Model")
        self.geometry("820x660")
        _apply_theme(self)
        self._build()
        self._refresh()

    def _build(self) -> None:
        # Header strip
        top = ttk.Frame(self)
        top.pack(fill="x", padx=14, pady=(12, 6))
        self.lbl_status = ttk.Label(top, text="—",
                                     font=("SF Pro", 13, "bold"),
                                     foreground=THEME["text_strong"])
        self.lbl_status.pack(side="left")
        ttk.Button(top, text="🧠  Train Now",
                   style="Primary.TButton",
                   command=self._train).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="🔄 Refresh",
                   command=self._refresh).pack(side="right", padx=4)

        # Notebook: Overview + Calibration
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        # Tab 1: Overview (metrics + feature importance)
        tab_ov = ttk.Frame(nb)
        nb.add(tab_ov, text=_t("ml_tab_overview"))
        self.txt_metrics = tk.Text(tab_ov, height=8, font=("Menlo", 11),
                                    wrap="word",
                                    bg=THEME["card_bg"], fg=THEME["text"],
                                    relief="solid", borderwidth=1,
                                    highlightthickness=0, padx=8, pady=6)
        self.txt_metrics.pack(fill="x", padx=6, pady=6)
        self.txt_metrics.configure(state="disabled")
        ttk.Label(tab_ov, text="Feature importance (top 20)",
                  font=("SF Pro", 11, "bold"),
                  foreground=THEME["text_strong"]).pack(anchor="w",
                                                         padx=10, pady=(8, 2))
        self.fi_canvas = tk.Canvas(tab_ov, bg=THEME["card_bg"], height=380,
                                    relief="solid", borderwidth=1,
                                    highlightthickness=0)
        self.fi_canvas.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        # Cache the last feature_importance dict so the canvas can redraw
        # itself on window resize (otherwise bars vanish when user enlarges
        # the window after first paint).
        self._fi_data: dict = {}
        self.fi_canvas.bind(
            "<Configure>",
            lambda _e: self._draw_feature_importance(self._fi_data),
        )

        # Tab 2: Calibration (predicted proba vs actual outcome)
        tab_cal = ttk.Frame(nb)
        nb.add(tab_cal, text=_t("ml_tab_calibration"))
        self.lbl_calib_brier = ttk.Label(tab_cal, text="—",
                                          font=("SF Pro", 12, "bold"),
                                          foreground=THEME["text_strong"])
        self.lbl_calib_brier.pack(anchor="w", padx=12, pady=(10, 0))
        ttk.Label(tab_cal, text=_t("ml_calibration_title"),
                  foreground=THEME["muted"],
                  font=("SF Pro", 11)).pack(anchor="w", padx=12, pady=(2, 4))
        cal_cols = ("range", "n", "mean_proba", "winrate", "diff", "mean_r")
        cal_widths = (110, 60, 100, 90, 80, 80)
        self.cal_tree = ttk.Treeview(tab_cal, columns=cal_cols,
                                      show="headings", height=8)
        for c, w in zip(cal_cols, cal_widths):
            self.cal_tree.heading(c, text=c.upper())
            self.cal_tree.column(c, width=w, anchor="center")
        self.cal_tree.pack(fill="x", padx=12, pady=8)
        self.lbl_calib_note = ttk.Label(tab_cal, text="", wraplength=760,
                                         font=("SF Pro", 10),
                                         foreground=THEME["muted"])
        self.lbl_calib_note.pack(anchor="w", padx=12, pady=8)

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
        """Draw the top-20 feature-importance horizontal bar chart.

        Cache `fi` so the bound <Configure> event can redraw with the new
        size. Use `update_idletasks()` to force a layout pass before reading
        winfo_width — otherwise the canvas reports width=1 on first paint
        and bars get drawn with negative width (invisible).
        """
        self._fi_data = fi or {}
        self.fi_canvas.delete("all")
        items = list(self._fi_data.items())[:20]
        if not items:
            return
        self.fi_canvas.update_idletasks()   # force layout — fixes width=1 bug
        w = self.fi_canvas.winfo_width()
        if w < 300:
            # Canvas not laid out yet OR window is tiny — use a sane default
            # so bars are still visible. The <Configure> binding will redraw
            # at the real size as soon as the layout settles.
            w = 740
        max_imp = max(v for _, v in items) or 1
        bar_w = max(50, w - 200)            # never go negative
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


# ---------- Help / Glossary window ----------

class HelpWindow(tk.Toplevel):
    """In-app glossary + feature documentation.

    Left side: tree of categories → terms (from src/glossary.py).
    Right side: scrollable formatted text with the term's explanation.
    """

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("Help · Glossary")
        self.geometry("980x680")
        _apply_theme(self)
        self._build()
        self._populate_tree()

    def _build(self) -> None:
        # Header strip
        header = ttk.Frame(self)
        header.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(header, text="📚 词条与功能说明",
                  font=("SF Pro", 14, "bold"),
                  foreground=THEME["text_strong"]).pack(side="left")
        ttk.Label(header,
                  text="点左边目录看每个功能 / 指标 / 算法的详细解释",
                  foreground=THEME["muted"],
                  font=("SF Pro", 10)).pack(side="left", padx=(12, 0))

        # PanedWindow — drag the divider to resize
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # Left: category tree
        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        self.tree = ttk.Treeview(left, show="tree", height=24)
        self.tree.column("#0", width=280, anchor="w")
        tree_sb = ttk.Scrollbar(left, orient="vertical",
                                 command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # Right: detail pane
        right = ttk.Frame(paned)
        paned.add(right, weight=3)
        self.txt = tk.Text(right, wrap="word", padx=20, pady=18,
                            font=("SF Pro", 11),
                            bg=THEME["card_bg"], fg=THEME["text"],
                            relief="solid", borderwidth=1,
                            highlightthickness=0,
                            spacing1=2, spacing3=6)
        txt_sb = ttk.Scrollbar(right, orient="vertical",
                                command=self.txt.yview)
        self.txt.configure(yscrollcommand=txt_sb.set, state="disabled")
        self.txt.pack(side="left", fill="both", expand=True)
        txt_sb.pack(side="right", fill="y")

        # Configure text tags for formatting
        self._configure_tags()

    def _configure_tags(self) -> None:
        c = THEME
        self.txt.tag_configure("h1",
            font=("SF Pro", 18, "bold"),
            foreground=c["text_strong"], spacing3=12)
        self.txt.tag_configure("h2",
            font=("SF Pro", 12, "bold"),
            foreground=c["primary"], spacing1=12, spacing3=4)
        self.txt.tag_configure("subtitle",
            font=("SF Pro", 11, "italic"),
            foreground=c["muted"], spacing3=10)
        self.txt.tag_configure("body",
            font=("SF Pro", 11), foreground=c["text"], spacing3=4)
        self.txt.tag_configure("code",
            font=("Menlo", 10),
            foreground=c["text_strong"],
            background=c["tree_head_bg"],
            lmargin1=20, lmargin2=20, spacing1=4, spacing3=4)
        self.txt.tag_configure("value",
            font=("Menlo", 11, "bold"),
            foreground=c["success"], spacing3=8)

    def _populate_tree(self) -> None:
        from src.glossary import GLOSSARY
        first_item = None
        for cat, entries in GLOSSARY.items():
            cat_id = self.tree.insert("", "end", text=cat, open=True)
            for term in entries:
                item = self.tree.insert(cat_id, "end", text=term,
                                         values=(cat, term))
                if first_item is None:
                    first_item = item
        # Select the first entry so the user sees content immediately
        if first_item is not None:
            self.tree.selection_set(first_item)
            self.tree.focus(first_item)
            self._on_select(None)

    def _on_select(self, _event) -> None:
        from src.glossary import get_entry
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        values = self.tree.item(item, "values")
        if not values or len(values) < 2:
            return   # selected a category header, not an entry
        category, term = values[0], values[1]
        entry = get_entry(category, term)
        if entry is None:
            return
        self._render(term, entry)

    def _render(self, term: str, entry: dict) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")

        # Title
        self.txt.insert("end", f"{term}\n", ("h1",))
        # English subtitle if present and different
        name = entry.get("name") or ""
        if name and name != term:
            self.txt.insert("end", f"{name}\n", ("subtitle",))

        # Summary
        summary = entry.get("summary") or ""
        if summary:
            self.txt.insert("end", "概述\n", ("h2",))
            self.txt.insert("end", summary + "\n", ("body",))

        # Explain
        explain = entry.get("explain") or ""
        if explain:
            self.txt.insert("end", "说明\n", ("h2",))
            # Lines with leading spaces or code-like chars get code style.
            for line in explain.split("\n"):
                if line.startswith("  ") or line.lstrip().startswith(("•", "→", "=", "▶")):
                    self.txt.insert("end", line + "\n", ("code",))
                else:
                    self.txt.insert("end", line + "\n", ("body",))

        # Where used
        where = entry.get("where") or ""
        if where:
            self.txt.insert("end", "在系统里用在哪\n", ("h2",))
            for line in where.split("\n"):
                if line.startswith("  ") or "/" in line and "." in line:
                    self.txt.insert("end", line + "\n", ("code",))
                else:
                    self.txt.insert("end", line + "\n", ("body",))

        # Current value
        value = entry.get("value") or ""
        if value:
            self.txt.insert("end", "当前设置\n", ("h2",))
            self.txt.insert("end", value + "\n", ("value",))

        self.txt.configure(state="disabled")
        self.txt.yview_moveto(0)


if __name__ == "__main__":
    App().mainloop()
