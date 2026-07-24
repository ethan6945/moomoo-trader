import SwiftUI

/// The web dashboard's top stat cards, natively: a coloured accent rail, label,
/// big figure, an optional sub line, and a mini visual at the bottom (progress
/// bar / bar sparkline / equity line). Fixed height so a row lines up.
struct MetricCard<Visual: View>: View {
    let label: String
    let value: String
    var valueTint: Color = Theme.strong
    var accent: Color = Theme.blue
    var sub: String?
    @ViewBuilder var visual: Visual

    var body: some View {
        HStack(spacing: 0) {
            accent.frame(width: 3)                     // web's coloured card accent
            VStack(alignment: .leading, spacing: 5) {
                Text(label)
                    .font(Theme.Font_.label)
                    .foregroundStyle(Theme.muted)
                    .lineLimit(1)
                Text(value)
                    .font(Theme.Font_.figure)
                    .monospacedDigit()
                    .foregroundStyle(valueTint)
                    .lineLimit(1)
                    .minimumScaleFactor(0.55)
                if let sub {
                    Text(sub).font(.system(size: 10)).foregroundStyle(Theme.muted).lineLimit(1)
                }
                Spacer(minLength: 2)
                visual.frame(height: 26)
            }
            .padding(.horizontal, Theme.Space.md)
            .padding(.vertical, Theme.Space.md)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, minHeight: 104, alignment: .leading)
        // Clip before the surface so the square-cornered accent rail doesn't
        // poke past the card's rounded corners.
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.card, style: .continuous))
        .cardSurface()
    }
}

/// Deployed-budget progress bar (web's qbar/qfill).
struct ProgressMeter: View {
    var fraction: Double            // 0…1+
    var tint: Color = Theme.red     // web fills the budget bar red as it fills up

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.card2)
                Capsule().fill(tint)
                    .frame(width: geo.size.width * min(1, max(0, fraction)))
            }
        }
        .frame(height: 5)
        .frame(maxHeight: .infinity, alignment: .bottom)
    }
}

/// Mini bar sparkline — daily P&L / per-position unrealized. Right-aligned,
/// zero line in the middle, green up / red down (web's sparkBars).
struct BarSparkline: View {
    let values: [Double]

    var body: some View {
        Canvas { ctx, size in
            guard !values.isEmpty else { return }
            let maxAbs = max(values.map { abs($0) }.max() ?? 1, 1)
            let zero = size.height * 0.55
            // baseline
            ctx.fill(Path(CGRect(x: 0, y: zero, width: size.width, height: 0.7)),
                     with: .color(Theme.muted.opacity(0.3)))
            let n = values.count
            let gap: CGFloat = 2
            let bw = max(1.5, min(9, size.width / CGFloat(n) - gap))
            for (i, v) in values.enumerated() {
                let x = size.width - CGFloat(n - i) * (bw + gap)
                guard x >= 0 else { continue }
                let up = v >= 0
                let h = max(1.5, CGFloat(abs(v)) / CGFloat(maxAbs) * (up ? zero - 2 : size.height - zero - 2))
                let rect = CGRect(x: x, y: up ? zero - h : zero, width: bw, height: h)
                ctx.fill(Path(roundedRect: rect, cornerRadius: 1),
                         with: .color(up ? Theme.green : Theme.red))
            }
        }
    }
}

/// Cumulative equity line with a soft brand-gradient fill (web's sparkLine /
/// eqDash, shrunk into the card).
struct LineSparkline: View {
    let values: [Double]

    var body: some View {
        Canvas { ctx, size in
            guard values.count > 1 else { return }
            let lo = values.min() ?? 0, hi = values.max() ?? 1
            let span = max(hi - lo, 1e-6)
            func pt(_ i: Int) -> CGPoint {
                let x = size.width * CGFloat(i) / CGFloat(values.count - 1)
                let y = size.height - (CGFloat(values[i] - lo) / CGFloat(span)) * (size.height - 3) - 1.5
                return CGPoint(x: x, y: y)
            }
            var line = Path()
            line.move(to: pt(0))
            for i in 1..<values.count { line.addLine(to: pt(i)) }
            // area fill under the line
            var area = line
            area.addLine(to: CGPoint(x: size.width, y: size.height))
            area.addLine(to: CGPoint(x: 0, y: size.height))
            area.closeSubpath()
            ctx.fill(area, with: .linearGradient(
                Gradient(colors: [Theme.purple.opacity(0.30), Theme.blue.opacity(0.02)]),
                startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))
            ctx.stroke(line, with: .linearGradient(
                Gradient(colors: [Theme.blue, Theme.purple]),
                startPoint: .zero, endPoint: CGPoint(x: size.width, y: 0)),
                style: .init(lineWidth: 1.6, lineJoin: .round))
        }
    }
}
