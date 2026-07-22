import Charts
import SwiftUI

/// Closed trades: cumulative equity curve + daily P&L + the trade table.
struct HistoryView: View {
    @State private var trades: [ClosedTrade] = []
    @State private var loading = true

    /// Same column spec idea as PositionsView so the two tables share a grid.
    private let cols: [CGFloat] = [92, 66, 50, 74, 74, 92, 92, 56, 140]

    /// Running total after each closed trade.
    private var equityCurve: [(index: Int, value: Double)] {
        var total = 0.0
        return trades.enumerated().map { i, t in
            total += t.pnl ?? 0
            return (i, total)
        }
    }

    private var dailyPnl: [(day: String, pnl: Double)] {
        var byDay: [String: Double] = [:]
        for t in trades where !t.day.isEmpty { byDay[t.day, default: 0] += t.pnl ?? 0 }
        return byDay.keys.sorted().suffix(30).map { ($0, byDay[$0] ?? 0) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                if loading {
                    Panel(title: "历史") {
                        ProgressView()
                            .tint(Theme.blue)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 40)
                    }
                } else if trades.isEmpty {
                    Panel(title: "历史") { EmptyNote(text: "暂无已平仓交易") }
                } else {
                    HStack(alignment: .top, spacing: Theme.Space.md) {
                        Panel(title: "净值曲线", subtitle: "累计已实现") { equityChart }
                        Panel(title: "每日盈亏", subtitle: "最近 30 个交易日") { dailyChart }
                            .frame(width: 380)
                    }
                    Panel(title: "已平仓", subtitle: "\(trades.count) 笔") { table }
                }
            }
            .padding(Theme.Space.xl)
        }
        .pageBackground()
        .navigationTitle("历史")
        .task { await load() }
    }

    private var equityChart: some View {
        Chart(equityCurve, id: \.index) { point in
            AreaMark(x: .value("序号", point.index), y: .value("累计", point.value))
                .foregroundStyle(.linearGradient(
                    colors: [Theme.purple.opacity(0.38), Theme.blue.opacity(0.03)],
                    startPoint: .top, endPoint: .bottom))
            LineMark(x: .value("序号", point.index), y: .value("累计", point.value))
                .foregroundStyle(Theme.brand)
                .lineStyle(.init(lineWidth: 2))
        }
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks { _ in
                AxisGridLine().foregroundStyle(Theme.border)
                AxisValueLabel().font(Theme.Font_.label).foregroundStyle(Theme.muted)
            }
        }
        .frame(height: 190)
    }

    private var dailyChart: some View {
        Chart(dailyPnl, id: \.day) { d in
            BarMark(x: .value("日期", String(d.day.suffix(5))), y: .value("盈亏", d.pnl))
                .foregroundStyle(d.pnl >= 0 ? Theme.green : Theme.red)
                .cornerRadius(2)
        }
        .chartXAxis {
            AxisMarks { _ in
                AxisValueLabel().font(.system(size: 8)).foregroundStyle(Theme.muted)
            }
        }
        .chartYAxis {
            AxisMarks { _ in
                AxisGridLine().foregroundStyle(Theme.border)
                AxisValueLabel().font(Theme.Font_.label).foregroundStyle(Theme.muted)
            }
        }
        .frame(height: 190)
    }

    private var table: some View {
        // .leading: the columns are fixed-width, so a centring stack would
        // float the whole table away from the panel title.
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: Theme.Space.sm) {
                ColumnHeader(text: "时间").frame(width: cols[0], alignment: .leading)
                ColumnHeader(text: "代码").frame(width: cols[1], alignment: .leading)
                ColumnHeader(text: "数量").frame(width: cols[2], alignment: .trailing)
                ColumnHeader(text: "入场").frame(width: cols[3], alignment: .trailing)
                ColumnHeader(text: "离场").frame(width: cols[4], alignment: .trailing)
                ColumnHeader(text: "原因").frame(width: cols[5], alignment: .leading)
                ColumnHeader(text: "策略").frame(width: cols[6], alignment: .leading)
                ColumnHeader(text: "R").frame(width: cols[7], alignment: .trailing)
                ColumnHeader(text: "盈亏").frame(width: cols[8], alignment: .trailing)
            }
            .padding(.horizontal, Theme.Space.md)
            .padding(.bottom, Theme.Space.xs)

            LazyVStack(alignment: .leading, spacing: 2) {
                ForEach(trades.reversed()) { t in
                    HStack(spacing: Theme.Space.sm) {
                        Text(Fmt.stamp(t.ts))
                            .font(Theme.Font_.mono).foregroundStyle(Theme.muted)
                            .frame(width: cols[0], alignment: .leading)
                        Text(t.symbol ?? "—")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(Theme.strong)
                            .frame(width: cols[1], alignment: .leading)
                        NumCell(text: Fmt.qty(t.qty)).frame(width: cols[2])
                        NumCell(text: Fmt.money(t.entry)).frame(width: cols[3])
                        NumCell(text: Fmt.money(t.exitPrice), tint: Theme.strong)
                            .frame(width: cols[4])
                        Pill(text: t.exitReason ?? "—", tint: exitTint(t.exitReason))
                            .frame(width: cols[5], alignment: .leading)
                        Text(t.strategy ?? "—")
                            .font(Theme.Font_.body).foregroundStyle(Theme.muted)
                            .frame(width: cols[6], alignment: .leading)
                        NumCell(text: t.rMultiple.map { String(format: "%.2f", $0) } ?? "—",
                                tint: pnlColor(t.rMultiple)).frame(width: cols[7])
                        NumCell(text: "\(Fmt.signed(t.pnl)) (\(Fmt.pct(t.pnlPct)))",
                                tint: pnlColor(t.pnl)).frame(width: cols[8])
                    }
                    .padding(.horizontal, Theme.Space.md)
                    .padding(.vertical, Theme.Space.sm)
                    .rowSurface()
                }
            }
        }
    }

    /// SL red, TP green, everything else neutral — matches the P&L language.
    private func exitTint(_ reason: String?) -> Color {
        switch reason {
        case "SL": return Theme.red
        case "TP": return Theme.green
        case "BREAKEVEN": return Theme.muted
        default: return Theme.blue
        }
    }

    private func load() async {
        loading = trades.isEmpty
        trades = (try? await APIClient.shared.closedTrades(limit: 400)) ?? []
        loading = false
    }
}
