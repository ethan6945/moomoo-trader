import SwiftUI

/// Open positions — the web dashboard's 持仓 table, natively.
///
/// Custom rows rather than `Table`: the range bar and strategy badges need real
/// layout, and one shared column spec keeps the header, cells and every other
/// panel on the same grid.
struct PositionsView: View {
    let positions: [Position]
    @State private var tableWidth: CGFloat = 0

    /// Column weights — justified to the panel width once measured (fixed px as
    /// the pre-measure fallback). 区间 carries the extra slack.
    private let weights: [CGFloat] = [70, 52, 74, 74, 74, 74, 110, 150, 140]
    private var cols: [CGFloat] { columnWidths(weights, total: tableWidth) }

    var body: some View {
        if positions.isEmpty {
            EmptyNote(text: "🌙 当前无持仓", compact: true)
        } else {
            VStack(alignment: .leading, spacing: 2) {
                header
                ForEach(positions) { row($0) }
            }
            .measureWidth { tableWidth = $0 }
        }
    }

    private var header: some View {
        HStack(spacing: Theme.Space.sm) {
            // Header alignment must match each data cell's, or the labels drift
            // off their columns once the widths stretch.
            ColumnHeader(text: "代码", alignment: .leading).frame(width: cols[0])
            ColumnHeader(text: "数量", alignment: .trailing).frame(width: cols[1])
            ColumnHeader(text: "入场", alignment: .trailing).frame(width: cols[2])
            ColumnHeader(text: "现价", alignment: .trailing).frame(width: cols[3])
            ColumnHeader(text: "止损", alignment: .trailing).frame(width: cols[4])
            ColumnHeader(text: "止盈", alignment: .trailing).frame(width: cols[5])
            ColumnHeader(text: "区间", alignment: .center).frame(width: cols[6])
            ColumnHeader(text: "策略", alignment: .leading).frame(width: cols[7])
            ColumnHeader(text: "盈亏", alignment: .trailing).frame(width: cols[8])
        }
        .padding(.horizontal, Theme.Space.md)
        .padding(.bottom, Theme.Space.xs)
    }

    private func row(_ p: Position) -> some View {
        HStack(spacing: Theme.Space.sm) {
            Text(p.symbol)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.strong)
                .frame(width: cols[0], alignment: .leading)
            NumCell(text: Fmt.qty(p.qty)).frame(width: cols[1])
            NumCell(text: Fmt.money(p.entryPrice)).frame(width: cols[2])
            NumCell(text: Fmt.money(p.last), tint: Theme.strong).frame(width: cols[3])
            NumCell(text: Fmt.money(p.stopLoss), tint: Theme.red).frame(width: cols[4])
            NumCell(text: Fmt.money(p.takeProfit), tint: Theme.green).frame(width: cols[5])
            RangeBar(position: p).frame(width: cols[6])
            StrategyTags(position: p).frame(width: cols[7], alignment: .leading)
            NumCell(text: pnlText(p), tint: pnlColor(p.plValue)).frame(width: cols[8])
        }
        .padding(.horizontal, Theme.Space.md)
        .padding(.vertical, Theme.Space.sm)
        .rowSurface()
    }

    private func pnlText(_ p: Position) -> String {
        guard let v = p.plValue, p.last != nil else { return "—" }
        return "\(Fmt.signed(v)) (\(Fmt.pct(p.plRatio)))"
    }
}

/// Strategy tag + chart pattern + manual-adoption badge, matching the web UI's
/// semantics: 手动·自管 = your own buy, high risk, you're watching it;
/// 手动·接管 = the bot has taken over stops/targets.
struct StrategyTags: View {
    let position: Position

    var body: some View {
        HStack(spacing: Theme.Space.xs) {
            if position.manualAdopted {
                Pill(text: position.userManaged ? "手动·自管" : "手动·接管",
                     tint: position.userManaged ? Theme.amber : Theme.blue, icon: "🖐")
                    .help(position.userManaged
                          ? "你手动买入 · 高风险 · 等你确认是否接管（你自己盯盘）"
                          : "你手动买入 · 已被机器人接管止损/止盈")
            } else {
                Pill(text: position.strategy ?? "—", tint: Theme.blue)
            }
            if let pattern = position.pattern {
                Pill(text: pattern, tint: Theme.purple, icon: "📐").help("AI 形态识别")
            }
        }
    }
}
