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
        return (v >= 0 ? "+" : "") + money(v, decimals: decimals).replacingOccurrences(of: "-", with: "-")
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
        let date = s.prefix(10).dropFirst(5)      // MM-dd
        let time = s.dropFirst(11).prefix(5)      // HH:mm
        return "\(date) \(time)"
    }
}

/// Green for gains, red for losses, secondary for nothing.
func pnlColor(_ v: Double?) -> Color {
    guard let v, v != 0 else { return .secondary }
    return v > 0 ? .green : .red
}

// ── building blocks ───────────────────────────────────────────────────
/// Headline number with a caption — the dashboard's top row.
struct StatCard: View {
    let label: String
    let value: String
    var tint: Color = .primary
    var footnote: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 22, weight: .semibold, design: .rounded))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            if let footnote {
                Text(footnote)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
    }
}

/// Small rounded label used for regime / OpenD / env badges.
struct Pill: View {
    let text: String
    var tint: Color = .secondary
    var icon: String?

    var body: some View {
        HStack(spacing: 4) {
            if let icon { Text(icon) }
            Text(text)
        }
        .font(.caption)
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(tint.opacity(0.15), in: Capsule())
        .overlay(Capsule().strokeBorder(tint.opacity(0.35)))
        .foregroundStyle(tint == .secondary ? .secondary : tint)
    }
}

/// Titled container matching the web dashboard's panels.
struct Panel<Content: View>: View {
    let title: String
    var trailing: AnyView?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                if let trailing { trailing }
            }
            content
        }
        .padding(14)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 12))
    }
}

/// stop-loss → take-profit position bar (web UI's 区间 column).
struct RangeBar: View {
    let position: Position

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary).frame(height: 5)
                if let entry = position.entryFraction {
                    Rectangle()
                        .fill(.secondary)
                        .frame(width: 1.5, height: 11)
                        .offset(x: geo.size.width * entry)
                }
                if let now = position.rangeFraction {
                    Circle()
                        .fill(pnlColor(position.plValue))
                        .frame(width: 8, height: 8)
                        .offset(x: max(0, geo.size.width * now - 4))
                }
            }
            .frame(height: 11)
            .frame(maxHeight: .infinity)
        }
        .frame(minWidth: 60)
        .help(position.rangeFraction.map {
            "SL \(Fmt.money(position.stopLoss)) → TP \(Fmt.money(position.takeProfit)) · \(Int($0 * 100))%"
        } ?? "—")
    }
}

extension String {
    /// "green"/"red"/... from /api/status → a SwiftUI colour.
    var statusColor: Color {
        switch self {
        case "green": return .green
        case "blue": return .blue
        case "yellow": return .orange
        case "red": return .red
        default: return .secondary
        }
    }
}
