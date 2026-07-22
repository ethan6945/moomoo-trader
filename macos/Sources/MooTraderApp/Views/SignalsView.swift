import SwiftUI

/// 盯盘 — watchlist monitor tiles, alert feed, and the reporter's controls.
/// Data comes from src/signal_reporter.py via the signal-* endpoints.
struct SignalsView: View {
    @State private var ticks: [MonitorTick] = []
    @State private var alerts: [SignalAlert] = []
    @State private var running = false
    @State private var pid: Int32?
    @State private var watchlist: [String] = []
    @State private var busy = false
    @State private var note: String?
    @State private var editingWatchlist = false
    @State private var poll: Task<Void, Never>?

    private let runModes: [(id: String, label: String)] = [
        ("premarket", "盘前"), ("intraday", "盘中"), ("close", "收盘"),
        ("brief", "简报"), ("review", "复盘"), ("monitor", "单次盯盘"),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                controls
                if let note {
                    Text(note)
                        .font(Theme.Font_.body)
                        .foregroundStyle(Theme.text)
                        .padding(Theme.Space.md)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .rowSurface()
                }
                Panel(title: "自选股", subtitle: "\(watchlist.count) 只",
                      trailing: AnyView(Button("编辑") { editingWatchlist = true }
                        .buttonStyle(QuietButtonStyle(tint: Theme.blue)))) {
                    watchlistChips
                }
                Panel(title: "盯盘快照") { tiles }
                Panel(title: "警报流") { alertFeed }
            }
            .padding(Theme.Space.xl)
        }
        .pageBackground()
        .navigationTitle("信号")
        .sheet(isPresented: $editingWatchlist) {
            WatchlistEditor(tickers: watchlist) { updated in
                Task {
                    try? await APIClient.shared.setSignalWatchlist(updated)
                    watchlist = (try? await APIClient.shared.signalWatchlist()) ?? updated
                }
            }
        }
        .task {
            await load()
            poll?.cancel()
            poll = Task {
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(10))
                    await load()
                }
            }
        }
        .onDisappear { poll?.cancel() }
    }

    // ── reporter controls ──
    private var controls: some View {
        HStack(spacing: Theme.Space.sm) {
            if running {
                Pill(text: "盯盘运行中" + (pid.map { " · PID \($0)" } ?? ""),
                     tint: Theme.green, icon: "●")
            } else {
                Pill(text: "盯盘已停止", icon: "○")
            }
            Pill(text: "\(ticks.count) 只有快照", tint: Theme.blue)
            Spacer(minLength: Theme.Space.sm)
            Menu("单次运行") {
                ForEach(runModes, id: \.id) { mode in
                    Button(mode.label) { run(mode.id) }
                }
            }
            .frame(width: 104)
            .disabled(busy)

            if running {
                Button("■ 停止盯盘") { toggleScheduler() }
                    .buttonStyle(QuietButtonStyle(tint: Theme.red))
                    .disabled(busy)
            } else {
                Button("▶ 启动盯盘") { toggleScheduler() }
                    .buttonStyle(BrandButtonStyle())
                    .disabled(busy)
            }
        }
        .padding(.horizontal, Theme.Space.md)
        .padding(.vertical, Theme.Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardSurface()
    }

    private var watchlistChips: some View {
        Group {
            if watchlist.isEmpty {
                EmptyNote(text: "尚未添加自选股")
            } else {
                FlowChips(items: watchlist)
            }
        }
    }

    // ── per-symbol tiles ──
    private var tiles: some View {
        Group {
            if ticks.isEmpty {
                EmptyNote(text: "暂无盯盘数据 — 启动盯盘后每轮扫描会写入快照")
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 216), spacing: Theme.Space.md)],
                          spacing: Theme.Space.md) {
                    ForEach(ticks) { MonitorTile(tick: $0) }
                }
            }
        }
    }

    // ── alert feed ──
    private var alertFeed: some View {
        Group {
            if alerts.isEmpty {
                EmptyNote(text: "暂无警报")
            } else {
                VStack(spacing: Theme.Space.sm) {
                    ForEach(alerts.prefix(60)) { a in
                        HStack(alignment: .top, spacing: Theme.Space.sm) {
                            RoundedRectangle(cornerRadius: 2)
                                .fill(a.isPush ? Theme.amber : Theme.border)
                                .frame(width: 3)
                            Text(a.emoji ?? "•").font(.system(size: 13))
                            VStack(alignment: .leading, spacing: 2) {
                                HStack(spacing: Theme.Space.sm) {
                                    Text(a.sym ?? "—")
                                        .font(.system(size: 12, weight: .semibold))
                                        .foregroundStyle(Theme.strong)
                                    Text(a.title ?? "")
                                        .font(.system(size: 12))
                                        .foregroundStyle(Theme.text)
                                    if a.isPush { Pill(text: "push", tint: Theme.amber) }
                                }
                                Text(a.detail ?? "")
                                    .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                            }
                            Spacer(minLength: Theme.Space.sm)
                            VStack(alignment: .trailing, spacing: 2) {
                                Text(Fmt.money(a.price))
                                    .font(Theme.Font_.body).monospacedDigit()
                                    .foregroundStyle(Theme.strong)
                                Text(Fmt.pct(a.chg, decimals: 2))
                                    .font(Theme.Font_.label).monospacedDigit()
                                    .foregroundStyle(pnlColor(a.chg))
                                Text(Fmt.stamp(a.tsIso))
                                    .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                            }
                        }
                        .padding(Theme.Space.sm)
                        .rowSurface()
                    }
                }
            }
        }
    }

    // ── actions ──
    private func load() async {
        if let s = try? await APIClient.shared.signalStatus() {
            running = s.running ?? false
            pid = s.pid
        }
        ticks = (try? await APIClient.shared.monitorTicks()) ?? ticks
        alerts = (try? await APIClient.shared.signalAlerts()) ?? alerts
        watchlist = (try? await APIClient.shared.signalWatchlist()) ?? watchlist
    }

    private func toggleScheduler() {
        busy = true
        Task {
            defer { busy = false }
            try? await APIClient.shared.signalScheduler(running ? "stop" : "start")
            await load()
        }
    }

    private func run(_ mode: String) {
        busy = true
        note = "正在运行…"
        Task {
            defer { busy = false }
            do {
                try await APIClient.shared.signalRun(mode)
                note = "已触发 \(mode) — 结果会推送到 Telegram，警报流稍后更新"
            } catch {
                note = "运行失败：\(error.localizedDescription)"
            }
            await load()
        }
    }
}

/// One watchlist symbol's latest tick.
struct MonitorTile: View {
    let tick: MonitorTick

    private var netTint: Color {
        guard let n = tick.net else { return Theme.muted }
        if n >= 5 { return Theme.green }
        if n <= -5 { return Theme.red }
        return Theme.muted
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.sm) {
            HStack {
                Text(tick.symbol)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.strong)
                Spacer()
                Text(Fmt.pct(tick.chg, decimals: 2))
                    .font(Theme.Font_.figureSm).monospacedDigit()
                    .foregroundStyle(pnlColor(tick.chg))
            }
            Text(Fmt.money(tick.price))
                .font(Theme.Font_.figure).monospacedDigit()
                .foregroundStyle(Theme.strong)

            HStack(spacing: Theme.Space.xs) {
                if let rsi = tick.rsi {
                    Pill(text: "RSI \(Int(rsi))",
                         tint: rsi >= 78 ? Theme.red : (rsi <= 22 ? Theme.green : Theme.muted))
                }
                if let vol = tick.vol {
                    Pill(text: String(format: "%.1fx", vol),
                         tint: vol >= 2.5 ? Theme.amber : Theme.muted)
                }
                if let above = tick.aboveVwap {
                    Pill(text: above ? "VWAP↑" : "VWAP↓",
                         tint: above ? Theme.green : Theme.red)
                }
            }

            HStack(spacing: Theme.Space.xs) {
                Pill(text: "买\(tick.buy ?? 0)/卖\(tick.sell ?? 0)", tint: netTint)
                if let n = tick.alertsToday, n > 0 { Pill(text: "今日 \(n) 警报") }
            }

            if let last = tick.lastAlert {
                Text(last)
                    .font(Theme.Font_.label).foregroundStyle(Theme.text)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            Text(Fmt.stamp(tick.ts)).font(Theme.Font_.label).foregroundStyle(Theme.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Space.md)
        .rowSurface()
    }
}

/// Simple wrapping chip row (watchlist symbols).
struct FlowChips: View {
    let items: [String]

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 66), spacing: 6)],
                  alignment: .leading, spacing: 6) {
            ForEach(items, id: \.self) { Pill(text: $0) }
        }
    }
}

/// One-symbol-per-line editor; the server normalises (upper/trim/dedupe).
struct WatchlistEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    let onSave: ([String]) -> Void

    init(tickers: [String], onSave: @escaping ([String]) -> Void) {
        _text = State(initialValue: tickers.joined(separator: "\n"))
        self.onSave = onSave
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Space.md) {
            Text("盯盘自选股")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(Theme.strong)
            Text("一行一个代码，保存后写入 config/signal_watchlist.json")
                .font(Theme.Font_.label)
                .foregroundStyle(Theme.muted)
            TextEditor(text: $text)
                .font(Theme.Font_.mono)
                .foregroundStyle(Theme.text)
                .scrollContentBackground(.hidden)
                .padding(Theme.Space.sm)
                .frame(width: 260, height: 300)
                .rowSurface()
                .overlay(RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous)
                    .strokeBorder(Theme.border))
            HStack {
                Button("取消") { dismiss() }
                    .buttonStyle(QuietButtonStyle())
                Spacer()
                Button("保存") {
                    let list = text.split(whereSeparator: \.isNewline)
                        .map { $0.trimmingCharacters(in: .whitespaces).uppercased() }
                        .filter { !$0.isEmpty }
                    onSave(list)
                    dismiss()
                }
                .buttonStyle(BrandButtonStyle())
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(Theme.Space.xl)
        .pageBackground()
    }
}
