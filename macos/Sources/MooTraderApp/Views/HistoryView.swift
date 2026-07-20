import Charts
import SwiftUI

/// Closed trades: cumulative equity curve + daily P&L + the trade table.
struct HistoryView: View {
    @State private var trades: [ClosedTrade] = []
    @State private var loading = true

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
        for t in trades where !t.day.isEmpty {
            byDay[t.day, default: 0] += t.pnl ?? 0
        }
        return byDay.keys.sorted().suffix(30).map { ($0, byDay[$0] ?? 0) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if loading {
                    ProgressView().frame(maxWidth: .infinity).padding(.vertical, 40)
                } else if trades.isEmpty {
                    Text("暂无已平仓交易")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity).padding(.vertical, 40)
                } else {
                    Panel(title: "净值曲线（累计已实现）") { equityChart }
                    Panel(title: "每日盈亏") { dailyChart }
                    Panel(title: "已平仓 · \(trades.count) 笔") { table }
                }
            }
            .padding(16)
        }
        .navigationTitle("历史")
        .task { await load() }
        .refreshable { await load() }
    }

    private var equityChart: some View {
        Chart(equityCurve, id: \.index) { point in
            AreaMark(x: .value("序号", point.index), y: .value("累计", point.value))
                .foregroundStyle(.linearGradient(
                    colors: [.accentColor.opacity(0.35), .accentColor.opacity(0.02)],
                    startPoint: .top, endPoint: .bottom))
            LineMark(x: .value("序号", point.index), y: .value("累计", point.value))
                .foregroundStyle(Color.accentColor)
        }
        .chartXAxis(.hidden)
        .frame(height: 180)
    }

    private var dailyChart: some View {
        Chart(dailyPnl, id: \.day) { d in
            BarMark(x: .value("日期", String(d.day.suffix(5))), y: .value("盈亏", d.pnl))
                .foregroundStyle(d.pnl >= 0 ? Color.green : Color.red)
        }
        .frame(height: 130)
    }

    private var table: some View {
        Table(trades.reversed()) {
            TableColumn("时间") { t in Text(Fmt.stamp(t.ts)) }
                .width(min: 80, ideal: 92)
            TableColumn("代码") { t in Text(t.symbol ?? "—").fontWeight(.semibold) }
                .width(min: 60, ideal: 68)
            TableColumn("数量") { t in Text(Fmt.qty(t.qty)) }
                .width(min: 44, ideal: 50)
            TableColumn("入场") { t in Text(Fmt.money(t.entry)) }
                .width(min: 62, ideal: 74)
            TableColumn("离场") { t in Text(Fmt.money(t.exitPrice)) }
                .width(min: 62, ideal: 74)
            TableColumn("原因") { t in Pill(text: t.exitReason ?? "—") }
                .width(min: 70, ideal: 96)
            TableColumn("策略") { t in Text(t.strategy ?? "—").foregroundStyle(.secondary) }
                .width(min: 70, ideal: 96)
            TableColumn("R") { t in
                Text(t.rMultiple.map { String(format: "%.2f", $0) } ?? "—")
                    .foregroundStyle(pnlColor(t.rMultiple))
            }
            .width(min: 44, ideal: 54)
            TableColumn("盈亏") { t in
                Text("\(Fmt.signed(t.pnl)) (\(Fmt.pct(t.pnlPct)))")
                    .foregroundStyle(pnlColor(t.pnl))
            }
            .width(min: 110, ideal: 140)
        }
        .frame(minHeight: 260, idealHeight: min(560, Double(trades.count) * 26 + 40))
    }

    private func load() async {
        loading = trades.isEmpty
        trades = (try? await APIClient.shared.closedTrades(limit: 400)) ?? []
        loading = false
    }
}
