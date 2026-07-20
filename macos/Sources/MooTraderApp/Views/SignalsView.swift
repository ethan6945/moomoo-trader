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
            VStack(alignment: .leading, spacing: 14) {
                controls
                if let note {
                    Text(note).font(.caption).foregroundStyle(.secondary)
                }
                Panel(title: "自选股 · \(watchlist.count)",
                      trailing: AnyView(Button("编辑") { editingWatchlist = true })) {
                    watchlistChips
                }
                Panel(title: "盯盘快照") { tiles }
                Panel(title: "警报流") { alertFeed }
            }
            .padding(16)
        }
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
        HStack(spacing: 8) {
            if running {
                Pill(text: "盯盘运行中" + (pid.map { " · PID \($0)" } ?? ""),
                     tint: .green, icon: "●")
            } else {
                Pill(text: "盯盘已停止", icon: "○")
            }
            Spacer()
            Menu("单次运行") {
                ForEach(runModes, id: \.id) { mode in
                    Button(mode.label) { run(mode.id) }
                }
            }
            .frame(width: 110)
            .disabled(busy)

            Button(running ? "■ 停止盯盘" : "▶ 启动盯盘") {
                toggleScheduler()
            }
            .disabled(busy)
        }
    }

    private var watchlistChips: some View {
        Group {
            if watchlist.isEmpty {
                Text("尚未添加自选股").foregroundStyle(.secondary).font(.callout)
            } else {
                FlowChips(items: watchlist)
            }
        }
    }

    // ── per-symbol tiles ──
    private var tiles: some View {
        Group {
            if ticks.isEmpty {
                Text("暂无盯盘数据 — 启动盯盘后每轮扫描会写入快照")
                    .foregroundStyle(.secondary)
                    .font(.callout)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 18)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 210), spacing: 10)],
                          spacing: 10) {
                    ForEach(ticks) { MonitorTile(tick: $0) }
                }
            }
        }
    }

    // ── alert feed ──
    private var alertFeed: some View {
        Group {
            if alerts.isEmpty {
                Text("暂无警报").foregroundStyle(.secondary).font(.callout)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 14)
            } else {
                VStack(spacing: 6) {
                    ForEach(alerts.prefix(60)) { a in
                        HStack(alignment: .top, spacing: 8) {
                            Text(a.emoji ?? "•")
                            VStack(alignment: .leading, spacing: 2) {
                                HStack(spacing: 6) {
                                    Text(a.sym ?? "—").fontWeight(.semibold)
                                    Text(a.title ?? "").font(.callout)
                                    if a.isPush { Pill(text: "push", tint: .orange) }
                                }
                                Text(a.detail ?? "")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 6)
                            VStack(alignment: .trailing, spacing: 2) {
                                Text(Fmt.money(a.price)).font(.caption).monospacedDigit()
                                Text(Fmt.pct(a.chg, decimals: 2))
                                    .font(.caption2).foregroundStyle(pnlColor(a.chg))
                                Text(Fmt.stamp(a.tsIso))
                                    .font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                        .padding(8)
                        .background(.quaternary.opacity(0.28),
                                    in: RoundedRectangle(cornerRadius: 7))
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
        guard let n = tick.net else { return .secondary }
        if n >= 5 { return .green }
        if n <= -5 { return .red }
        return .secondary
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(tick.symbol).fontWeight(.semibold)
                Spacer()
                Text(Fmt.pct(tick.chg, decimals: 2))
                    .font(.callout).foregroundStyle(pnlColor(tick.chg))
            }
            Text(Fmt.money(tick.price))
                .font(.system(size: 19, weight: .semibold, design: .rounded))

            HStack(spacing: 5) {
                if let rsi = tick.rsi {
                    Pill(text: "RSI \(Int(rsi))",
                         tint: rsi >= 78 ? .red : (rsi <= 22 ? .green : .secondary))
                }
                if let vol = tick.vol {
                    Pill(text: String(format: "%.1fx", vol), tint: vol >= 2.5 ? .orange : .secondary)
                }
                if let above = tick.aboveVwap {
                    Pill(text: above ? "VWAP↑" : "VWAP↓", tint: above ? .green : .red)
                }
            }

            HStack(spacing: 5) {
                Pill(text: "买\(tick.buy ?? 0)/卖\(tick.sell ?? 0)", tint: netTint)
                if let n = tick.alertsToday, n > 0 {
                    Pill(text: "今日 \(n) 警报")
                }
            }

            if let last = tick.lastAlert {
                Text(last).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            Text(Fmt.stamp(tick.ts)).font(.caption2).foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 9))
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
        VStack(alignment: .leading, spacing: 12) {
            Text("盯盘自选股").font(.headline)
            Text("一行一个代码，保存后写入 config/signal_watchlist.json")
                .font(.caption).foregroundStyle(.secondary)
            TextEditor(text: $text)
                .font(.system(.body, design: .monospaced))
                .frame(width: 260, height: 300)
                .border(.quaternary)
            HStack {
                Button("取消") { dismiss() }
                Spacer()
                Button("保存") {
                    let list = text.split(whereSeparator: \.isNewline)
                        .map { $0.trimmingCharacters(in: .whitespaces).uppercased() }
                        .filter { !$0.isEmpty }
                    onSave(list)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
    }
}
