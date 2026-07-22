import SwiftUI

/// Native replacement for the web dashboard's main tab: account cards,
/// market/engine state, scheduler controls, trade record, positions, activity.
struct OverviewView: View {
    @EnvironmentObject var poller: StatusPoller

    private var s: TraderStatus? { poller.status }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                cards
                statusBar
                HStack(alignment: .top, spacing: Theme.Space.md) {
                    // fillHeight: match the log panel so the bottom edges line up.
                    Panel(title: "战绩", subtitle: streakNote, fillHeight: true) {
                        TradeRecordView(summary: s?.summary)
                    }
                    .frame(width: 340)
                    Panel(title: "近期动态") { activityLog }
                }
                Panel(title: "持仓", subtitle: "\(s?.positions.count ?? 0) 只") {
                    PositionsView(positions: s?.positions ?? [])
                }
            }
            .padding(Theme.Space.xl)
        }
        .pageBackground()
        .navigationTitle("总览")
    }

    // ── account numbers ──
    private var cards: some View {
        HStack(spacing: Theme.Space.md) {
            StatCard(label: "净值", value: Fmt.money(s?.equity),
                     footnote: "现金 \(Fmt.money(s?.cash, decimals: 0))", accent: true)
            StatCard(label: "持仓市值", value: Fmt.money(s?.invested),
                     footnote: "\(s?.positionsCount ?? 0) 只持仓")
            StatCard(label: "未实现盈亏", value: Fmt.signed(s?.unrealizedPnl),
                     tint: pnlColor(s?.unrealizedPnl), footnote: unrealizedNote)
            StatCard(label: "已实现累计", value: Fmt.signed(s?.realizedPnlTotal),
                     tint: pnlColor(s?.realizedPnlTotal),
                     footnote: "今日 \(Fmt.signed(s?.realizedPnlToday, decimals: 0))")
            StatCard(label: "预算", value: Fmt.money(s?.budget, decimals: 0),
                     footnote: "敞口 \(Fmt.money(s?.openRisk, decimals: 0)) / \(Fmt.money(s?.heatCap, decimals: 0))")
        }
    }

    /// Return on cost — the only card without a footnote otherwise, which left
    /// a visible hole in the row.
    private var unrealizedNote: String {
        guard let invested = s?.invested, invested > 0,
              let pnl = s?.unrealizedPnl else { return "无持仓" }
        return "成本回报 \(Fmt.pct(pnl / invested * 100))"
    }

    private var streakNote: String? {
        guard let sm = s?.summary, let streak = sm.streak, streak > 0 else { return nil }
        return sm.streakKind == "win" ? "\(streak) 连胜" : (sm.streakKind == "loss" ? "\(streak) 连败" : nil)
    }

    // ── engine + market state, and the scheduler controls ──
    private var statusBar: some View {
        HStack(spacing: Theme.Space.sm) {
            if let pid = poller.schedulerPID {
                Pill(text: "调度器运行中 · PID \(String(pid))", tint: Theme.green, icon: "●")
            } else {
                Pill(text: poller.schedulerBusy ? "调度器切换中…" : "调度器已停止",
                     tint: poller.schedulerBusy ? Theme.amber : Theme.muted, icon: "○")
            }
            if let label = s?.opendLabel {
                Pill(text: "OpenD · \(label)", tint: (s?.opendStatus ?? "").statusColor)
            }
            if let regime = s?.regime, !regime.isEmpty {
                Pill(text: [regime, s?.regimeSub].compactMap { $0 }
                        .filter { !$0.isEmpty }.joined(separator: " · "),
                     tint: regime == "BULL" ? Theme.green : (regime == "BEAR" ? Theme.red : Theme.muted))
                    .help(s?.regimeNote ?? "")
            }
            if let vix = s?.vix {
                Pill(text: "VIX \(String(format: "%.1f", vix))",
                     tint: vix >= 25 ? Theme.amber : Theme.muted)
            }
            if let phase = s?.phase, !phase.isEmpty { Pill(text: phase) }
            if let env = s?.tradeEnv {
                Pill(text: env == "REAL" ? "实盘" : "模拟盘",
                     tint: env == "REAL" ? Theme.red : Theme.blue)
            }

            Spacer(minLength: Theme.Space.sm)

            if poller.schedulerPID != nil {
                Button("■ 停止") { poller.scheduler("stop") }
                    .buttonStyle(QuietButtonStyle(tint: Theme.red))
                    .disabled(poller.schedulerBusy)
            } else {
                Button("▶ 启动") { poller.scheduler("start") }
                    .buttonStyle(BrandButtonStyle())
                    .disabled(poller.schedulerBusy)
            }
            Button(poller.caffeinate?.on == true ? "☕ 防睡眠 开" : "☕ 防睡眠 关") {
                poller.toggleCaffeinate()
            }
            .buttonStyle(QuietButtonStyle(tint: poller.caffeinate?.on == true ? Theme.amber : Theme.muted))
        }
        .padding(.horizontal, Theme.Space.md)
        .padding(.vertical, Theme.Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardSurface()
    }

    private var activityLog: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(poller.activity.enumerated()), id: \.offset) { _, line in
                    Text(line)
                        .font(Theme.Font_.mono)
                        .foregroundStyle(logColor(line))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(Theme.Space.sm)
        }
        .frame(height: 150)
        // Fade the last line out instead of slicing it in half — the cut edge
        // reads as a rendering glitch, the fade reads as "there's more".
        .mask(LinearGradient(stops: [.init(color: .black, location: 0),
                                     .init(color: .black, location: 0.88),
                                     .init(color: .clear, location: 1)],
                             startPoint: .top, endPoint: .bottom))
        .rowSurface()
    }

    /// Same highlighting rules as the web log pane.
    private func logColor(_ line: String) -> Color {
        if line.range(of: "buy|BUY|开仓|TP|profit", options: .regularExpression) != nil { return Theme.green }
        if line.range(of: "SL|stop|error|fail|blacklist|⚠", options: .regularExpression) != nil { return Theme.red }
        return Theme.muted
    }
}

/// 交易次数 / 胜率 / 盈亏比 / 净额 — mirrors the web "战绩" boxes.
struct TradeRecordView: View {
    let summary: TradeSummary?

    var body: some View {
        let n = summary?.count ?? 0
        let wr = summary?.winRate ?? 0
        let pf = summary?.profitFactor
        // 2×2, not 1×4: across a 340pt panel four columns leave each box barely
        // 78pt wide, and a single row can't fill the height the log panel sets.
        VStack(spacing: Theme.Space.sm) {
            HStack(spacing: Theme.Space.sm) {
                box("交易", n == 0 ? "—" : String(n), Theme.strong, nil)
                box("胜率", n == 0 ? "—" : String(format: "%.0f%%", wr),
                    wr >= 50 ? Theme.green : (wr >= 40 ? Theme.amber : Theme.red),
                    n == 0 ? nil : wr / 100)
            }
            HStack(spacing: Theme.Space.sm) {
                box("盈亏比", pfText(pf), (pf ?? 0) >= 1 ? Theme.green : Theme.red, nil)
                box("净额", n == 0 ? "—" : Fmt.signed(summary?.net, decimals: 0),
                    pnlColor(summary?.net), nil)
            }
        }
        .frame(maxHeight: .infinity)
    }

    private func pfText(_ pf: Double?) -> String {
        guard let pf else { return "—" }
        return pf.isInfinite ? "∞" : String(format: "%.2f", pf)
    }

    /// `ratio` draws a thin progress rail under the figure (win rate only).
    private func box(_ k: String, _ v: String, _ tint: Color, _ ratio: Double?) -> some View {
        VStack(spacing: Theme.Space.xs) {
            Text(k).font(Theme.Font_.label).foregroundStyle(Theme.muted)
            Text(v).font(Theme.Font_.figureSm).monospacedDigit().foregroundStyle(tint)
                .lineLimit(1).minimumScaleFactor(0.6)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.border).frame(height: 3)
                    if let ratio {
                        Capsule().fill(tint)
                            .frame(width: geo.size.width * min(1, max(0, ratio)), height: 3)
                    }
                }
            }
            .frame(height: 3)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(.vertical, Theme.Space.sm)
        .padding(.horizontal, Theme.Space.sm)
        .rowSurface()
    }
}
