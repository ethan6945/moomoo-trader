import SwiftUI

/// Native replacement for the web dashboard's main tab: account cards,
/// market/engine state, scheduler controls, trade record, positions, activity.
struct OverviewView: View {
    @EnvironmentObject var poller: StatusPoller
    @ObservedObject private var l10n = L10n.shared

    private var s: TraderStatus? { poller.status }

    var body: some View {
        // Fills the window height instead of an outer ScrollView, so the page
        // fits without scrolling. Order: title + engine/scheduler bar → stat
        // cards → US sectors → positions → activity. The sectors grid sizes to
        // its content (no inner scroll); positions caps at a few rows and scrolls
        // for the rest; the activity log is the flexible section that absorbs the
        // leftover height.
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            header
            cards
                .frame(height: 112)   // fixed, or the cards' inner spacer grows greedily
            if let sec = poller.sectors, !sec.sectors.isEmpty {
                Panel(title: L("美国板块总览", "US sectors"), subtitle: sec.sessionLabel) {
                    SectorGrid(overview: sec, tileHeight: sectorTileHeight)
                }
            }
            Panel(title: L("持仓", "Positions"), subtitle: L("\(s?.positions.count ?? 0) 只", "\(s?.positions.count ?? 0)")) {
                PositionsView(positions: s?.positions ?? [])
            }
            Panel(title: L("近期动态", "Activity"), fillHeight: true) { activityLog }
        }
        .padding(Theme.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .pageBackground()
    }


    // ── account cards — mirrors the web dashboard's four stat cards ──
    private var cards: some View {
        HStack(spacing: Theme.Space.md) {
            // ① Deployed / budget + progress bar
            MetricCard(label: L("已部署 / 预算", "Deployed / budget"),
                       value: "\(Fmt.money(s?.invested, decimals: 0)) / \(Fmt.money(s?.budget, decimals: 0))",
                       accent: Theme.blue,
                       sub: L("\(deployedPct)% 预算已部署", "\(deployedPct)% deployed")) {
                ProgressMeter(fraction: deployedFraction)
            }
            // ② Today realized + daily P&L bars
            MetricCard(label: L("今日盈亏 · 已实现", "Today · realized"),
                       value: Fmt.signed(s?.realizedPnlToday, decimals: 0),
                       valueTint: pnlColor(s?.realizedPnlToday), accent: Theme.green) {
                BarSparkline(values: dailyPnl)
            }
            // ③ Unrealized + per-position bars (moved up now that 总盈亏 is gone)
            MetricCard(label: L("浮动盈亏 · 持仓", "Unrealized · open"),
                       value: Fmt.signed(s?.unrealizedPnl, decimals: 0),
                       valueTint: pnlColor(s?.unrealizedPnl), accent: Theme.blueUp,
                       sub: L("\(s?.positionsCount ?? 0) 笔持仓", "\(s?.positionsCount ?? 0) open")) {
                BarSparkline(values: (s?.positions ?? []).map { $0.plValue ?? 0 })
            }
            // ④ Trade record — net figure, win rate, per-trade result bars
            MetricCard(label: L("战绩 · 净额", "Record · net"),
                       value: recordValue,
                       valueTint: pnlColor(s?.summary?.net), accent: Theme.purple,
                       sub: recordSub) {
                BarSparkline(values: poller.closed.map { $0.pnl ?? 0 })
            }
        }
    }

    /// Record card headline — net realized, or an em-dash before anything closes.
    private var recordValue: String {
        (s?.summary?.count ?? 0) == 0 ? "—" : Fmt.signed(s?.summary?.net, decimals: 0)
    }

    /// Record card sub — trades + win rate, mirroring the other cards' one-liner.
    private var recordSub: String {
        guard let sm = s?.summary, (sm.count ?? 0) > 0 else {
            return L("暂无成交", "No trades yet")
        }
        let n = sm.count ?? 0
        let wr = String(format: "%.0f", sm.winRate ?? 0)
        return L("\(n) 笔 · 胜率 \(wr)%", "\(n) trades · \(wr)% win")
    }

    private var deployedFraction: Double {
        guard let b = s?.budget, b > 0 else { return 0 }
        return (s?.invested ?? 0) / b
    }
    private var deployedPct: Int { Int((deployedFraction * 100).rounded()) }

    /// Daily realized P&L (last ~18 sessions) — the today card's bars.
    private var dailyPnl: [Double] {
        var byDay: [String: Double] = [:]
        for t in poller.closed where !t.day.isEmpty { byDay[t.day, default: 0] += t.pnl ?? 0 }
        return byDay.keys.sorted().suffix(18).map { byDay[$0] ?? 0 }
    }

    /// The sectors grid now sizes to its content, so a steady tile height keeps
    /// every row the same size regardless of how many sectors report.
    private let sectorTileHeight: CGFloat = 56

    // ── page header: the 总览 title, the engine/market pills right beside it,
    //    and the scheduler controls pinned to the trailing edge ──
    private var header: some View {
        HStack(alignment: .center, spacing: Theme.Space.md) {
            Text(L("总览", "Overview"))
                .font(.system(size: 22, weight: .bold))
                .foregroundStyle(Theme.strong)
                .fixedSize()
            statusPills
            Spacer(minLength: Theme.Space.sm)
            statusControls
        }
    }

    /// Engine + market state, as capsules.
    private var statusPills: some View {
        HStack(spacing: Theme.Space.sm) {
            if let pid = poller.schedulerPID {
                Pill(text: L("调度器运行中", "Scheduler running") + " · PID \(String(pid))", tint: Theme.green, icon: "●")
            } else {
                Pill(text: poller.schedulerBusy ? L("调度器切换中…", "Scheduler switching…") : L("调度器已停止", "Scheduler stopped"),
                     tint: poller.schedulerBusy ? Theme.amber : Theme.muted, icon: "○")
            }
            if let label = s?.opendLabel {
                Pill(text: "OpenD · " + l10n.opend(label), tint: (s?.opendStatus ?? "").statusColor)
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
            if let env = s?.tradeEnv {
                Pill(text: env == "REAL" ? L("实盘", "LIVE") : L("模拟盘", "PAPER"),
                     tint: env == "REAL" ? Theme.red : Theme.blue)
            }
        }
    }

    /// Start/stop the scheduler and toggle sleep prevention.
    private var statusControls: some View {
        HStack(spacing: Theme.Space.sm) {
            if poller.schedulerPID != nil {
                Button("■ " + L("停止", "Stop")) { poller.scheduler("stop") }
                    .buttonStyle(QuietButtonStyle(tint: Theme.red))
                    .disabled(poller.schedulerBusy)
            } else {
                // Preflight first. Everything key-gated in this bot fails SAFE
                // and therefore SILENTLY, so pressing start is the one moment
                // the user is guaranteed to be looking — it is where "half your
                // capabilities are switched off" has to be said out loud.
                // Fails open: if the check itself errors we start anyway rather
                // than stranding the user behind a broken diagnostic.
                Button("▶ " + L("启动", "Start")) { poller.startWithPreflight() }
                    .buttonStyle(BrandButtonStyle())
                    .disabled(poller.schedulerBusy || poller.preflightBusy)
            }
            Button("☕ " + (poller.caffeinate?.on == true ? L("防睡眠 开", "Awake on") : L("防睡眠 关", "Awake off"))) {
                poller.toggleCaffeinate()
            }
            .buttonStyle(QuietButtonStyle(tint: poller.caffeinate?.on == true ? Theme.amber : Theme.muted))
        }
    }

    /// /api/log is oldest→newest, so the freshest line is last; keep it pinned
    /// to the bottom as new lines arrive (a fresh scroll each poll).
    private var activityLog: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(poller.activity.enumerated()), id: \.offset) { i, line in
                        Text(line)
                            .font(Theme.Font_.mono)
                            .foregroundStyle(logColor(line))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(i)
                    }
                    Color.clear.frame(height: 1).id(logBottomID)
                }
                .padding(Theme.Space.sm)
            }
            .frame(maxHeight: .infinity)
            .rowSurface()
            .onChange(of: poller.activity.count) { _, _ in
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(logBottomID, anchor: .bottom) }
            }
            .onAppear { proxy.scrollTo(logBottomID, anchor: .bottom) }
        }
    }

    private let logBottomID = "log-bottom"

    /// Same highlighting rules as the web log pane.
    private func logColor(_ line: String) -> Color {
        if line.range(of: "buy|BUY|开仓|TP|profit", options: .regularExpression) != nil { return Theme.green }
        if line.range(of: "SL|stop|error|fail|blacklist|⚠", options: .regularExpression) != nil { return Theme.red }
        return Theme.muted
    }
}

/// US sector heatmap — the web dashboard's 美国板块总览, natively. Index pills up
/// top, then sector tiles sorted strongest→weakest with a shared green/red tint
/// scale (intensity ∝ |pct| against the day's largest move).
struct SectorGrid: View {
    @ObservedObject private var l10n = L10n.shared
    let overview: SectorOverview
    /// Grows when positions are few, shrinks when the book fills up — so the
    /// two panels share the window's height instead of the sector block
    /// staying a fixed size while 持仓 sits mostly empty.
    var tileHeight: CGFloat = 56

    private var maxAbs: Double {
        max(0.5, overview.sectors.map { abs($0.pct ?? 0) }.max() ?? 0.5)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            if !overview.indices.isEmpty {
                HStack(spacing: Theme.Space.sm) {
                    ForEach(overview.indices) { idx in
                        HStack(spacing: 5) {
                            Text((l10n.lang == "en" ? idx.en : idx.zh) ?? idx.sym)
                                .font(Theme.Font_.body).foregroundStyle(Theme.text)
                            Text(Fmt.pct(idx.pct, decimals: 2))
                                .font(.system(size: 12, weight: .semibold))
                                .monospacedDigit()
                                .foregroundStyle(pnlColor(idx.pct))
                        }
                        .padding(.horizontal, 9).padding(.vertical, 4)
                        .background(Theme.card2, in: Capsule())
                        .overlay(Capsule().strokeBorder(Theme.border))
                        .help("\(idx.sym) · \(Fmt.money(idx.price))")
                    }
                    Spacer(minLength: 0)
                }
            }

            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: Theme.Space.sm),
                                     count: 4),
                      spacing: Theme.Space.sm) {
                ForEach(overview.sectors.sorted { ($0.pct ?? 0) > ($1.pct ?? 0) }) { tile in
                    SectorTileView(tile: tile, maxAbs: maxAbs, minHeight: tileHeight)
                }
            }
        }
    }
}

struct SectorTileView: View {
    @ObservedObject private var l10n = L10n.shared
    let tile: SectorTile
    let maxAbs: Double
    var minHeight: CGFloat = 56

    private var up: Bool { (tile.pct ?? 0) >= 0 }
    private var base: Color { up ? Theme.green : Theme.red }
    private var intensity: Double {
        0.10 + 0.30 * min(1, abs(tile.pct ?? 0) / maxAbs)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Text((l10n.lang == "en" ? tile.en : tile.zh) ?? tile.sym)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Theme.strong)
                    .lineLimit(1)
                Text(tile.sym)
                    .font(.system(size: 9))
                    .foregroundStyle(Theme.muted)
                Spacer(minLength: 0)
            }
            HStack(alignment: .firstTextBaseline) {
                Text(Fmt.pct(tile.pct, decimals: 2))
                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(base)
                Spacer(minLength: 0)
                Text(Fmt.money(tile.price))
                    .font(.system(size: 10)).monospacedDigit()
                    .foregroundStyle(Theme.muted)
            }
        }
        .padding(Theme.Space.md)
        .frame(maxWidth: .infinity, minHeight: minHeight, alignment: .topLeading)
        .background(base.opacity(intensity),
                    in: RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous)
            .strokeBorder(base.opacity(0.38)))
    }
}
