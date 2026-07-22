import Combine
import Foundation

/// App-wide language (zh / en). Observable so every view that reads a string
/// through `L(_:_:)` re-renders the instant the language flips. Persisted in
/// UserDefaults; the backend and the web dashboard keep their own language.
@MainActor
final class L10n: ObservableObject {
    static let shared = L10n()

    static let key = "lang"

    @Published var lang: String {
        didSet { UserDefaults.standard.set(lang, forKey: Self.key) }
    }

    private init() {
        lang = UserDefaults.standard.string(forKey: Self.key) ?? "zh"
    }

    /// Pick the string for the current language.
    func s(_ zh: String, _ en: String) -> String { lang == "en" ? en : zh }

    /// Backend-sourced OpenD status label — Chinese from web/server.py. In
    /// English mode, translate it segment by segment (" · "-joined); unknown
    /// pieces pass through so a wording change never blanks the pill.
    func opend(_ label: String) -> String {
        guard lang == "en" else { return label }
        let seg = [
            "OpenD 未启动": "OpenD not running",
            "模拟盘": "PAPER", "已解锁": "Unlocked", "已连接": "Connected",
            "可交易": "Ready", "连接中…": "Connecting…", "调度器已停": "Scheduler stopped",
            "等待首次账户查询": "Waiting for first account query",
            "解锁未确认 — 请在 OpenD 窗口点击解锁": "Unlock unconfirmed — click unlock in OpenD",
            "解锁未确认(开盘前请解锁)": "Unlock unconfirmed (unlock before open)",
            "待开盘": "Pre-open", "已收盘": "Closed", "周末休市": "Weekend",
            "假期休市": "Holiday", "休市": "Closed",
        ]
        return label.components(separatedBy: " · ")
            .map { seg[$0] ?? $0 }.joined(separator: " · ")
    }

    /// English descriptions for the fixed .env keys, so the settings list reads
    /// English too (the backend ships Chinese only). Falls back to the backend
    /// text for any key not listed here.
    func keyDesc(_ key: String, fallback: String) -> String {
        guard lang == "en" else { return fallback }
        return [
            "WEB_PASSWORD": "Web access password — phone/LAN (and this Mac) must log in. Empty = no password (local only). Required before exposing to phones.",
            "DEEPSEEK_API_KEY": "DeepSeek API key (comma-separate several) — used for all AI analysis (signals/sentiment/exit/optimize/entry check).",
            "TAVILY_API_KEY": "Tavily news-search key — gives the AI real-time news context.",
            "TELEGRAM_TOKEN": "Telegram bot token — pushes trade notifications + approval cards.",
            "TELEGRAM_CHAT_ID": "Telegram chat ID — the chat that receives notifications.",
        ][key] ?? fallback
    }
}

/// Shorthand used across views. The enclosing view must observe `L10n.shared`
/// (via `@ObservedObject`) so a language change re-renders it.
@MainActor
func L(_ zh: String, _ en: String) -> String { L10n.shared.s(zh, en) }
