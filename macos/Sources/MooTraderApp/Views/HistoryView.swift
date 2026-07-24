import Charts
import SwiftUI

/// Closed trades: cumulative equity curve + daily P&L + the trade table.
struct HistoryView: View {
    @ObservedObject private var l10n = L10n.shared
    @State private var trades: [ClosedTrade] = []
    @State private var loading = true
    @State private var tableWidth: CGFloat = 0

    /// Column weights — justified to the panel width once measured (fixed px as
    /// the pre-measure fallback).
    private let weights: [CGFloat] = [92, 66, 50, 74, 74, 92, 92, 56, 140]
    private var cols: [CGFloat] { columnWidths(weights, total: tableWidth) }

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
        // Fills the window: the charts are a fixed band on top and the trade
        // table flexes to fill the rest, scrolling inside itself.
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            if loading {
                Panel(title: L("历史", "History"), fillHeight: true) {
                    ProgressView().tint(Theme.blue)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else if trades.isEmpty {
                Panel(title: L("历史", "History"), fillHeight: true) {
                    EmptyNote(text: L("暂无已平仓交易", "No closed trades yet"))
                        .frame(maxHeight: .infinity)
                }
            } else {
                HStack(alignment: .top, spacing: Theme.Space.md) {
                    Panel(title: L("净值曲线", "Equity curve"), subtitle: L("累计已实现", "cumulative realized")) { equityChart }
                    Panel(title: L("每日盈亏", "Daily P&L"), subtitle: L("最近 30 个交易日", "last 30 sessions")) { dailyChart }
                        .frame(width: 380)
                }
                .frame(height: 220)
                Panel(title: L("已平仓", "Closed"), subtitle: L("\(trades.count) 笔", "\(trades.count)"),
                      fillHeight: true) { table }
            }
        }
        .padding(Theme.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .pageBackground()
        .navigationTitle(L("历史", "History"))
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
        // Header pinned; only the rows scroll, so the columns stay labelled as
        // the list grows past the panel.
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: Theme.Space.sm) {
                // Header alignment must match each data cell's, or the labels
                // drift off their columns once the widths stretch.
                ColumnHeader(text: L("时间", "Time"), alignment: .leading).frame(width: cols[0])
                ColumnHeader(text: L("代码", "Sym"), alignment: .leading).frame(width: cols[1])
                ColumnHeader(text: L("数量", "Qty"), alignment: .trailing).frame(width: cols[2])
                ColumnHeader(text: L("入场", "Entry"), alignment: .trailing).frame(width: cols[3])
                ColumnHeader(text: L("离场", "Exit"), alignment: .trailing).frame(width: cols[4])
                ColumnHeader(text: L("原因", "Reason"), alignment: .leading).frame(width: cols[5])
                ColumnHeader(text: L("策略", "Strategy"), alignment: .leading).frame(width: cols[6])
                ColumnHeader(text: "R", alignment: .trailing).frame(width: cols[7])
                ColumnHeader(text: L("盈亏", "P&L"), alignment: .trailing).frame(width: cols[8])
            }
            .padding(.horizontal, Theme.Space.md)
            .padding(.bottom, Theme.Space.xs)

            ScrollView {
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
        .measureWidth { tableWidth = $0 }
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
