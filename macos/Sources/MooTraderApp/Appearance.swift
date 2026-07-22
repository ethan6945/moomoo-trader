import AppKit

/// App-wide light/dark override. "system" follows macOS; "dark"/"light" pin it.
/// Persisted in UserDefaults and applied to NSApp so both native views (the
/// themed colours re-resolve) and the WKWebView follow it.
enum Appearance {
    static let key = "appearance"

    static var current: String {
        UserDefaults.standard.string(forKey: key) ?? "system"
    }

    static func apply(_ mode: String) {
        UserDefaults.standard.set(mode, forKey: key)
        NSApp.appearance = switch mode {
            case "dark":  NSAppearance(named: .darkAqua)
            case "light": NSAppearance(named: .aqua)
            default:      nil          // follow the system setting
        }
    }

    /// Restore the saved choice on launch.
    static func applySaved() { apply(current) }
}
