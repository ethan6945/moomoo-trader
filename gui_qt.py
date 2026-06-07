"""PyQt6 control panel for moomoo-trader — modern Bloomberg-style redesign.

Install PyQt6 (one-time):
    .venv/bin/pip install PyQt6

Run:
    .venv/bin/python gui_qt.py
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
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize
from PyQt6.QtGui import (
    QColor, QFont, QTextCursor, QAction, QPainter,
    QPen, QBrush, QLinearGradient, QFontMetrics,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog,
    QHBoxLayout, QVBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QPlainTextEdit, QTextEdit, QLineEdit, QComboBox,
    QTabWidget, QProgressBar, QListWidget, QScrollArea,
    QMessageBox, QInputDialog, QMenu, QMenuBar,
    QSizePolicy, QAbstractItemView, QSpinBox,
    QTreeWidget, QTreeWidgetItem, QScrollBar,
)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.i18n import t as _t, current_lang, set_lang

PID_FILE        = ROOT / "logs" / "scheduler.pid"
LOG_FILE        = ROOT / "logs" / "trader.log"
SCHEDULER_LOG   = ROOT / "logs" / "scheduler.log"
OPEN_TRADES     = ROOT / "data" / "open_trades.json"
STATE_FILE      = ROOT / "data" / "state.json"
ACCOUNT_FILE    = ROOT / "data" / "account.json"
HISTORY_FILE    = ROOT / "data" / "history.jsonl"
BACKTEST_FILE   = ROOT / "data" / "backtest_results.json"
RECONCILE_FILE  = ROOT / "data" / "reconcile.json"
AUDIT_FILE      = ROOT / "data" / "audit.jsonl"
TRADES_FILE     = ROOT / "data" / "trades.jsonl"
WATCHLIST_FILE  = ROOT / "config" / "watchlist.json"
SIGNAL_WL_FILE  = ROOT / "config" / "signal_watchlist.json"
SIGNAL_PID_FILE = ROOT / "logs" / "signal_reporter.pid"
SIGNAL_LOG_FILE = ROOT / "logs" / "signal_reporter.log"
PREFS_FILE      = ROOT / "data" / "prefs.json"
PYTHON          = str(ROOT / ".venv" / "bin" / "python")

REFRESH_MS = 5000

# ── Theme ──────────────────────────────────────────────────────────────────

DARK = {
    "bg":          "#0b0e14",   # deeper, slightly cooler near-black
    "sidebar_bg":  "#12161f",
    "card_bg":     "#1a1f2b",   # richer card surface (subtle elevation)
    "border":      "#2a313c",   # softer hairline
    "primary":     "#4d9dff",   # a touch more vibrant
    "success":     "#3fb950",
    "danger":      "#f85149",
    "warning":     "#d29922",
    "muted":       "#8b949e",
    "text":        "#cdd5e0",   # slightly brighter body text
    "text_strong": "#f0f6fc",
    "accent1":     "#4d9dff",
    "accent2":     "#a371f7",
    "accent3":     "#3fb950",
    "accent4":     "#d29922",
}

LIGHT = {
    "bg":          "#f6f8fa",
    "sidebar_bg":  "#eaeef2",
    "card_bg":     "#ffffff",
    "border":      "#d0d7de",
    "primary":     "#0969da",
    "success":     "#1a7f37",
    "danger":      "#cf222e",
    "warning":     "#9a6700",
    "muted":       "#57606a",
    "text":        "#24292f",
    "text_strong": "#1f2328",
    "accent1":     "#0969da",
    "accent2":     "#8250df",
    "accent3":     "#1a7f37",
    "accent4":     "#9a6700",
}

T: dict = dict(DARK)   # live theme, mutated on switch


def _build_qss(t: dict) -> str:
    return f"""
QMainWindow, QDialog, QWidget {{
    background-color: {t["bg"]};
    color: {t["text"]};
    font-family: "Inter", "-apple-system", "SF Pro Display", "Helvetica Neue", sans-serif;
    font-size: 13px;
}}
QToolTip {{
    background-color: {t["card_bg"]};
    color: {t["text"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
    padding: 4px 8px;
}}
/* sidebar */
Sidebar {{
    background-color: {t["sidebar_bg"]};
    border-right: 1px solid {t["border"]};
}}
/* nav buttons */
NavButton {{
    background-color: transparent;
    color: {t["text"]};
    border: none;
    text-align: left;
    padding: 10px 16px;
    border-radius: 9px;
    font-size: 13px;
}}
NavButton:hover {{
    background-color: {t["border"]};
    color: {t["text_strong"]};
}}
NavButton:pressed {{ background-color: {t["card_bg"]}; }}
NavAccentButton {{
    background-color: {t["primary"]};
    color: #ffffff;
    border: none;
    text-align: left;
    padding: 10px 16px;
    border-radius: 9px;
    font-size: 13px;
    font-weight: bold;
}}
NavAccentButton:hover {{ background-color: {t["success"]}; }}
/* stat cards */
StatCard {{
    background-color: {t["card_bg"]};
    border: 1px solid {t["border"]};
    border-radius: 14px;
}}
/* status/info strips */
StatusStrip, InfoStrip {{
    background-color: {t["card_bg"]};
    border: 1px solid {t["border"]};
    border-radius: 9px;
}}
/* table */
QTableWidget {{
    background-color: {t["bg"]};
    alternate-background-color: {t["sidebar_bg"]};
    gridline-color: {t["border"]};
    border: 1px solid {t["border"]};
    border-radius: 9px;
    color: {t["text"]};
    selection-background-color: {t["primary"]};
    selection-color: #ffffff;
    outline: none;
}}
QTableWidget::item {{ padding: 4px 8px; border: none; }}
QHeaderView::section {{
    background-color: {t["sidebar_bg"]};
    color: {t["muted"]};
    border: none;
    border-bottom: 1px solid {t["border"]};
    padding: 6px 8px;
    font-weight: bold;
    font-size: 11px;
}}
/* text areas */
QPlainTextEdit, QTextEdit {{
    background-color: {t["bg"]};
    color: {t["muted"]};
    border: 1px solid {t["border"]};
    border-radius: 9px;
    font-family: "Menlo", "Monaco", "Courier New", monospace;
    font-size: 11px;
    selection-background-color: {t["primary"]};
}}
/* generic buttons */
QPushButton {{
    background-color: {t["border"]};
    color: {t["text"]};
    border: none;
    border-radius: 9px;
    padding: 6px 14px;
    font-size: 13px;
}}
QPushButton:hover {{ background-color: {t["card_bg"]}; color: {t["text_strong"]}; }}
QPushButton:disabled {{ background-color: {t["sidebar_bg"]}; color: {t["muted"]}; }}
PrimaryButton {{
    background-color: {t["success"]};
    color: #ffffff;
    border: none;
    border-radius: 9px;
    padding: 7px 16px;
    font-weight: bold;
    font-size: 13px;
}}
PrimaryButton:hover {{ background-color: #46d260; }}
PrimaryButton:disabled {{ background-color: {t["border"]}; color: {t["muted"]}; }}
DangerButton {{
    background-color: transparent;
    color: {t["danger"]};
    border: 1px solid {t["danger"]};
    border-radius: 9px;
    padding: 7px 16px;
    font-size: 13px;
}}
DangerButton:hover {{ background-color: {t["danger"]}; color: #ffffff; }}
/* input fields */
QLineEdit, QComboBox, QSpinBox {{
    background-color: {t["sidebar_bg"]};
    color: {t["text"]};
    border: 1px solid {t["border"]};
    border-radius: 9px;
    padding: 4px 8px;
    font-size: 13px;
}}
QLineEdit:focus {{ border-color: {t["primary"]}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background-color: {t["sidebar_bg"]};
    border: 1px solid {t["border"]};
    selection-background-color: {t["primary"]};
    selection-color: #ffffff;
    color: {t["text"]};
}}
/* tabs */
QTabWidget::pane {{
    border: 1px solid {t["border"]};
    background-color: {t["bg"]};
    border-radius: 0 6px 6px 6px;
    top: -1px;
}}
QTabBar::tab {{
    background-color: {t["sidebar_bg"]};
    color: {t["muted"]};
    border: 1px solid {t["border"]};
    border-bottom: none;
    padding: 6px 18px;
    margin-right: 2px;
    border-radius: 9px 6px 0 0;
}}
QTabBar::tab:selected {{
    background-color: {t["bg"]};
    color: {t["text_strong"]};
    border-top: 2px solid {t["primary"]};
}}
QTabBar::tab:hover:!selected {{ background-color: {t["border"]}; color: {t["text"]}; }}
/* scrollbars */
QScrollBar:vertical {{
    background-color: {t["bg"]}; width: 8px; margin: 0; border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background-color: {t["border"]}; border-radius: 4px; min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background-color: {t["muted"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    height: 0; background: none;
}}
QScrollBar:horizontal {{
    background-color: {t["bg"]}; height: 8px; border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background-color: {t["border"]}; border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; background: none;
}}
/* tree / list */
QTreeWidget, QListWidget {{
    background-color: {t["bg"]};
    border: 1px solid {t["border"]};
    border-radius: 9px;
    color: {t["text"]};
    outline: none;
}}
QTreeWidget::item, QListWidget::item {{ padding: 4px; }}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background-color: {t["primary"]}; color: #ffffff;
}}
QTreeWidget::item:hover, QListWidget::item:hover {{
    background-color: {t["sidebar_bg"]};
}}
/* progress bar */
QProgressBar {{
    background-color: {t["border"]};
    border: none; border-radius: 4px; height: 8px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{
    background-color: {t["primary"]}; border-radius: 4px;
}}
/* menu */
QMenuBar {{
    background-color: {t["sidebar_bg"]};
    color: {t["text"]};
    border-bottom: 1px solid {t["border"]};
}}
QMenuBar::item:selected {{ background-color: {t["border"]}; }}
QMenu {{
    background-color: {t["card_bg"]};
    color: {t["text"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
}}
QMenu::item:selected {{ background-color: {t["primary"]}; color: #ffffff; }}
/* divider */
Divider {{ background-color: {t["border"]}; }}
"""


# ── Custom widget subclasses for QSS targeting ─────────────────────────────

class Sidebar(QWidget):       pass
class StatusStrip(QWidget):   pass
class InfoStrip(QWidget):     pass
class Divider(QFrame):        pass
class NavButton(QPushButton):      pass
class NavAccentButton(QPushButton): pass
class PrimaryButton(QPushButton):  pass
class DangerButton(QPushButton):   pass


class StatCard(QFrame):
    """Card with colored accent bar + title + value label."""

    def __init__(self, title: str, accent_color: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        accent = QFrame()
        accent.setFixedHeight(3)
        accent.setStyleSheet(f"background-color: {accent_color}; border: none; border-radius: 0;")
        accent.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(accent)

        inner = QWidget()
        inner.setStyleSheet(f"background-color: {T['card_bg']}; border: none;")
        self._inner = inner
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(18, 14, 18, 16)
        inner_lay.setSpacing(4)
        layout.addWidget(inner)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(f"color: {T['muted']}; font-size: 10px;")
        inner_lay.addWidget(self.title_lbl)

        self.value_lbl = QLabel("—")
        self.value_lbl.setStyleSheet(
            f"color: {T['text_strong']}; font-size: 26px; font-weight: bold;"
        )
        inner_lay.addWidget(self.value_lbl)

        self._extra_lay = inner_lay

    def add_widget(self, w: QWidget) -> None:
        self._extra_lay.addWidget(w)

    def set_value(self, text: str, color: str | None = None) -> None:
        self.value_lbl.setText(text)
        if color:
            self.value_lbl.setStyleSheet(
                f"color: {color}; font-size: 26px; font-weight: bold;"
            )

    def recolor(self, t: dict) -> None:
        self._inner.setStyleSheet(f"background-color: {t['card_bg']}; border: none;")
        self.title_lbl.setStyleSheet(f"color: {t['muted']}; font-size: 10px;")


# ── Utility helpers ────────────────────────────────────────────────────────

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
    from src import clock
    return clock.ny_now()


def in_market_hours() -> bool:
    now = ny_now()
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= m <= 16 * 60


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def load_prefs() -> dict:
    try:
        return json.loads(PREFS_FILE.read_text())
    except Exception:
        return {}


def save_prefs(key: str, val) -> None:
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        p = json.loads(PREFS_FILE.read_text())
    except Exception:
        p = {}
    p[key] = val
    PREFS_FILE.write_text(json.dumps(p, indent=2, ensure_ascii=False))


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


def _detect_macos_dark() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0 and "Dark" in r.stdout
    except Exception:
        return False


def _pill(text: str, color: str | None = None) -> QLabel:
    lbl = QLabel(text)
    c = color or T["text"]
    lbl.setStyleSheet(f"color: {c}; font-size: 11px; padding: 0 6px;")
    return lbl


# ── Worker threads ─────────────────────────────────────────────────────────

class ScanWorker(QThread):
    done = pyqtSignal()
    error = pyqtSignal(str)

    def run(self):
        try:
            subprocess.run(
                [PYTHON, "-m", "src.main", "scan"],
                cwd=str(ROOT),
                stdout=open(SCHEDULER_LOG, "a"),
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.done.emit()


class ManualCloseWorker(QThread):
    done = pyqtSignal(str)

    def __init__(self, sym: str):
        super().__init__()
        self._sym = sym

    def run(self):
        try:
            from src.moomoo_client import client
            from src.executor import manual_close
            with client() as c:
                res = manual_close(c, self._sym)
            self.done.emit(
                _t("closed_toast", symbol=res["symbol"], price=res["price"], pnl=res["pnl"])
            )
        except Exception as e:
            self.done.emit(f"⚠ Close failed: {e}")


class ClockSyncWorker(QThread):
    done = pyqtSignal(str)

    def run(self):
        try:
            from src import clock
            s = clock.force_refresh()
            self.done.emit(
                f"Clock sync: src={s['source']} drift={s['last_drift_sec']:+.1f}s"
            )
        except Exception as e:
            self.done.emit(f"Clock sync failed: {e}")


# ── Main Window ────────────────────────────────────────────────────────────

class App(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(_t("app_title"))
        self.resize(1240, 760)
        self.setMinimumSize(1024, 640)

        prefs = load_prefs()
        theme_pref = prefs.get("theme", "auto")
        if theme_pref == "dark" or (theme_pref == "auto" and _detect_macos_dark()):
            T.update(DARK)
        else:
            T.update(LIGHT)

        self._apply_theme()
        self._build_menu()
        self._build()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(REFRESH_MS)
        QTimer.singleShot(0, self._refresh)

    # ── theme ──────────────────────────────────────────────────────────────

    def _apply_theme(self):
        QApplication.instance().setStyleSheet(_build_qss(T))
        save_prefs("theme", "dark" if T is DARK or T["bg"] == DARK["bg"] else "light")

    def _switch_theme(self, name: str):
        save_prefs("theme", name)
        if name == "dark":
            T.update(DARK)
        elif name == "light":
            T.update(LIGHT)
        else:
            T.update(DARK if _detect_macos_dark() else LIGHT)
        self._apply_theme()
        QTimer.singleShot(50, self._refresh)

    # ── menu bar ───────────────────────────────────────────────────────────

    def _build_menu(self):
        bar = self.menuBar()

        lang_menu = bar.addMenu(_t("menu_language"))
        for code, label in (("zh", _t("menu_lang_zh")), ("en", _t("menu_lang_en"))):
            act = QAction(label, self)
            act.triggered.connect(lambda checked, c=code: self._switch_lang(c))
            lang_menu.addAction(act)

        theme_menu = bar.addMenu(_t("menu_theme"))
        for name, label in (
            ("light", _t("menu_theme_light")),
            ("dark",  _t("menu_theme_dark")),
            ("auto",  _t("menu_theme_auto")),
        ):
            act = QAction(label, self)
            act.triggered.connect(lambda checked, n=name: self._switch_theme(n))
            theme_menu.addAction(act)

        help_menu = bar.addMenu(_t("menu_help"))
        help_menu.addAction(_t("menu_help"), self.open_help)
        help_menu.addSeparator()
        help_menu.addAction("🔑 API Keys", self.open_api_keys)

    def _switch_lang(self, lang: str):
        if lang == current_lang():
            return
        set_lang(lang)
        QMessageBox.information(self, _t("lang_switched_title"), _t("lang_switched_body"))

    # ── layout ─────────────────────────────────────────────────────────────

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_lay = QHBoxLayout(central)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # Sidebar
        self._sidebar = Sidebar()
        self._sidebar.setFixedWidth(200)
        sb_lay = QVBoxLayout(self._sidebar)
        sb_lay.setContentsMargins(0, 0, 0, 0)
        sb_lay.setSpacing(0)
        self._build_sidebar(sb_lay)
        root_lay.addWidget(self._sidebar)

        # 1px divider
        div = Divider()
        div.setFixedWidth(1)
        root_lay.addWidget(div)

        # Content
        content = QWidget()
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(12, 8, 12, 8)
        content_lay.setSpacing(6)
        self._build_content(content_lay)
        root_lay.addWidget(content, 1)

    def _nav(self, parent_lay: QVBoxLayout, text: str, callback,
             accent: bool = False) -> QPushButton:
        btn = NavAccentButton(text) if accent else NavButton(text)
        btn.setFixedHeight(36)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(callback)
        parent_lay.addWidget(btn)
        return btn

    def _build_sidebar(self, lay: QVBoxLayout):
        # Logo
        logo_w = QWidget()
        logo_lay = QVBoxLayout(logo_w)
        logo_lay.setContentsMargins(16, 20, 16, 14)
        logo_lay.setSpacing(2)
        title = QLabel("MooMoo Trader")
        title.setStyleSheet(
            f"color: {T['primary']}; font-size: 14px; font-weight: bold;"
        )
        sub = QLabel("Auto Trading Bot")
        sub.setStyleSheet(f"color: {T['muted']}; font-size: 10px;")
        logo_lay.addWidget(title)
        logo_lay.addWidget(sub)
        lay.addWidget(logo_w)

        div = Divider()
        div.setFixedHeight(1)
        lay.addWidget(div)

        # Spacer
        lay.addSpacing(8)

        # Primary controls
        self.btn_start = self._nav(lay, f"▶  {_t('btn_start')}",  self.start_scheduler, accent=True)
        self.btn_stop  = self._nav(lay, f"■  {_t('btn_stop')}",   self.stop_scheduler)
        self.btn_scan  = self._nav(lay, f"⚡ {_t('btn_scan')}",   self.scan_now)
        self.btn_logs  = self._nav(lay, f"📋 {_t('btn_log')}",    self.open_logs)

        lay.addSpacing(6)
        div2 = Divider()
        div2.setFixedHeight(1)
        lay.addWidget(div2)

        # Section header
        sec = QLabel("  VIEWS")
        sec.setStyleSheet(
            f"color: {T['muted']}; font-size: 10px; font-weight: bold; padding: 8px 16px 2px 16px;"
        )
        lay.addWidget(sec)

        self._nav(lay, f"📈 {_t('btn_history')}",   self.open_history)
        self._nav(lay, f"🔬 {_t('btn_backtest')}",  self.open_backtest)
        self._nav(lay, f"📊 {_t('btn_equity')}",    self.open_equity)
        self._nav(lay, f"🔍 {_t('btn_audit')}",     self.open_audit)
        self._nav(lay, f"🗺  {_t('btn_sectors')}",  self.open_sectors)
        self._nav(lay, "📡 Signal Reporter",          self.open_signal_reporter)
        self._nav(lay, "✅ Approvals",                 self.open_approvals)

        lay.addStretch(1)
        lay.addSpacing(8)

    def _build_content(self, lay: QVBoxLayout):
        # ── Status strip ──────────────────────────────────────────────────
        self._status_strip = StatusStrip()
        ss_lay = QHBoxLayout(self._status_strip)
        ss_lay.setContentsMargins(12, 6, 12, 6)
        ss_lay.setSpacing(0)
        self.lbl_scheduler = _pill(f"{_t('scheduler')}: ?")
        self.lbl_opend     = _pill(f"{_t('opend')}: ?")
        self.lbl_unlock    = _pill(f"{_t('trade')}: ?")
        self.lbl_market    = _pill(f"{_t('market')}: ?")
        self.lbl_clock     = _pill(f"🕐 --:--")
        self.lbl_clock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_clock.mousePressEvent = lambda _: ClockSyncWorker(parent=self).start()
        self.lbl_heart     = _pill(_t("last_scan_none"))
        self.lbl_regime    = _pill(_t("regime_none"))
        for w in (self.lbl_scheduler, self.lbl_opend, self.lbl_unlock,
                  self.lbl_market, self.lbl_clock, self.lbl_heart, self.lbl_regime):
            ss_lay.addWidget(w)
            sep = QLabel(" │ ")
            sep.setStyleSheet(f"color: {T['border']}; font-size: 13px;")
            ss_lay.addWidget(sep)
        ss_lay.addStretch(1)
        lay.addWidget(self._status_strip)

        # ── 4 stat cards ──────────────────────────────────────────────────
        cards_row = QWidget()
        cards_lay = QHBoxLayout(cards_row)
        cards_lay.setContentsMargins(0, 0, 0, 0)
        cards_lay.setSpacing(14)

        self._card_budget   = StatCard(_t("budget_label_total"),    T["accent1"])
        self._card_deployed = StatCard(_t("budget_label_deployed"),  T["accent2"])
        self._card_today    = StatCard(_t("budget_label_today_pnl"), T["accent3"])
        self._card_total    = StatCard(_t("budget_label_total_pnl"), T["accent4"])

        # Budget card: clickable value + edit button
        budget_row_w = QWidget()
        budget_row_w.setStyleSheet("background: transparent;")
        budget_row_lay = QHBoxLayout(budget_row_w)
        budget_row_lay.setContentsMargins(0, 0, 0, 0)
        budget_row_lay.setSpacing(4)
        self.lbl_budget = QLabel("$—")
        self.lbl_budget.setStyleSheet(
            f"color: {T['text_strong']}; font-size: 26px; font-weight: bold;"
        )
        self.lbl_budget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_budget.mousePressEvent = lambda _: self._edit_budget()
        edit_btn = QPushButton("✎")
        edit_btn.setFixedSize(26, 26)
        edit_btn.setStyleSheet(
            f"QPushButton {{background: {T['border']}; border-radius: 4px; font-size: 11px; padding: 2px;}}"
        )
        edit_btn.clicked.connect(self._edit_budget)
        budget_row_lay.addWidget(self.lbl_budget)
        budget_row_lay.addWidget(edit_btn)
        budget_row_lay.addStretch(1)
        self._card_budget.add_widget(budget_row_w)

        # Deployed card: text + progress bar
        self.lbl_invested = QLabel("$— / $—  (—%)")
        self.lbl_invested.setStyleSheet(
            f"color: {T['text']}; font-family: Menlo, monospace; font-size: 11px; font-weight: bold;"
        )
        self.bar_deployed = QProgressBar()
        self.bar_deployed.setMaximum(100)
        self.bar_deployed.setFixedHeight(8)
        self.bar_deployed.setStyleSheet(
            f"QProgressBar {{ background: {T['border']}; border: none; border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background: {T['accent2']}; border-radius: 4px; }}"
        )
        self._card_deployed.value_lbl.hide()
        self._card_deployed.add_widget(self.lbl_invested)
        self._card_deployed.add_widget(self.bar_deployed)

        for card in (self._card_budget, self._card_deployed, self._card_today, self._card_total):
            cards_lay.addWidget(card)
        lay.addWidget(cards_row)

        # ── Info strip ────────────────────────────────────────────────────
        self._info_strip = InfoStrip()
        is_lay = QHBoxLayout(self._info_strip)
        is_lay.setContentsMargins(12, 5, 12, 5)
        is_lay.setSpacing(0)
        self.lbl_cash      = _pill(f"{_t('broker_cash')}: —")
        self.lbl_positions = _pill(f"{_t('positions')}: —")
        self.lbl_streak    = _pill(f"{_t('loss_streak')}: —")
        self.lbl_halted    = _pill(f"{_t('halted')}: —")
        self.lbl_halted.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_halted.mousePressEvent = lambda _: self._reset_halt()
        self.lbl_recon     = _pill(f"{_t('reconcile')}: —")
        self.lbl_ai        = _pill(f"{_t('ai_model')}: —")
        self.lbl_vix       = _pill("VIX: —")
        self.lbl_env       = _pill(f"{_t('env')}: —")
        for w in (self.lbl_cash, self.lbl_positions, self.lbl_streak, self.lbl_halted,
                  self.lbl_recon, self.lbl_ai, self.lbl_vix, self.lbl_env):
            is_lay.addWidget(w)
            sep = QLabel(" │ ")
            sep.setStyleSheet(f"color: {T['border']};")
            is_lay.addWidget(sep)
        is_lay.addStretch(1)
        lay.addWidget(self._info_strip)

        # ── Config row ────────────────────────────────────────────────────
        cfg_w = QWidget()
        cfg_lay = QHBoxLayout(cfg_w)
        cfg_lay.setContentsMargins(4, 0, 4, 0)
        cfg_lay.setSpacing(16)
        self.lbl_thresh   = _pill(f"{_t('entry_threshold')} —", T["muted"])
        self.lbl_interval = _pill(f"{_t('scan_every')} —",      T["muted"])
        self.lbl_hold     = _pill(f"{_t('max_hold')} —",        T["muted"])
        self.lbl_ml_cfg   = _pill("ML: —",                      T["muted"])
        self.lbl_strat    = _pill("🎯 —",                       T["muted"])
        for w in (self.lbl_thresh, self.lbl_interval, self.lbl_hold, self.lbl_ml_cfg, self.lbl_strat):
            cfg_lay.addWidget(w)
        cfg_lay.addStretch(1)
        lay.addWidget(cfg_w)

        # ── Positions section ─────────────────────────────────────────────
        pos_header = QLabel(f"  {_t('positions_section')}")
        pos_header.setStyleSheet(
            f"color: {T['muted']}; font-size: 11px; font-weight: bold; padding: 2px 0;"
        )
        lay.addWidget(pos_header)

        self.pos_table = QTableWidget(0, 8)
        self.pos_table.setHorizontalHeaderLabels([
            _t("col_symbol"), _t("col_qty"), _t("col_entry"), _t("col_last"),
            _t("col_stop"), _t("col_tp"), _t("col_atr"), _t("col_pnl"),
        ])
        self.pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pos_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.pos_table.verticalHeader().setVisible(False)
        self.pos_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pos_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pos_table.setAlternatingRowColors(True)
        self.pos_table.setMaximumHeight(130)
        self.pos_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pos_table.customContextMenuRequested.connect(self._pos_context_menu)
        lay.addWidget(self.pos_table)

        # ── Log tail section ──────────────────────────────────────────────
        log_header = QLabel(f"  {_t('log_section')}")
        log_header.setStyleSheet(
            f"color: {T['muted']}; font-size: 11px; font-weight: bold; padding: 2px 0;"
        )
        lay.addWidget(log_header)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(60)
        self.log_view.setFixedHeight(66)   # ~3 lines — compact recent-activity strip
        lay.addWidget(self.log_view)
        lay.addStretch(1)                  # pack content to the top; no big empty log

    # ── Actions ────────────────────────────────────────────────────────────

    def start_scheduler(self):
        if read_pid() is not None:
            self._toast(_t("scheduler_already_running"))
            return
        if not opend_reachable():
            self._toast(_t("opend_unreachable"))
            return
        SCHEDULER_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_fd = open(SCHEDULER_LOG, "a")
        cmd = []
        caf = "/usr/bin/caffeinate"
        if Path(caf).exists():
            cmd = [caf, "-is"]
        cmd += [PYTHON, "-m", "src.main", "run"]
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=log_fd, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        self._toast(_t("scheduler_started", pid=proc.pid))

    def stop_scheduler(self):
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

    def scan_now(self):
        if not opend_reachable():
            self._toast(_t("opend_unreachable"))
            return
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText(_t("btn_scanning"))
        self._scan_worker = ScanWorker()
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.error.connect(lambda e: self._toast(f"⚠ Scan error: {e}"))
        self._scan_worker.start()

    def _on_scan_done(self):
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText(f"⚡ {_t('btn_scan')}")

    def open_logs(self):
        subprocess.run(["open", str(LOG_FILE)])

    def open_history(self):
        HistoryDialog(self).show()

    def open_backtest(self):
        BacktestDialog(self).show()

    def open_equity(self):
        EquityDialog(self).show()

    def open_audit(self):
        AuditDialog(self).show()

    def open_sectors(self):
        SectorHeatmapDialog(self).show()

    def open_signal_reporter(self):
        SignalReporterDialog(self).show()

    def open_approvals(self):
        ApprovalsDialog(self).show()

    def open_api_keys(self):
        ApiKeysDialog(self).show()

    def open_help(self):
        HelpDialog(self).show()

    def _edit_budget(self):
        # 2026-06-03 dynamic capital: the budget is now a RUNTIME value in db
        # state ('budget_usd'). Changing it takes effect on the NEXT scan with
        # NO restart. Sizing also auto-caps to live account equity, so this is
        # the owner's allocated ceiling, not a hardcoded constant.
        from src import db, risk_manager
        current = float(risk_manager.budget_usd())
        val, ok = QInputDialog.getDouble(
            self, "Set Trading Budget",
            f"Allocated trading budget (currently ${current:,.2f}).\n"
            f"Takes effect next scan — no restart. Sizing auto-caps to live equity.",
            current, 0, 9_999_999, 2,
        )
        if not ok or val <= 0 or abs(val - current) < 1:
            return
        try:
            db.update_state({"budget_usd": val})
            self._toast(f"💰 Budget set to ${val:,.0f} (live — no restart)")
        except Exception as e:
            self._toast(f"⚠ Budget update failed: {e}")

    def _reset_halt(self):
        state = read_json(STATE_FILE)
        if not state.get("halted"):
            return
        reply = QMessageBox.question(
            self, _t("halted_reset_confirm_title"), _t("halted_reset_confirm_body"),
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            from src import db
            db.atomic_state(lambda s: {"halted": False, "loss_streak_days": 0})
            self._toast(_t("halted_reset_done"))
        except Exception as e:
            self._toast(f"reset halt failed: {e}")

    # ── Positions context menu ─────────────────────────────────────────────

    def _pos_context_menu(self, pos):
        idx = self.pos_table.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        sym = self.pos_table.item(row, 0).text() if self.pos_table.item(row, 0) else None
        if not sym:
            return
        menu = QMenu(self)
        close_act = menu.addAction(_t("menu_close_now"))
        edit_act  = menu.addAction(_t("menu_edit_stop"))
        act = menu.exec(self.pos_table.viewport().mapToGlobal(pos))
        if act == close_act:
            self._do_close(sym)
        elif act == edit_act:
            self._do_edit_stop(sym)

    def _do_close(self, sym: str):
        reply = QMessageBox.question(
            self, _t("confirm_close_title", symbol=sym), _t("confirm_close_body")
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._close_worker = ManualCloseWorker(sym)
        self._close_worker.done.connect(self._toast)
        self._close_worker.start()

    def _do_edit_stop(self, sym: str):
        trades = read_json(OPEN_TRADES)
        if sym not in trades:
            self._toast(_t("not_in_open_trades", symbol=sym))
            return
        current = trades[sym].get("stop_loss", 0)
        val, ok = QInputDialog.getDouble(
            self, _t("edit_stop_title", symbol=sym),
            _t("edit_stop_body", current=current),
            current, 0, 99999, 2,
        )
        if not ok or val <= 0:
            return
        def _edit():
            try:
                from src.moomoo_client import client
                from src.executor import edit_stop
                with client() as c:
                    edit_stop(c, sym, val)
            except Exception as e:
                pass
        threading.Thread(target=_edit, daemon=True).start()

    # ── Refresh ────────────────────────────────────────────────────────────

    def _refresh(self):
        pid    = read_pid()
        opend  = opend_reachable()
        mkt    = in_market_hours()
        account = read_json(ACCOUNT_FILE)
        state   = read_json(STATE_FILE)

        # Status strip
        sched_color = T["success"] if pid else T["danger"]
        self.lbl_scheduler.setText(
            f"{'🟢' if pid else '🔴'} {_t('scheduler')}: "
            f"{_t('scheduler_running') if pid else _t('scheduler_stopped')}"
        )
        self.lbl_opend.setText(f"{'🟢' if opend else '🔴'} {_t('opend')}")
        self.lbl_market.setText(
            f"{'🟢' if mkt else '🔴'} {_t('market_open') if mkt else _t('market_closed')}"
        )

        # Trade unlock
        unlocked = False
        if account.get("ts"):
            try:
                ts = datetime.fromisoformat(account["ts"])
                age_min = (datetime.now(ts.tzinfo) - ts).total_seconds() / 60
                interval = float(account.get("scan_interval_min", 30) or 30)
                unlocked = age_min < interval + 5
            except Exception:
                pass
        self.lbl_unlock.setText(
            f"{'🔓' if unlocked else '🔒'} {_t('trade_unlocked') if unlocked else _t('trade_locked')}"
        )

        # Clock
        clk = account.get("clock") or {}
        drift = clk.get("last_drift_sec", 0)
        badge = "🔴" if abs(drift) > 30 else ("🟡" if abs(drift) > 5 else "🟢")
        try:
            self.lbl_clock.setText(f"{badge} ET {ny_now().strftime('%H:%M')}")
        except Exception:
            self.lbl_clock.setText(f"{badge} ET --:--")

        # Heartbeat
        interval = float(account.get("scan_interval_min", 30) or 30)
        last_scan = account.get("last_scan_utc", "")
        if last_scan:
            try:
                t = datetime.fromisoformat(last_scan)
                from datetime import timezone
                now_ref = datetime.now(timezone.utc) if t.tzinfo else datetime.utcnow()
                age = (now_ref - t).total_seconds() / 60
                green_max, yellow_max = interval + 5, interval * 3
                if age >= yellow_max and not mkt and pid:
                    self.lbl_heart.setText(_t("market_closed_badge", ago=int(age)))
                else:
                    hb = "🟢" if age < green_max else ("🟡" if age < yellow_max else "🔴")
                    self.lbl_heart.setText(f"{hb} {age:.0f}m {_t('ago')}")
            except Exception:
                self.lbl_heart.setText(f"{_t('last_scan')}: ?")
        else:
            self.lbl_heart.setText(_t("last_scan_none"))

        # Regime
        reg = account.get("regime", "")
        if reg:
            rb = "🟢" if reg == "BULL" else ("🟡" if reg == "NEUTRAL" else "🔴")
            self.lbl_regime.setText(f"{_t('regime')}: {rb} {reg}")
        else:
            self.lbl_regime.setText(_t("regime_none"))

        # Budget card
        from src.config import settings as _live
        budget   = _live.account_usd or account.get("budget", 0) or 0
        invested = account.get("invested", 0)
        pct      = (invested / budget * 100) if budget else 0
        self.lbl_budget.setText(f"${budget:,.0f}")
        self.lbl_invested.setText(f"${invested:,.2f} / ${budget:,.0f}  ({pct:.1f}%)")
        self.bar_deployed.setValue(int(min(100, pct)))

        # PnL cards
        total  = account.get("total_pnl", 0)
        today  = account.get("realized_pnl_today", 0)
        unreal = account.get("unrealized_pnl", 0)
        realiz = account.get("realized_pnl_total", 0)
        self._card_today.set_value(
            f"${today:+,.2f}",
            T["success"] if today >= 0 else T["danger"],
        )
        self._card_total.set_value(
            f"${total:+,.2f}",
            T["success"] if total >= 0 else T["danger"],
        )

        # Info strip
        cash = account.get("cash") or state.get("starting_cash")
        ts_short = account.get("ts", "")[-8:-3] if account.get("ts") else ""
        self.lbl_cash.setText(
            f"{_t('broker_cash')}: ${cash:,.2f}" + (f" ({ts_short})" if ts_short else "")
            if cash else _t("broker_cash_waiting")
        )
        trades = read_json(OPEN_TRADES)
        self.lbl_positions.setText(f"{_t('positions')}: {len(trades)}")
        self.lbl_streak.setText(f"{_t('loss_streak')}: {state.get('loss_streak_days', 0)}d")
        self.lbl_halted.setText(
            _t("halted_label_yes") if state.get("halted") else _t("halted_label_no")
        )
        recon = read_json(RECONCILE_FILE)
        if recon:
            rb = "🟢" if recon.get("ok") else "⚠️"
            self.lbl_recon.setText(f"{_t('reconcile')}: {rb} {recon.get('summary', '—')}")
        else:
            self.lbl_recon.setText(_t("reconcile_none"))
        self.lbl_ai.setText(f"{_t('ai_model')}: {account.get('ai_model', '—')}")
        vix = account.get("vix", 0)
        if vix:
            vb = "🔴" if vix > 35 else ("🟡" if vix > 25 else "🟢")
            self.lbl_vix.setText(f"VIX: {vb} {vix:.1f}")
        env = account.get("trade_env", "—")
        env_b = _t("env_paper") if env == "SIMULATE" else (_t("env_real") if env == "REAL" else "—")
        self.lbl_env.setText(f"{_t('env')}: {env_b}")

        # Config row
        self.lbl_thresh.setText(f"{_t('entry_threshold')} {account.get('entry_threshold', '—')}")
        self.lbl_interval.setText(f"{_t('scan_every')} {account.get('scan_interval_min', '—')}m")
        self.lbl_hold.setText(f"{_t('max_hold')} {account.get('max_hold_days', '—')}d")
        self.lbl_ml_cfg.setText("")   # ML subsystem removed 2026-06-03

        # Positions table
        per_pos = account.get("per_position") or {}
        self.pos_table.setRowCount(0)
        for sym, tr in trades.items():
            live   = per_pos.get(sym, {})
            last   = live.get("last") or 0
            pl_val = live.get("pl_val") or 0
            row = self.pos_table.rowCount()
            self.pos_table.insertRow(row)
            cells = [
                sym,
                str(tr.get("qty", "—")),
                f"${tr.get('entry_price', 0):.2f}",
                f"${last:.2f}" if last else "—",
                f"${tr.get('stop_loss', 0):.2f}",
                f"${tr.get('take_profit', 0):.2f}",
                f"{tr.get('atr', 0):.2f}",
                f"${pl_val:+.2f} ({live.get('pl_ratio', 0):+.1f}%)" if last else "—",
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 7 and last:
                    item.setForeground(
                        QColor(T["success"]) if pl_val > 0 else
                        QColor(T["danger"])  if pl_val < 0 else
                        QColor(T["text"])
                    )
                self.pos_table.setItem(row, col, item)

        self._tail_log()

    def _tail_log(self):
        if not LOG_FILE.exists():
            return
        try:
            with open(LOG_FILE, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 10000))
                tail = f.read().decode(errors="replace")
        except OSError:
            return
        NOISE = ("open_context_base", "on_disconnect", "New connect",
                 "quota_metric", "GenerateRequests", "GenerateContent",
                 "violations", "key:", "value:", "retry_delay",
                 "Please retry", "description:", "url:", "links {")
        lines = [l for l in tail.splitlines() if not any(n in l for n in NOISE)]
        body = "\n".join(lines[-100:])
        cursor = self.log_view.textCursor()
        was_at_end = cursor.atEnd()
        self.log_view.setPlainText(body)
        if was_at_end:
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _toast(self, msg: str):
        self.log_view.appendPlainText(f"\n[GUI] {msg}")
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)


# ── History Dialog ─────────────────────────────────────────────────────────

class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Trading History")
        self.resize(860, 520)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # All snapshots
        t1 = QWidget()
        t1_lay = QVBoxLayout(t1)
        cols = ["TS", "Invested", "Unrealized", "Realized", "Total P&L", "Positions", "Symbols"]
        widths = [150, 90, 100, 100, 100, 80, 200]
        tree = QTableWidget(0, len(cols))
        tree.setHorizontalHeaderLabels(cols)
        for i, w in enumerate(widths):
            tree.setColumnWidth(i, w)
        tree.setAlternatingRowColors(True)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.verticalHeader().setVisible(False)
        t1_lay.addWidget(tree)
        tabs.addTab(t1, "All Snapshots")

        # Weekly
        t2 = QWidget()
        t2_lay = QVBoxLayout(t2)
        wcols = ["Week", "Last TS", "Invested", "Unrealized", "Realized", "Total P&L", "Positions"]
        wtree = QTableWidget(0, len(wcols))
        wtree.setHorizontalHeaderLabels(wcols)
        wtree.setAlternatingRowColors(True)
        wtree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        wtree.verticalHeader().setVisible(False)
        t2_lay.addWidget(wtree)
        tabs.addTab(t2, "Weekly")

        records = []
        if HISTORY_FILE.exists():
            for line in HISTORY_FILE.read_text().strip().split("\n"):
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass

        for r in records[-200:]:
            row = tree.rowCount(); tree.insertRow(row)
            vals = [
                r.get("ts", "")[-15:],
                f"${r.get('invested', 0):,.0f}",
                f"${r.get('unrealized_pnl', 0):+,.2f}",
                f"${r.get('realized_pnl_total', 0):+,.2f}",
                f"${r.get('total_pnl', 0):+,.2f}",
                str(r.get("positions_count", 0)),
                ",".join(r.get("symbols", []))[:30],
            ]
            for col, v in enumerate(vals):
                tree.setItem(row, col, QTableWidgetItem(v))

        weekly: dict = {}
        for r in records:
            weekly[r.get("week", "?")] = r
        for week, r in sorted(weekly.items()):
            row = wtree.rowCount(); wtree.insertRow(row)
            vals = [
                week, r.get("ts", "")[-15:],
                f"${r.get('invested', 0):,.0f}",
                f"${r.get('unrealized_pnl', 0):+,.2f}",
                f"${r.get('realized_pnl_total', 0):+,.2f}",
                f"${r.get('total_pnl', 0):+,.2f}",
                str(r.get("positions_count", 0)),
            ]
            for col, v in enumerate(vals):
                wtree.setItem(row, col, QTableWidgetItem(v))


# ── Backtest Dialog ────────────────────────────────────────────────────────

class BacktestDialog(QDialog):
    _done   = pyqtSignal(dict)
    _error  = pyqtSignal(str)
    _prog   = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backtest")
        self.resize(900, 680)
        self._running = False
        self._done.connect(self._show_results)
        self._error.connect(lambda m: self.lbl_prog.setText(f"Error: {m}"))
        self._prog.connect(lambda m: self.lbl_prog.setText(m))
        self._build()
        if BACKTEST_FILE.exists():
            try:
                self._show_results(json.loads(BACKTEST_FILE.read_text()))
                self.lbl_prog.setText("Loaded saved results.")
            except Exception:
                pass

    def _build(self):
        lay = QVBoxLayout(self)

        # Config row
        cfg = QWidget()
        cfg_lay = QHBoxLayout(cfg)
        cfg_lay.setContentsMargins(0, 0, 0, 0)
        cfg_lay.setSpacing(12)

        cfg_lay.addWidget(QLabel("Days:"))
        self.cb_days = QComboBox(); self.cb_days.addItems(["60","90","180","365"]); self.cb_days.setCurrentText("180"); self.cb_days.setFixedWidth(70)
        cfg_lay.addWidget(self.cb_days)

        cfg_lay.addWidget(QLabel("Timeframe:"))
        self.cb_tf = QComboBox(); self.cb_tf.addItems(["HOUR_1","DAILY"]); self.cb_tf.setFixedWidth(90)
        cfg_lay.addWidget(self.cb_tf)

        cfg_lay.addWidget(QLabel("Threshold:"))
        self.le_thresh = QLineEdit("70"); self.le_thresh.setFixedWidth(60)
        cfg_lay.addWidget(self.le_thresh)

        cfg_lay.addWidget(QLabel("Tickers:"))
        self.le_tickers = QLineEdit(); self.le_tickers.setPlaceholderText("blank = watchlist"); self.le_tickers.setFixedWidth(180)
        cfg_lay.addWidget(self.le_tickers)

        self.btn_run = PrimaryButton("▶  Run Backtest")
        self.btn_run.clicked.connect(self._run)
        cfg_lay.addWidget(self.btn_run)
        cfg_lay.addStretch(1)
        lay.addWidget(cfg)

        # Progress
        prog_row = QWidget()
        pr_lay = QHBoxLayout(prog_row); pr_lay.setContentsMargins(0, 0, 0, 0)
        self.lbl_prog = QLabel("Ready.")
        self.lbl_prog.setStyleSheet(f"color: {T['muted']}; font-size: 11px;")
        self.progressbar = QProgressBar(); self.progressbar.setMaximumWidth(200); self.progressbar.hide()
        pr_lay.addWidget(self.lbl_prog); pr_lay.addStretch(1); pr_lay.addWidget(self.progressbar)
        lay.addWidget(prog_row)

        # Tabs
        tabs = QTabWidget()

        # Summary
        t1 = QWidget(); t1l = QVBoxLayout(t1)
        self.txt_summary = QPlainTextEdit(); self.txt_summary.setReadOnly(True)
        self.txt_summary.setFont(QFont("Menlo", 12))
        t1l.addWidget(self.txt_summary); tabs.addTab(t1, "Summary")

        # Monthly PnL
        t2 = QWidget(); t2l = QVBoxLayout(t2)
        self.mtree = QTableWidget(0, 2); self.mtree.setHorizontalHeaderLabels(["Month","PnL"])
        self.mtree.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.mtree.setAlternatingRowColors(True); self.mtree.verticalHeader().setVisible(False)
        t2l.addWidget(self.mtree); tabs.addTab(t2, "Monthly PnL")

        # All Trades
        t3 = QWidget(); t3l = QVBoxLayout(t3)
        tcols = ["Date","Symbol","Entry","Exit","PnL%","PnL$","Reason","Score"]
        self.ttree = QTableWidget(0, len(tcols)); self.ttree.setHorizontalHeaderLabels(tcols)
        self.ttree.setAlternatingRowColors(True); self.ttree.verticalHeader().setVisible(False)
        self.ttree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t3l.addWidget(self.ttree); tabs.addTab(t3, "All Trades")

        # By Symbol
        t4 = QWidget(); t4l = QVBoxLayout(t4)
        scols = ["Symbol","Trades","Wins","Win Rate","PnL"]
        self.stree = QTableWidget(0, len(scols)); self.stree.setHorizontalHeaderLabels(scols)
        self.stree.setAlternatingRowColors(True); self.stree.verticalHeader().setVisible(False)
        t4l.addWidget(self.stree); tabs.addTab(t4, "By Symbol")

        lay.addWidget(tabs)

    def _run(self):
        if self._running:
            return
        if not opend_reachable():
            self.lbl_prog.setText("OpenD not reachable.")
            return
        self._running = True
        self.btn_run.setEnabled(False)
        self.progressbar.setRange(0, 0); self.progressbar.show()
        threading.Thread(target=self._do_run, daemon=True).start()

    def _do_run(self):
        try:
            days   = int(self.cb_days.currentText())
            tf     = self.cb_tf.currentText()
            thresh = float(self.le_thresh.text())
            raw    = self.le_tickers.text().strip()
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()] if raw else []
            from src.backtest import BacktestConfig, run_backtest
            from src.config import settings
            cfg = BacktestConfig(
                days=days, timeframe=tf, threshold=thresh, tickers=tickers,
                account_usd=settings.account_usd, risk_per_trade=settings.risk_per_trade,
                max_position_pct=settings.max_position_pct, max_hold_days=settings.max_hold_days,
            )
            n_tickers = len(tickers) or len(json.loads((ROOT / "config" / "watchlist.json").read_text())["tickers"])
            def prog(cur, total, sym):
                pct = int((cur+1)/max(total,1)*100)
                label = f"Simulating {sym}… ({pct}%)" if total > n_tickers*2 else f"Fetching {sym}… ({cur+1}/{total})"
                self._prog.emit(label)
            result = run_backtest(cfg, progress_cb=prog)
            self._done.emit(result)
        except Exception as e:
            self._error.emit(str(e))
        finally:
            self._running = False
            self.btn_run.setEnabled(True)
            self.progressbar.hide()

    def _show_results(self, result: dict):
        m   = result.get("metrics", {})
        cfg = result.get("config", {})
        trades = result.get("trades", [])
        lines = [
            f"Backtest: {cfg.get('timeframe')}  |  {cfg.get('days')} days  |  threshold={cfg.get('threshold')}",
            f"Generated: {result.get('generated_at','?')}",
            "",
        ]
        if m.get("total_trades", 0) == 0:
            lines.append("No trades generated. Lower the threshold.")
        else:
            lines += [
                f"Total trades      : {m['total_trades']}",
                f"Win rate          : {m['win_rate_pct']}%",
                f"Profit factor     : {m['profit_factor']}",
                f"Net PnL           : ${m['net_pnl_usd']:+,.2f}",
                f"Avg win           : {m['avg_win_pct']:+.2f}%",
                f"Avg loss          : {m['avg_loss_pct']:+.2f}%",
                f"Max drawdown      : ${m['max_drawdown_usd']:,.2f}",
                f"Sharpe ratio      : {m['sharpe_ratio']}",
            ]
        self.txt_summary.setPlainText("\n".join(lines))

        self.mtree.setRowCount(0)
        for month, pnl in m.get("monthly_pnl", {}).items():
            row = self.mtree.rowCount(); self.mtree.insertRow(row)
            self.mtree.setItem(row, 0, QTableWidgetItem(month))
            item = QTableWidgetItem(f"${pnl:+,.2f}")
            item.setForeground(QColor(T["success"] if pnl >= 0 else T["danger"]))
            self.mtree.setItem(row, 1, item)

        self.ttree.setRowCount(0)
        for t in trades:
            row = self.ttree.rowCount(); self.ttree.insertRow(row)
            vals = [t["entry_date"], t["symbol"], f"${t['entry_price']:.2f}",
                    f"${t['exit_price']:.2f}", f"{t['pnl_pct']:+.2f}%",
                    f"${t['pnl']:+.2f}", t["exit_reason"], f"{t['score']:.0f}"]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if col == 5:
                    item.setForeground(QColor(T["success"] if t["pnl"] >= 0 else T["danger"]))
                self.ttree.setItem(row, col, item)

        self.stree.setRowCount(0)
        by_sym = m.get("by_symbol", {})
        for sym, s in sorted(by_sym.items(), key=lambda x: -x[1]["pnl"]):
            row = self.stree.rowCount(); self.stree.insertRow(row)
            wr = round(s["wins"]/s["trades"]*100) if s["trades"] else 0
            vals = [sym, str(s["trades"]), str(s["wins"]), f"{wr}%", f"${s['pnl']:+,.2f}"]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if col == 4:
                    item.setForeground(QColor(T["success"] if s["pnl"] >= 0 else T["danger"]))
                self.stree.setItem(row, col, item)

        self.lbl_prog.setText(
            f"Done. {m.get('total_trades',0)} trades, win rate {m.get('win_rate_pct',0)}%, "
            f"net PnL ${m.get('net_pnl_usd',0):+,.2f}"
        )


# ── Equity Dialog ──────────────────────────────────────────────────────────

class EquityDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Equity Curve")
        self.resize(980, 660)
        self._build()
        self._refresh()

    def _build(self):
        lay = QVBoxLayout(self)

        # Toolbar
        top = QWidget()
        tl = QHBoxLayout(top); tl.setContentsMargins(0,0,0,0); tl.setSpacing(8)
        tl.addWidget(QLabel("Source:"))
        self.cb_src = QComboBox()
        self.cb_src.addItems(["closed_trades","snapshot_history"])
        tl.addWidget(self.cb_src)
        btn = QPushButton("🔄 Refresh"); btn.clicked.connect(self._refresh)
        tl.addWidget(btn)
        self.lbl_stats = QLabel("—")
        self.lbl_stats.setStyleSheet(f"color: {T['text_strong']}; font-family: Menlo; font-weight: bold;")
        tl.addStretch(1); tl.addWidget(self.lbl_stats)
        lay.addWidget(top)

        import matplotlib
        matplotlib.use("QtAgg")
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.fig = Figure(figsize=(8, 5), dpi=100, facecolor=T["bg"])
        self.canvas = FigureCanvasQTAgg(self.fig)
        lay.addWidget(self.canvas)

    def _refresh(self):
        src = self.cb_src.currentText()
        self.fig.clear()
        if src == "closed_trades":
            trades = _load_trades_jsonl()
            if not trades:
                self.lbl_stats.setText("No closed trades yet")
                self.canvas.draw(); return
            trades.sort(key=lambda r: r.get("ts",""))
            equity = [0.0]
            for t in trades:
                equity.append(equity[-1] + t.get("pnl", 0))
            self._plot(equity, [t["ts"][:10] for t in trades])
            wins = sum(1 for t in trades if t["pnl"] > 0)
            wr   = round(wins/len(trades)*100, 1)
            tot  = round(sum(t["pnl"] for t in trades), 2)
            avg_r = round(sum(t.get("r_multiple",0) for t in trades)/len(trades), 2)
            self.lbl_stats.setText(
                f"{len(trades)} trades  |  WR {wr}%  |  avg R {avg_r:+.2f}  |  net ${tot:+,.2f}"
            )
        else:
            if not HISTORY_FILE.exists():
                self.lbl_stats.setText("No snapshot history"); self.canvas.draw(); return
            rows = []
            for line in HISTORY_FILE.read_text().strip().split("\n"):
                if line:
                    try: rows.append(json.loads(line))
                    except: pass
            if not rows:
                self.lbl_stats.setText("No snapshot history"); self.canvas.draw(); return
            equity = [r.get("total_pnl", 0) for r in rows]
            self._plot(equity, [r.get("ts","")[:10] for r in rows])
            self.lbl_stats.setText(f"{len(rows)} snapshots  |  last ${equity[-1]:+,.2f}")
        self.canvas.draw()

    def _plot(self, equity: list, labels: list):
        ax = self.fig.add_subplot(111)
        ax.set_facecolor(T["card_bg"])
        for sp in ("top","right"): ax.spines[sp].set_visible(False)
        ax.spines["left"].set_color(T["border"])
        ax.spines["bottom"].set_color(T["border"])
        ax.plot(equity, linewidth=2, color=T["primary"], solid_capstyle="round")
        ax.fill_between(range(len(equity)), equity, 0,
                        where=[e>=0 for e in equity], color=T["primary"], alpha=0.10)
        peak, dd = 0.0, []
        for e in equity:
            peak = max(peak, e); dd.append(e - peak)
        ax2 = ax.twinx()
        for sp in ax2.spines.values(): sp.set_visible(False)
        ax2.fill_between(range(len(dd)), dd, 0, color=T["danger"], alpha=0.15)
        ax2.set_ylabel("Drawdown ($)", color=T["danger"], fontsize=9)
        ax2.tick_params(axis="y", labelcolor=T["danger"], labelsize=8)
        ax.set_ylabel("Cumulative PnL ($)", fontsize=9, color=T["text"])
        ax.set_xlabel("Trades →", fontsize=9, color=T["muted"])
        ax.grid(True, alpha=0.2, linestyle="--", linewidth=0.7, color=T["border"])
        ax.axhline(0, color=T["muted"], linewidth=0.8, alpha=0.6)
        ax.tick_params(colors=T["muted"], labelsize=8)
        if labels and len(labels) > 8:
            step = max(1, len(labels)//8)
            ax.set_xticks(range(0, len(labels), step))
            ax.set_xticklabels([labels[i] for i in range(0, len(labels), step)], rotation=30, fontsize=8)
        self.fig.tight_layout()


# ── Audit Dialog ───────────────────────────────────────────────────────────

class AuditDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Decisions & Audit Log")
        self.resize(980, 640)
        self._build()
        self._refresh()

    def _build(self):
        lay = QVBoxLayout(self)
        top = QWidget(); tl = QHBoxLayout(top); tl.setContentsMargins(0,0,0,0)
        btn = QPushButton("🔄 Refresh"); btn.clicked.connect(self._refresh); tl.addWidget(btn)
        tl.addWidget(QLabel("Filter:"))
        self.cb_filter = QComboBox(); self.cb_filter.addItems(["all","buy","skip","error"])
        self.cb_filter.currentTextChanged.connect(lambda _: self._refresh())
        tl.addWidget(self.cb_filter); tl.addStretch(1)
        self.lbl_summary = QLabel("—")
        self.lbl_summary.setStyleSheet(f"color: {T['text_strong']}; font-family: Menlo; font-weight: bold;")
        tl.addWidget(self.lbl_summary)
        lay.addWidget(top)

        tabs = QTabWidget()
        # Decisions
        t1 = QWidget(); t1l = QVBoxLayout(t1)
        cols = ["TS","Action","Symbol","Gate","Reason","Score"]
        widths = [130, 65, 70, 90, 380, 60]
        self.tree = QTableWidget(0, len(cols)); self.tree.setHorizontalHeaderLabels(cols)
        for i, w in enumerate(widths): self.tree.setColumnWidth(i, w)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.verticalHeader().setVisible(False)
        t1l.addWidget(self.tree); tabs.addTab(t1, "Decisions")

        # Skip gate stats
        t2 = QWidget(); t2l = QVBoxLayout(t2)
        self.gtree = QTableWidget(0, 3); self.gtree.setHorizontalHeaderLabels(["Gate","Count","Pct"])
        self.gtree.setAlternatingRowColors(True); self.gtree.verticalHeader().setVisible(False)
        t2l.addWidget(self.gtree); tabs.addTab(t2, "Skip Gate Stats")
        lay.addWidget(tabs)

    def _refresh(self):
        rows = _load_audit_jsonl(1000)
        f = self.cb_filter.currentText()
        filtered = rows if f == "all" else [r for r in rows if r.get("action") == f]
        self.tree.setRowCount(0)
        for r in filtered[-300:]:
            row = self.tree.rowCount(); self.tree.insertRow(row)
            vals = [
                r.get("ts","")[:19].replace("T"," "),
                r.get("action",""), r.get("symbol",""), r.get("gate",""),
                r.get("reason","")[:80], str(r.get("score",0)),
            ]
            action = r.get("action","")
            color  = QColor(T["success"] if action=="buy" else T["danger"] if action=="error" else T["muted"])
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if col in (1, 2): item.setForeground(color)
                self.tree.setItem(row, col, item)

        buys  = sum(1 for r in rows if r.get("action")=="buy")
        skips = [r for r in rows if r.get("action")=="skip"]
        errs  = sum(1 for r in rows if r.get("action")=="error")
        self.lbl_summary.setText(f"{len(rows)} events  |  {buys} buys  |  {len(skips)} skips  |  {errs} errors")

        self.gtree.setRowCount(0)
        gate_counts: dict = {}
        for r in skips: gate_counts[r.get("gate","?")] = gate_counts.get(r.get("gate","?"),0)+1
        total = sum(gate_counts.values()) or 1
        for g, c in sorted(gate_counts.items(), key=lambda x: -x[1]):
            row = self.gtree.rowCount(); self.gtree.insertRow(row)
            for col, v in enumerate([g, str(c), f"{c/total*100:.0f}%"]):
                self.gtree.setItem(row, col, QTableWidgetItem(v))


# ── Approvals Dialog ───────────────────────────────────────────────────────
# The owner-facing half of the feedback 铁律: automated analysis (weekly
# self-review + DeepSeek optimizer) enqueues SUGGESTIONS here; nothing touches
# live behavior until the owner Approves. Approved items are applied by the
# scheduler on its next scan (runtime override / blacklist add — no restart).
class ApprovalsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Approvals — 待批准建议")
        self.resize(940, 560)
        self._items: list = []
        self._build()
        self._refresh()
        # Auto-refresh so actions taken in Telegram (shared db queue) show up
        # here live, without clicking Refresh.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(4000)

    def closeEvent(self, e):
        try:
            self._timer.stop()
        except Exception:
            pass
        super().closeEvent(e)

    def _build(self):
        lay = QVBoxLayout(self)
        top = QWidget(); tl = QHBoxLayout(top); tl.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("🔄 Refresh"); btn.clicked.connect(self._refresh); tl.addWidget(btn)
        tl.addStretch(1)
        self.lbl = QLabel("—")
        self.lbl.setStyleSheet(f"color: {T['text_strong']}; font-family: Menlo; font-weight: bold;")
        tl.addWidget(self.lbl)
        lay.addWidget(top)

        cols = ["When", "Kind", "Suggestion", "Status"]
        widths = [140, 130, 470, 90]
        self.tbl = QTableWidget(0, len(cols)); self.tbl.setHorizontalHeaderLabels(cols)
        for i, w in enumerate(widths): self.tbl.setColumnWidth(i, w)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        lay.addWidget(self.tbl)

        actions = QWidget(); al = QHBoxLayout(actions); al.setContentsMargins(0, 0, 0, 0)
        self.btn_app = QPushButton("✅ Approve selected")
        self.btn_app.clicked.connect(lambda: self._resolve(True))
        self.btn_rej = QPushButton("✖ Reject selected")
        self.btn_rej.clicked.connect(lambda: self._resolve(False))
        al.addWidget(self.btn_app); al.addWidget(self.btn_rej); al.addStretch(1)
        note = QLabel("批准后由调度器下次扫描执行（运行时生效，免重启）。")
        note.setStyleSheet(f"color: {T['muted']};")
        al.addWidget(note)
        lay.addWidget(actions)

    def _refresh(self):
        try:
            from src import approvals
            items = approvals.list_all()
        except Exception as e:
            items = []
            self.lbl.setText(f"load failed: {e}")
        # pending first, then most-recent
        items = sorted(items, key=lambda a: (a.get("status") != "pending",
                                             a.get("created_at", "")), reverse=False)
        self._items = items
        self.tbl.setRowCount(0)
        npend = 0
        for a in items:
            r = self.tbl.rowCount(); self.tbl.insertRow(r)
            st = a.get("status", "pending")
            if st == "pending":
                npend += 1
            vals = [a.get("created_at", "")[:19].replace("T", " "),
                    a.get("kind", ""), a.get("detail", "")[:110], st]
            color = QColor(T["text_strong"] if st == "pending"
                           else T["success"] if st in ("approved",) else T["muted"])
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 3:
                    item.setForeground(color)
                self.tbl.setItem(r, c, item)
        self.lbl.setText(f"{npend} pending  |  {len(items)} total")

    def _resolve(self, approved: bool):
        from src import approvals
        rows = sorted({i.row() for i in self.tbl.selectedItems()})
        done = 0
        for r in rows:
            if 0 <= r < len(self._items):
                a = self._items[r]
                if a.get("status") == "pending" and approvals.resolve(a.get("id", ""), approved):
                    done += 1
        self._refresh()
        try:
            self.parent()._toast(
                f"{'✅ approved' if approved else '✖ rejected'} {done} "
                f"{'(applies next scan)' if approved else ''}")
        except Exception:
            pass


# ── Watchlist Dialog ───────────────────────────────────────────────────────

class SectorCanvas(QWidget):
    PADDING = 12; CARD_GAP = 12; CARD_MIN_W = 210; HEADER_H = 42; LINE_H = 20; MAX_COLS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: list = []
        self._held: set = set()
        self._max_per: int = 3

    def set_data(self, sorted_groups, held, max_per):
        self._data = sorted_groups
        self._held = held
        self._max_per = max_per
        self.update()

    def _ratio_colors(self, ratio):
        if ratio == 0:   return "#264326", "#3fb950", "#3fb950"
        if ratio < 0.7:  return "#1b3358", "#58a6ff", "#58a6ff"
        if ratio < 1.0:  return "#4a3000", "#d29922", "#d29922"
        return "#4a1515", "#f85149", "#f85149"

    def paintEvent(self, event):
        if not self._data: return
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self._data)
        cw = self.width()
        usable = cw - 2*self.PADDING + self.CARD_GAP
        cols = max(1, min(self.MAX_COLS, usable//(self.CARD_MIN_W + self.CARD_GAP), n))
        rows_n = (n + cols - 1) // cols
        card_w = (cw - 2*self.PADDING - (cols-1)*self.CARD_GAP) // cols

        row_heights = []
        for r in range(rows_n):
            tallest = 0
            for c in range(cols):
                idx = r*cols+c
                if idx >= n: continue
                tallest = max(tallest, self.HEADER_H + len(self._data[idx][1])*self.LINE_H + 16)
            row_heights.append(tallest)

        total_h = sum(row_heights) + (rows_n-1)*self.CARD_GAP + 2*self.PADDING
        self.setMinimumHeight(max(total_h, 200))

        y0 = self.PADDING
        for r in range(rows_n):
            x0 = self.PADDING
            for c in range(cols):
                idx = r*cols+c
                if idx >= n: break
                sect, syms = self._data[idx]
                in_sec = [s for s in syms if s in self._held]
                ratio = len(in_sec) / max(1, self._max_per)
                hfill, border, bar_color = self._ratio_colors(ratio)
                x1, y1 = x0+card_w, y0+row_heights[r]

                # Card bg
                p.setBrush(QBrush(QColor(T["card_bg"]))); p.setPen(QPen(QColor(T["border"]), 1))
                p.drawRoundedRect(x0, y0, card_w, row_heights[r], 6, 6)
                # Header bg
                p.setBrush(QBrush(QColor(hfill))); p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(x0, y0, card_w, self.HEADER_H, 6, 6)
                p.drawRect(x0, y0+self.HEADER_H-8, card_w, 8)
                # Sector name
                p.setPen(QColor(T["text_strong"])); f = QFont("-apple-system", 11, QFont.Weight.Bold)
                p.setFont(f); p.drawText(x0+10, y0+16, sect)
                # Usage
                p.setPen(QColor(T["muted"])); f2 = QFont("Menlo", 10); p.setFont(f2)
                p.drawText(x0+10, y0+32, f"{len(in_sec)}/{self._max_per}")
                # Progress bar bg
                bx1, bx2 = x0+10, x0+card_w-10
                p.setBrush(QBrush(QColor(T["border"]))); p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(bx1, y0+self.HEADER_H-7, bx2-bx1, 4, 2, 2)
                if ratio > 0:
                    fw = int((bx2-bx1)*min(1.0, ratio))
                    p.setBrush(QBrush(QColor(bar_color)))
                    p.drawRoundedRect(bx1, y0+self.HEADER_H-7, fw, 4, 2, 2)
                # Symbols
                ty = y0 + self.HEADER_H + 10
                for sym in syms:
                    is_held = sym in self._held
                    dot_c = bar_color if is_held else T["border"]
                    p.setBrush(QBrush(QColor(dot_c))); p.setPen(Qt.PenStyle.NoPen)
                    p.drawEllipse(x0+14, ty+2, 8, 8)
                    p.setPen(QColor(T["success"] if is_held else T["muted"]))
                    ff = QFont("Menlo", 10, QFont.Weight.Bold if is_held else QFont.Weight.Normal)
                    p.setFont(ff)
                    p.drawText(x0+28, ty+10, sym)
                    ty += self.LINE_H
                x0 += card_w + self.CARD_GAP
            y0 += row_heights[r] + self.CARD_GAP
        p.end()


class SectorHeatmapDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sector Heat-map")
        self.resize(880, 600)
        self._build()
        QTimer.singleShot(50, self._draw)

    def _build(self):
        lay = QVBoxLayout(self)
        lbl = QLabel("Watchlist by sector — colored dot = held position, bar = sector exposure.")
        lbl.setStyleSheet(f"color: {T['muted']}; font-size: 11px;")
        lay.addWidget(lbl)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        self._canvas = SectorCanvas(); scroll.setWidget(self._canvas)
        lay.addWidget(scroll)

    def _draw(self):
        try:
            from src.sector import SECTOR_MAP, MAX_PER_SECTOR
            if not WATCHLIST_FILE.exists(): return
            wl = json.loads(WATCHLIST_FILE.read_text())["tickers"]
            trades = read_json(OPEN_TRADES); held = set(trades.keys())
            groups: dict = {}
            for sym in wl:
                sect = SECTOR_MAP.get(sym.upper(), "unknown")
                groups.setdefault(sect, []).append(sym)
            sorted_groups = sorted(
                groups.items(),
                key=lambda kv: (-len([s for s in kv[1] if s in held]), -len(kv[1])),
            )
            self._canvas.set_data(sorted_groups, held, MAX_PER_SECTOR)
        except Exception as e:
            pass


# ── ML Dialog ──────────────────────────────────────────────────────────────

class SignalReporterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signal Reporter")
        self.resize(580, 720)
        self._build()
        self._load_watchlist()
        self._refresh_status()
        self._timer = QTimer(self); self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(5000)

    def _build(self):
        lay = QVBoxLayout(self)
        desc = QLabel("Independent signal watchlist — sends Telegram signals every 30 min during peak hours.")
        desc.setWordWrap(True); desc.setStyleSheet(f"color: {T['muted']}; font-size: 10px;")
        lay.addWidget(desc)

        # Scheduler controls
        ctl = QWidget(); cl = QHBoxLayout(ctl); cl.setContentsMargins(8, 8, 8, 8)
        self.lbl_sr_status = QLabel("⚪ Stopped")
        self.lbl_sr_status.setStyleSheet(f"color: {T['muted']}; font-size: 13px; font-weight: bold;")
        cl.addWidget(self.lbl_sr_status); cl.addSpacing(12)
        btn_start = PrimaryButton("▶ Start"); btn_start.clicked.connect(self._start); cl.addWidget(btn_start)
        btn_stop  = DangerButton("■ Stop");  btn_stop.clicked.connect(self._stop);  cl.addWidget(btn_stop)
        cl.addStretch(1)
        btn_pm = QPushButton("📊 Premarket Now"); btn_pm.clicked.connect(self._run_premarket); cl.addWidget(btn_pm)
        btn_id = QPushButton("⚡ Intraday Now");  btn_id.clicked.connect(self._run_intraday);  cl.addWidget(btn_id)
        lay.addWidget(ctl)

        # Watchlist editor
        wl_grp = QWidget()
        wl_lay = QVBoxLayout(wl_grp); wl_lay.setContentsMargins(0,0,0,0)
        wl_top = QWidget(); wtt = QHBoxLayout(wl_top); wtt.setContentsMargins(0,0,0,0)
        wtt.addWidget(QLabel("Symbol:"))
        self.le_new = QLineEdit(); self.le_new.setFixedWidth(100)
        self.le_new.returnPressed.connect(self._add_ticker)
        wtt.addWidget(self.le_new)
        btn_add = QPushButton("➕ Add");   btn_add.clicked.connect(self._add_ticker);  wtt.addWidget(btn_add)
        btn_rem = QPushButton("🗑 Remove"); btn_rem.clicked.connect(self._rem_ticker);  wtt.addWidget(btn_rem)
        wtt.addStretch(1)
        btn_save = PrimaryButton("💾 Save"); btn_save.clicked.connect(self._save_wl); wtt.addWidget(btn_save)
        wl_lay.addWidget(wl_top)
        self.list_w = QListWidget(); self.list_w.setMaximumHeight(160)
        wl_lay.addWidget(self.list_w)
        self.lbl_wl = QLabel(""); self.lbl_wl.setStyleSheet(f"color: {T['muted']}; font-size: 10px;")
        wl_lay.addWidget(self.lbl_wl)
        lay.addWidget(wl_grp)

        # Log
        log_top = QWidget(); lt = QHBoxLayout(log_top); lt.setContentsMargins(0,0,0,0)
        lt.addWidget(QLabel("Log:"))
        lt.addStretch(1)
        btn_open = QPushButton("📂 Open File")
        btn_open.clicked.connect(lambda: subprocess.run(["open", str(SIGNAL_LOG_FILE)]))
        lt.addWidget(btn_open)
        btn_ref = QPushButton("🔄"); btn_ref.clicked.connect(self._tail_log); lt.addWidget(btn_ref)
        lay.addWidget(log_top)
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True)
        lay.addWidget(self.log_view)
        self._tail_log()

    def _read_signal_pid(self):
        try:
            pid = int(SIGNAL_PID_FILE.read_text().strip())
            return pid if is_process_alive(pid) else None
        except Exception: return None

    def _refresh_status(self):
        pid = self._read_signal_pid()
        if pid:
            self.lbl_sr_status.setText(f"🟢 Running (PID {pid})")
            self.lbl_sr_status.setStyleSheet(f"color: {T['success']}; font-size: 13px; font-weight: bold;")
        else:
            self.lbl_sr_status.setText("⚪ Stopped")
            self.lbl_sr_status.setStyleSheet(f"color: {T['muted']}; font-size: 13px; font-weight: bold;")

    def _auto_refresh(self):
        if not self.isVisible(): return
        self._refresh_status(); self._tail_log()

    def _start(self):
        if self._read_signal_pid(): self.lbl_wl.setText("Already running"); return
        SIGNAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([PYTHON, "-m", "src.signal_reporter", "run"],
            cwd=str(ROOT), stdout=open(SIGNAL_LOG_FILE,"a"), stderr=subprocess.STDOUT,
            start_new_session=True)
        SIGNAL_PID_FILE.write_text(str(proc.pid))
        self.lbl_wl.setText(f"Started (PID {proc.pid})")
        self._refresh_status()

    def _stop(self):
        pid = self._read_signal_pid()
        if pid is None: self.lbl_wl.setText("Not running"); return
        try: os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError: pass
        for _ in range(10):
            if not is_process_alive(pid): break
            time.sleep(0.3)
        try: SIGNAL_PID_FILE.unlink()
        except FileNotFoundError: pass
        self.lbl_wl.setText("Stopped"); self._refresh_status()

    def _run_premarket(self):
        self.lbl_wl.setText("Running premarket…")
        threading.Thread(target=self._do_run, args=("premarket",), daemon=True).start()

    def _run_intraday(self):
        self.lbl_wl.setText("Running intraday…")
        threading.Thread(target=self._do_run, args=("intraday",), daemon=True).start()

    def _do_run(self, mode: str):
        try:
            subprocess.run([PYTHON, "-m", "src.signal_reporter", mode], cwd=str(ROOT),
                stdout=open(SIGNAL_LOG_FILE,"a"), stderr=subprocess.STDOUT, timeout=180)
            QTimer.singleShot(0, lambda: self.lbl_wl.setText(f"✓ {mode} done"))
        except Exception as e:
            QTimer.singleShot(0, lambda: self.lbl_wl.setText(f"⚠ {mode}: {e}"))
        QTimer.singleShot(0, self._tail_log)

    def _load_watchlist(self):
        if not SIGNAL_WL_FILE.exists(): return
        try:
            data = json.loads(SIGNAL_WL_FILE.read_text())
            for t in data.get("tickers",[]): self.list_w.addItem(t)
            self.lbl_wl.setText(f"Loaded {self.list_w.count()} tickers")
        except Exception as e: self.lbl_wl.setText(f"Load error: {e}")

    def _add_ticker(self):
        sym = self.le_new.text().strip().upper()
        if not sym: return
        existing = {self.list_w.item(i).text() for i in range(self.list_w.count())}
        if sym in existing: self.lbl_wl.setText(f"{sym} already in list"); return
        self.list_w.addItem(sym); self.le_new.clear()
        self.lbl_wl.setText(f"Added {sym} — click Save to persist")

    def _rem_ticker(self):
        for item in self.list_w.selectedItems():
            self.list_w.takeItem(self.list_w.row(item))
        self.lbl_wl.setText("Removed — click Save to persist")

    def _save_wl(self):
        tickers = [self.list_w.item(i).text() for i in range(self.list_w.count())]
        try:
            SIGNAL_WL_FILE.write_text(json.dumps({"tickers": tickers}, indent=2))
            self.lbl_wl.setText(f"✓ Saved {len(tickers)} tickers")
        except Exception as e: self.lbl_wl.setText(f"Save failed: {e}")

    def _tail_log(self):
        if not SIGNAL_LOG_FILE.exists():
            self.log_view.setPlainText(f"Log not found: {SIGNAL_LOG_FILE}"); return
        try:
            with open(SIGNAL_LOG_FILE, "rb") as f:
                f.seek(0, os.SEEK_END); f.seek(max(0, f.tell()-6000))
                tail = f.read().decode(errors="replace")
        except OSError: return
        self.log_view.setPlainText("\n".join(tail.splitlines()[-100:]))
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def closeEvent(self, event):
        self._timer.stop(); super().closeEvent(event)


# ── API Keys Dialog ────────────────────────────────────────────────────────

class ApiKeysDialog(QDialog):
    """Read / write API keys in .env without touching a text editor."""

    # (env_key, label, is_secret, placeholder, hint)
    _FIELDS = [
        ("GEMINI_API_KEYS",   "Gemini API Keys",    True,  "AIza...",
         "Comma-separated — add multiple keys for failover"),
        ("GEMINI_MODEL",      "Gemini Model",        False, "gemini-2.5-flash",
         "Model ID used for AI analysis"),
        ("TAVILY_API_KEY",    "Tavily API Key",      True,  "tvly-...",
         "Used for news fetching (optional)"),
        ("TELEGRAM_TOKEN",    "Telegram Bot Token",  True,  "123456:ABC...",
         "Bot token from @BotFather"),
        ("TELEGRAM_CHAT_ID",  "Telegram Chat ID",    False, "-100123456789",
         "Your personal or group chat ID"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API Keys")
        self.resize(560, 480)
        self._widgets: dict[str, QLineEdit] = {}
        self._build()
        self._load()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(12)

        hdr = QLabel("🔑  API Keys")
        hdr.setStyleSheet(f"color: {T['text_strong']}; font-size: 14px; font-weight: bold;")
        outer.addWidget(hdr)

        note = QLabel("Changes are written to <b>.env</b>. "
                      "Restart the scheduler for new values to take effect.")
        note.setStyleSheet(f"color: {T['muted']}; font-size: 10px;")
        note.setWordWrap(True)
        outer.addWidget(note)

        grid = QWidget()
        gl = QGridLayout(grid)
        gl.setContentsMargins(0, 4, 0, 4)
        gl.setHorizontalSpacing(10)
        gl.setVerticalSpacing(6)
        gl.setColumnStretch(1, 1)

        for row, (env_key, label, is_secret, placeholder, hint) in enumerate(self._FIELDS):
            lbl = QLabel(label + ":")
            lbl.setStyleSheet(f"color: {T['text']}; font-size: 11px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl.setFixedWidth(150)

            le = QLineEdit()
            le.setPlaceholderText(placeholder)
            le.setToolTip(hint)
            if is_secret:
                le.setEchoMode(QLineEdit.EchoMode.Password)

            field_row = QWidget()
            fr_lay = QHBoxLayout(field_row)
            fr_lay.setContentsMargins(0, 0, 0, 0)
            fr_lay.setSpacing(4)
            fr_lay.addWidget(le)

            if is_secret:
                toggle = QPushButton("👁")
                toggle.setFixedWidth(30)
                toggle.setCheckable(True)
                toggle.setStyleSheet("border: none; background: transparent; font-size: 13px;")
                toggle.setToolTip("Show / hide")
                toggle.toggled.connect(
                    lambda checked, w=le: w.setEchoMode(
                        QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
                    )
                )
                fr_lay.addWidget(toggle)

            hint_lbl = QLabel(hint)
            hint_lbl.setStyleSheet(f"color: {T['muted']}; font-size: 9px;")

            gl.addWidget(lbl,       row * 2,     0)
            gl.addWidget(field_row, row * 2,     1)
            gl.addWidget(hint_lbl,  row * 2 + 1, 1)

            self._widgets[env_key] = le

        outer.addWidget(grid)
        outer.addStretch(1)

        bot = QWidget()
        bl = QHBoxLayout(bot)
        bl.setContentsMargins(0, 0, 0, 0)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color: {T['muted']}; font-size: 10px;")
        bl.addWidget(self.lbl_status, 1)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        bl.addWidget(btn_cancel)

        btn_save = PrimaryButton("💾 Save")
        btn_save.clicked.connect(self._save)
        bl.addWidget(btn_save)

        outer.addWidget(bot)

    def _load(self):
        env_path = ROOT / ".env"
        if not env_path.exists():
            self.lbl_status.setText("⚠ .env not found")
            return
        env: dict[str, str] = {}
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        for env_key, le in self._widgets.items():
            le.setText(env.get(env_key, ""))

    def _save(self):
        env_path = ROOT / ".env"
        if not env_path.exists():
            env_path.write_text("")
        lines = env_path.read_text().splitlines()

        for env_key, le in self._widgets.items():
            val = le.text().strip()
            found = False
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if stripped.startswith(env_key + "=") or stripped.startswith("#" + env_key + "="):
                    lines[i] = f"{env_key}={val}"
                    found = True
                    break
            if not found:
                lines.append(f"{env_key}={val}")

        env_path.write_text("\n".join(lines) + "\n")
        self.lbl_status.setText("✓ Saved — restart scheduler to apply")
        self.lbl_status.setStyleSheet(f"color: {T['success']}; font-size: 10px;")


# ── Help Dialog ────────────────────────────────────────────────────────────

class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Help · Glossary")
        self.resize(1000, 700)
        self._build()
        self._populate_tree()

    def _build(self):
        lay = QVBoxLayout(self)

        hdr = QWidget(); hl = QHBoxLayout(hdr); hl.setContentsMargins(0,0,0,0)
        title = QLabel("📚 词条与功能说明")
        title.setStyleSheet(f"color: {T['text_strong']}; font-size: 15px; font-weight: bold;")
        sub = QLabel("点左边目录查看每个功能 / 指标 / 算法的详细解释")
        sub.setStyleSheet(f"color: {T['muted']}; font-size: 10px;")
        hl.addWidget(title); hl.addSpacing(12); hl.addWidget(sub); hl.addStretch(1)
        lay.addWidget(hdr, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True); self.tree.setFixedWidth(280)
        self.tree.itemSelectionChanged.connect(self._on_select)
        splitter.addWidget(self.tree)

        self.txt = QTextEdit(); self.txt.setReadOnly(True)
        splitter.addWidget(self.txt)
        splitter.setStretchFactor(1, 3)
        lay.addWidget(splitter, 1)

    def _populate_tree(self):
        try:
            from src.glossary import GLOSSARY
        except Exception:
            return
        first = None
        for i, (cat, entries) in enumerate(GLOSSARY.items()):
            label = f"{cat}  ({len(entries)})"
            cat_item = QTreeWidgetItem(self.tree, [label])
            cat_item.setExpanded(i == 0)
            for term in entries:
                item = QTreeWidgetItem(cat_item, [term])
                item.setData(0, Qt.ItemDataRole.UserRole, (cat, term))
                if first is None: first = item
        if first:
            self.tree.setCurrentItem(first)
            self._on_select()

    def _on_select(self):
        try:
            from src.glossary import get_entry
        except Exception: return
        items = self.tree.selectedItems()
        if not items: return
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not data: return
        cat, term = data
        entry = get_entry(cat, term)
        if not entry: return
        self._render(term, entry)

    def _render(self, term: str, entry: dict):
        c = T
        html = f'<h2 style="color:{c["text_strong"]}; margin-bottom:2px; margin-top:4px;">{term}</h2>'

        name = entry.get("name", "")
        if name and name != term:
            html += f'<p style="color:{c["muted"]}; font-style:italic; margin-top:0; margin-bottom:4px;">{name}</p>'

        summary = entry.get("summary", "")
        if summary:
            html += f'<p style="color:{c["primary"]}; font-weight:bold; margin-top:2px; margin-bottom:8px;">{summary}</p>'

        def section(title, content):
            if not content: return ""
            lines_html = ""
            for line in content.split("\n"):
                if not line.strip():
                    continue
                if line.startswith("  ") or line.lstrip().startswith(("•","→","=","▶")):
                    lines_html += f'<code style="background:{c["sidebar_bg"]}; color:{c["text_strong"]}; display:block; padding:2px 6px; border-radius:3px; margin:1px 0; font-family:Menlo,monospace; font-size:11px;">{line}</code>'
                else:
                    lines_html += f'<p style="color:{c["text"]}; margin:2px 0;">{line}</p>'
            return f'<p style="color:{c["primary"]}; font-weight:bold; margin-top:10px; margin-bottom:3px;">{title}</p>{lines_html}'

        html += section("说明", entry.get("explain", ""))
        html += section("在系统里用在哪", entry.get("where", ""))
        if entry.get("value"):
            html += f'<p style="color:{c["primary"]}; font-weight:bold; margin-top:10px; margin-bottom:3px;">当前设置</p>'
            html += f'<p style="color:{c["success"]}; font-family:Menlo,monospace; font-weight:bold; margin-top:0;">{entry["value"]}</p>'
        self.txt.setHtml(html)
        self.txt.moveCursor(QTextCursor.MoveOperation.Start)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("MooMoo Trader")
    prefs = load_prefs()
    theme_pref = prefs.get("theme", "auto")
    if theme_pref == "dark" or (theme_pref == "auto" and _detect_macos_dark()):
        T.update(DARK)
    else:
        T.update(LIGHT)
    app.setStyleSheet(_build_qss(T))
    window = App()
    window.show()
    sys.exit(app.exec())
