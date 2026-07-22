import AppKit
import SwiftUI

/// One place for colour, spacing, radius and type — so every view lines up.
///
/// The palette is the web dashboard's (`:root` in web/static/index.html), so the
/// native app and the browser panel read as the same product. Brand and semantic
/// hues are fixed; surfaces adapt, so the app is at home in a light menu bar too.
enum Theme {

    // ── brand & semantics (identical in both appearances) ──
    static let blue   = Color(hex: 0x58A6FF)
    static let blueUp = Color(hex: 0x1F6FEB)
    static let purple = Color(hex: 0xA371F7)
    static let green  = Color(hex: 0x3FB950)
    static let red    = Color(hex: 0xF85149)
    static let amber  = Color(hex: 0xD29922)

    /// The icon's blue→purple sweep — used sparingly: accent bars and charts.
    static var brand: LinearGradient {
        LinearGradient(colors: [blue, purple], startPoint: .topLeading, endPoint: .bottomTrailing)
    }

    // ── adaptive surfaces ──
    static let bg      = adaptive(dark: 0x070A12, light: 0xF4F6FB)
    static let card    = adaptive(dark: 0x111726, light: 0xFFFFFF)
    static let card2   = adaptive(dark: 0x1A2234, light: 0xEDF1F8)
    static let border  = adaptive(dark: 0x232C40, light: 0xDCE3ED)
    static let text    = adaptive(dark: 0xCDD6E4, light: 0x1C2430)
    static let strong  = adaptive(dark: 0xF0F6FC, light: 0x0B1220)
    static let muted   = adaptive(dark: 0x8B96A8, light: 0x6B7686)

    // ── spacing scale (4-based) ──
    enum Space {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 12
        static let lg: CGFloat = 16
        static let xl: CGFloat = 20
    }

    /// Height every inline control shares — pills and capsule buttons alike, so
    /// a status row reads as one band instead of three sizes.
    static let control: CGFloat = 24

    // ── corner radii ──
    enum Radius {
        static let card: CGFloat = 14
        static let row: CGFloat = 10
        static let chip: CGFloat = 6
    }

    // ── type: figures are always monospaced-digit so columns don't dance ──
    enum Font_ {
        static let figure = Font.system(size: 21, weight: .semibold, design: .rounded)
        static let figureSm = Font.system(size: 15, weight: .semibold, design: .rounded)
        static let label = Font.system(size: 11, weight: .medium)
        static let body = Font.system(size: 12)
        static let mono = Font.system(size: 11, design: .monospaced)
    }

    // ── helpers ──
    private static func adaptive(dark: UInt32, light: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor(hex: dark) : NSColor(hex: light)
        })
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

extension NSColor {
    convenience init(hex: UInt32) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }
}

// ── shared surface treatments ────────────────────────────────────────
extension View {
    /// Card surface: fill + hairline border + one consistent radius.
    func cardSurface(radius: CGFloat = Theme.Radius.card,
                     fill: Color = Theme.card) -> some View {
        background(fill, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous)
                .strokeBorder(Theme.border, lineWidth: 1))
    }

    /// Inner row surface — one step quieter than a card.
    func rowSurface(radius: CGFloat = Theme.Radius.row) -> some View {
        background(Theme.card2, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
    }

    /// The page background behind every detail view.
    func pageBackground() -> some View {
        background(Theme.bg)
    }
}

/// Prominent action button in the brand gradient (scheduler start/stop etc).
struct BrandButtonStyle: ButtonStyle {
    var tint: LinearGradient = Theme.brand

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 14)
            .frame(height: Theme.control)
            .background(tint, in: Capsule())
            .opacity(configuration.isPressed ? 0.75 : 1)
            .contentShape(Capsule())
    }
}

/// Quieter companion to BrandButtonStyle — same metrics, outlined.
struct QuietButtonStyle: ButtonStyle {
    var tint: Color = Theme.muted

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(tint)
            .padding(.horizontal, 12)
            .frame(height: Theme.control)
            .background(tint.opacity(configuration.isPressed ? 0.22 : 0.12), in: Capsule())
            .overlay(Capsule().strokeBorder(tint.opacity(0.35)))
            .contentShape(Capsule())
    }
}
