import SwiftUI

// ── formatting ────────────────────────────────────────────────────────
enum Fmt {
    static func money(_ v: Double?, decimals: Int = 2) -> String {
        guard let v else { return "—" }
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.minimumFractionDigits = decimals
        f.maximumFractionDigits = decimals
        let s = f.string(from: NSNumber(value: abs(v))) ?? "0"
        return (v < 0 ? "-$" : "$") + s
    }

    static func signed(_ v: Double?, decimals: Int = 2) -> String {
        guard let v else { return "—" }
        return (v >= 0 ? "+" : "") + money(v, decimals: decimals)
    }

    static func pct(_ v: Double?, decimals: Int = 1) -> String {
        guard let v else { return "—" }
        return String(format: "%@%.\(decimals)f%%", v >= 0 ? "+" : "", v)
    }

    static func qty(_ v: Double?) -> String {
        guard let v else { return "—" }
        return v == v.rounded() ? String(Int(v)) : String(format: "%.2f", v)
    }

    /// "2026-07-20T20:15:01.936673" / ISO with zone → "07-20 20:15"
    static func stamp(_ s: String?) -> String {
        guard let s, s.count >= 16 else { return "—" }
        return "\(s.prefix(10).dropFirst(5)) \(s.dropFirst(11).prefix(5))"
    }
}

/// Green for gains, red for losses, muted for flat/unknown.
func pnlColor(_ v: Double?) -> Color {
    guard let v, v != 0 else { return Theme.muted }
    return v > 0 ? Theme.green : Theme.red
}

// ── building blocks ───────────────────────────────────────────────────
/// Headline figure with caption — the dashboard's top row. Fixed height so a
/// row of them lines up regardless of whether footnotes are present.
struct StatCard: View {
    let label: String
    let value: String
    var tint: Color = Theme.strong
    var footnote: String?
    /// Draws the brand sweep as a thin left rail — used on the hero card.
    var accent: Bool = false

    var body: some View {
        HStack(spacing: 0) {
            if accent {
                Theme.brand.frame(width: 3)
            }
            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                Text(label)
                    .font(Theme.Font_.label)
                    .foregroundStyle(Theme.muted)
                Text(value)
                    .font(Theme.Font_.figure)
                    .monospacedDigit()
                    .foregroundStyle(tint)
                    .lineLimit(1)
                    .minimumScaleFactor(0.55)
                Text(footnote ?? " ")
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.muted.opacity(footnote == nil ? 0 : 1))
                    .lineLimit(1)
            }
            .padding(.horizontal, Theme.Space.md)
            .padding(.vertical, Theme.Space.md)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, minHeight: 84, alignment: .leading)
        // Clip BEFORE the surface: the accent rail is a square-cornered
        // rectangle in the content layer, so without this it pokes out past the
        // card's rounded corners. Clipping first leaves the border stroke whole.
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        .cardSurface()
    }
}

/// Small capsule label — status, tags, badges.
struct Pill: View {
    let text: String
    var tint: Color = Theme.muted
    var icon: String?

    var body: some View {
        HStack(spacing: 4) {
            if let icon { Text(icon).font(.system(size: 10)) }
            Text(text).font(Theme.Font_.label)
        }
        .padding(.horizontal, 9)
        .frame(height: Theme.control)
        .background(tint.opacity(0.16), in: Capsule())
        .overlay(Capsule().strokeBorder(tint.opacity(0.34)))
        .foregroundStyle(tint == Theme.muted ? Theme.text : tint)
        .fixedSize()
    }
}

/// Titled container. Every panel shares padding, radius and title treatment.
struct Panel<Content: View>: View {
    let title: String
    var subtitle: String?
    var trailing: AnyView?
    /// Grow to the tallest sibling in a row. Must be set on the panel itself —
    /// wrapping it in a `.frame(maxHeight:)` only centres it in a taller box.
    var fillHeight = false
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Space.sm) {
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.strong)
                if let subtitle {
                    Text(subtitle).font(Theme.Font_.label).foregroundStyle(Theme.muted)
                }
                Spacer(minLength: Theme.Space.sm)
                if let trailing { trailing }
            }
            content
        }
        .padding(Theme.Space.lg)
        // .topLeading, not .leading: when a panel is stretched to match a taller
        // neighbour, the extra height must fall out of the bottom — a centring
        // frame would push the title away from the top edge.
        .frame(maxWidth: .infinity,
               maxHeight: fillHeight ? .infinity : nil,
               alignment: .topLeading)
        .cardSurface()
    }
}

/// Column header for the custom data rows — muted, small, aligned with cells.
struct ColumnHeader: View {
    let text: String
    var alignment: Alignment = .leading

    var body: some View {
        Text(text)
            .font(Theme.Font_.label)
            .foregroundStyle(Theme.muted)
            .frame(maxWidth: .infinity, alignment: alignment)
    }
}

/// A numeric cell: monospaced digits, trailing-aligned, optional tint.
struct NumCell: View {
    let text: String
    var tint: Color = Theme.text
    var alignment: Alignment = .trailing

    var body: some View {
        Text(text)
            .font(Theme.Font_.body)
            .monospacedDigit()
            .foregroundStyle(tint)
            .frame(maxWidth: .infinity, alignment: alignment)
    }
}

/// stop-loss → take-profit position bar (web UI's 区间 column).
struct RangeBar: View {
    let position: Position

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(LinearGradient(colors: [Theme.red.opacity(0.5), Theme.card2,
                                                  Theme.green.opacity(0.5)],
                                         startPoint: .leading, endPoint: .trailing))
                    .frame(height: 4)
                if let entry = position.entryFraction {
                    Rectangle()
                        .fill(Theme.muted)
                        .frame(width: 1.5, height: 10)
                        .offset(x: geo.size.width * entry)
                }
                if let now = position.rangeFraction {
                    Circle()
                        .fill(pnlColor(position.plValue))
                        .overlay(Circle().strokeBorder(Theme.card, lineWidth: 1.5))
                        .frame(width: 9, height: 9)
                        .offset(x: max(0, geo.size.width * now - 4.5))
                }
            }
            .frame(height: 10)
            .frame(maxHeight: .infinity)
        }
        .frame(minWidth: 56, minHeight: 14)
        .help(position.rangeFraction.map {
            "SL \(Fmt.money(position.stopLoss)) → TP \(Fmt.money(position.takeProfit)) · \(Int($0 * 100))%"
        } ?? "—")
    }
}

/// Empty-state line used by every panel so they all read the same.
struct EmptyNote: View {
    let text: String
    /// Tight vertical padding — for panels that shouldn't balloon into a big
    /// empty block just because they have nothing to show (e.g. 0 持仓).
    var compact = false

    var body: some View {
        Text(text)
            .font(Theme.Font_.body)
            .foregroundStyle(Theme.muted)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, compact ? Theme.Space.sm : Theme.Space.xl)
    }
}

// ── column-width distribution ─────────────────────────────────────────
/// Reports a view's measured width up the tree.
struct WidthKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

extension View {
    func measureWidth(_ onChange: @escaping (CGFloat) -> Void) -> some View {
        background(GeometryReader { g in
            Color.clear.preference(key: WidthKey.self, value: g.size.width)
        })
        .onPreferenceChange(WidthKey.self, perform: onChange)
    }
}

/// Spread a container's width across columns by weight, so fixed-looking tables
/// justify to the full panel width instead of leaving a big empty right side.
/// `weights` double as the fallback (used until the width is measured).
func columnWidths(_ weights: [CGFloat], total: CGFloat,
                  spacing: CGFloat = Theme.Space.sm,
                  hPadding: CGFloat = Theme.Space.md) -> [CGFloat] {
    let gaps = CGFloat(max(0, weights.count - 1)) * spacing
    let usable = total - gaps - hPadding * 2
    let sum = weights.reduce(0, +)
    guard total > 0, usable > 0, sum > 0 else { return weights }
    return weights.map { usable * $0 / sum }
}

extension String {
    /// "green"/"red"/... from /api/status → a themed colour.
    var statusColor: Color {
        switch self {
        case "green": return Theme.green
        case "blue": return Theme.blue
        case "yellow": return Theme.amber
        case "red": return Theme.red
        default: return Theme.muted
        }
    }
}
