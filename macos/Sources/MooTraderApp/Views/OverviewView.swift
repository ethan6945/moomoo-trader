import SwiftUI

/// Native replacement for the web dashboard's main tab: account cards,
/// market/engine state, scheduler controls, trade record, positions, activity.
struct OverviewView: View {
    @EnvironmentObject var poller: StatusPoller

    private var s: TraderStatus? { poller.status }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                cards
                stateRow
                HStack(alignment: .top, spacing: 14) {
                    Panel(title: "战绩") { TradeRecordView(summary: s?.summary) }
                        .frame(maxWidth: 360)
                    Panel(title: "近期动态") { activityLog }
                }
                Panel(title: "持仓") { PositionsView(positions: s?.positions ?? []) }
            }
            .padding(16)
        }
        .navigationTitle("总览")
    }

    // ── account numbers ──
    private var cards: some View {
        HStack(spacing: 10) {
            StatCard(label: "净值", value: Fmt.money(s?.equity),
                     footnote: "现金 \(Fmt.money(s?.cash, decimals: 0))")
            StatCard(label: "持仓市值", value: Fmt.money(s?.invested),
                     footnote: "\(s?.positionsCount ?? 0) 只")
            StatCard(label: "未实现盈亏", value: Fmt.signed(s?.unrealizedPnl),
                     tint: pnlColor(s?.unrealizedPnl))
            StatCard(label: "已实现（累计）", value: Fmt.signed(s?.realizedPnlTotal),
                     tint: pnlColor(s?.realizedPnlTotal),
                     footnote: "今日 \(Fmt.signed(s?.realizedPnlToday))")
            StatCard(label: "预算", value: Fmt.money(s?.budget, decimals: 0),
                     footnote: "风险敞口 \(Fmt.money(s?.openRisk, decimals: 0)) / \(Fmt.money(s?.heatCap, decimals: 0))")
        }
    }

    // ── engine + market state, and the scheduler controls ──
    private var stateRow: some View {
        HStack(spacing: 8) {
            if let pid = poller.schedulerPID {
                Pill(text: "调度器运行中 · PID \(String(pid))", tint: .green, icon: "●")
            } else {
                Pill(text: poller.schedulerBusy ? "调度器切换中…" : "调度器已停止",
                     tint: poller.schedulerBusy ? .orange : .secondary, icon: "○")
            }
            if let label = s?.opendLabel {
                Pill(text: "OpenD: \(label)", tint: (s?.opendStatus ?? "").statusColor)
            }
            if let regime = s?.regime {
                Pill(text: [regime, s?.regimeSub].compactMap { $0 }
                        .filter { !$0.isEmpty }.joined(separator: " · "),
                     tint: regime == "BULL" ? .green : (regime == "BEAR" ? .red : .secondary))
                    .help(s?.regimeNote ?? "")
            }
            if let vix = s?.vix { Pill(text: "VIX \(String(format: "%.1f", vix))") }
            if let phase = s?.phase { Pill(text: phase) }
            if let env = s?.tradeEnv {
                Pill(text: env == "REAL" ? "实盘" : "模拟盘", tint: env == "REAL" ? .red : .blue)
            }

            Spacer()

            if poller.schedulerPID != nil {
                Button("■ 停止") { poller.scheduler("stop") }
                    .disabled(poller.schedulerBusy)
            } else {
                Button("▶ 启动") { poller.scheduler("start") }
                    .disabled(poller.schedulerBusy)
            }
            Button(poller.caffeinate?.on == true ? "☕ 防睡眠 开" : "☕ 防睡眠 关") {
                poller.toggleCaffeinate()
            }
        }
    }

    private var activityLog: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(poller.activity.enumerated()), id: \.offset) { _, line in
                    Text(line)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(logColor(line))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .frame(height: 132)
    }

    /// Same highlighting rules as the web log pane.
    private func logColor(_ line: String) -> Color {
        if line.range(of: "buy|BUY|开仓|TP|profit", options: .regularExpression) != nil { return .green }
        if line.range(of: "SL|stop|error|fail|blacklist|⚠", options: .regularExpression) != nil { return .red }
        return .secondary
    }
}

/// 交易次数 / 胜率 / 盈亏比 / 净额 — mirrors the web "战绩" boxes.
struct TradeRecordView: View {
    let summary: TradeSummary?

    var body: some View {
        let n = summary?.count ?? 0
        let wr = summary?.winRate ?? 0
        let pf = summary?.profitFactor
        HStack(spacing: 10) {
            box("交易", n == 0 ? "—" : String(n), .primary)
            box("胜率", n == 0 ? "—" : String(format: "%.1f%%", wr),
                wr >= 50 ? .green : (wr >= 40 ? .orange : .red))
            box("盈亏比", pfText(pf), (pf ?? 0) >= 1 ? .green : .red)
            box("净额", n == 0 ? "—" : Fmt.signed(summary?.net), pnlColor(summary?.net))
        }
    }

    private func pfText(_ pf: Double?) -> String {
        guard let pf else { return "—" }
        return pf.isInfinite ? "∞" : String(format: "%.2f", pf)
    }

    private func box(_ k: String, _ v: String, _ tint: Color) -> some View {
        VStack(spacing: 3) {
            Text(k).font(.caption2).foregroundStyle(.secondary)
            Text(v).font(.system(size: 16, weight: .semibold, design: .rounded))
                .foregroundStyle(tint)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
    }
}
