import SwiftUI

/// 设置 — trade env, budget, AI engine, .env keys, web exposure, stats reset.
/// Every mutation goes through the same endpoints the web panel uses, so the
/// Python side stays the single source of truth (and keeps its guards).
struct SettingsPanelView: View {
    @EnvironmentObject var poller: StatusPoller

    @State private var env: TradeEnvState?
    @State private var autoBudget: AutoBudgetState?
    @State private var ai: AIProviderState?
    @State private var aiModels: [String] = []
    @State private var keys: [SettingsKeys.Item] = []
    @State private var access: WebAccessState?
    @State private var budgetText = ""
    @State private var note: String?
    @State private var busy = false

    @State private var confirmReal = false
    @State private var confirmReset = false
    @State private var editingKey: SettingsKeys.Item?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let note {
                    Text(note)
                        .font(.callout)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary.opacity(0.35),
                                    in: RoundedRectangle(cornerRadius: 8))
                }
                tradeEnvPanel
                budgetPanel
                aiPanel
                keysPanel
                accessPanel
                maintenancePanel
            }
            .padding(16)
        }
        .navigationTitle("设置")
        .task { await load() }
        .sheet(item: $editingKey) { item in
            KeyEditor(item: item) { value in
                Task { await save(key: item.key, value: value) }
            }
        }
        .alert("切换到实盘？", isPresented: $confirmReal) {
            Button("取消", role: .cancel) {}
            Button("切到实盘", role: .destructive) { setEnv("REAL", confirm: true) }
        } message: {
            Text("实盘用的是真钱。需要已设置 6 位交易密码 MOO_TRADE_PWD，且当前无未平仓持仓；"
                 + "切换后要点「重启调度器」才生效。首次实盘务必只放小额。")
        }
        .alert("重置交易统计？", isPresented: $confirmReset) {
            Button("取消", role: .cancel) {}
            Button("重置", role: .destructive) {
                Task {
                    try? await APIClient.shared.resetStats()
                    note = "已重置统计基线 — 之后平仓的交易才计入战绩"
                    await poller.refresh()
                }
            }
        } message: {
            Text("战绩/胜率从此刻重新开始统计。历史交易记录本身不会被删除。")
        }
    }

    // ── 交易环境 ──
    private var tradeEnvPanel: some View {
        Panel(title: "交易环境") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Pill(text: ".env: \(env?.envFile ?? "—")",
                         tint: env?.envFile == "REAL" ? .red : .blue)
                    if let live = env?.envLive {
                        Pill(text: "运行中: \(live)", tint: live == "REAL" ? .red : .blue)
                    }
                    if env?.pendingRestart == true {
                        Pill(text: "需重启调度器生效", tint: .orange, icon: "⚠")
                    }
                    Pill(text: "未平仓 \(env?.openPositions ?? 0)")
                    Spacer()
                }
                HStack {
                    Button("切到模拟盘") { setEnv("SIMULATE") }
                        .disabled(busy || env?.envFile == "SIMULATE")
                    Button("切到实盘 💵") { confirmReal = true }
                        .disabled(busy || env?.envFile == "REAL")
                    if env?.pendingRestart == true {
                        Button("重启调度器") { poller.scheduler("restart") }
                            .disabled(poller.schedulerBusy)
                    }
                }
            }
        }
    }

    // ── 预算 ──
    private var budgetPanel: some View {
        Panel(title: "预算") {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("当前预算").frame(width: 76, alignment: .leading)
                    TextField("USD", text: $budgetText)
                        .frame(width: 110)
                        .onSubmit { applyBudget() }
                    Button("应用") { applyBudget() }.disabled(busy)
                    Spacer()
                }
                Divider()
                HStack(spacing: 8) {
                    Text("自动复利").frame(width: 76, alignment: .leading)
                    Pill(text: autoBudget?.enabled == true ? "已开启" : "已关闭",
                         tint: autoBudget?.enabled == true ? .green : .secondary)
                    if autoBudget?.armed == true {
                        Pill(text: "已锁定基准 \(Fmt.money(autoBudget?.seed, decimals: 0))",
                             tint: .blue)
                    }
                    if let target = autoBudget?.target {
                        Pill(text: "目标 \(Fmt.money(target, decimals: 0))")
                    }
                    if let p = autoBudget?.profitSinceArm {
                        Pill(text: "累计 \(Fmt.signed(p, decimals: 0))",
                             tint: p >= 0 ? .green : .red)
                    }
                    Spacer()
                }
                HStack {
                    Button(autoBudget?.enabled == true ? "关闭复利" : "开启复利") {
                        act { try await APIClient.shared.setAutoBudget(
                            enabled: !(autoBudget?.enabled ?? false)) }
                    }
                    Button(autoBudget?.armed == true ? "解除锁定" : "锁定当前为基准") {
                        act { try await APIClient.shared.autoBudgetAction(
                            autoBudget?.armed == true ? "disarm" : "arm") }
                    }
                    Button("重算") { act { try await APIClient.shared.autoBudgetAction("recompute") } }
                }
                .disabled(busy)
            }
        }
    }

    // ── AI 引擎 ──
    private var aiPanel: some View {
        Panel(title: "AI 引擎") {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Pill(text: ai?.provider ?? "—", tint: .purple)
                    Pill(text: ai?.model ?? "—")
                    if ai?.supportsVision == true { Pill(text: "支持看图", tint: .blue, icon: "📐") }
                    Spacer()
                }
                if let providers = ai?.providers {
                    HStack {
                        ForEach(providers) { p in
                            Button(p.label ?? p.id) { switchProvider(p.id) }
                                .disabled(busy || p.hasKey != true || ai?.provider == p.id)
                                .help(p.hasKey == true ? "" : "未配置该引擎的 API Key")
                        }
                        Spacer()
                    }
                }
                if !aiModels.isEmpty {
                    Picker("模型", selection: Binding(
                        get: { ai?.model ?? "" },
                        set: { model in
                            guard let provider = ai?.provider else { return }
                            act { try await APIClient.shared.setAIProvider(provider, model: model) }
                        })) {
                        ForEach(aiModels, id: \.self) { Text($0).tag($0) }
                    }
                    .frame(maxWidth: 420)
                }
            }
        }
    }

    // ── .env keys ──
    private var keysPanel: some View {
        Panel(title: "API Key / 密码（写入 .env）") {
            VStack(spacing: 8) {
                ForEach(keys) { item in
                    HStack(alignment: .top, spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                Text(item.key).fontWeight(.medium)
                                Pill(text: item.isSet == true ? (item.masked ?? "已设置") : "未设置",
                                     tint: item.isSet == true ? .green : .secondary)
                            }
                            Text(item.desc ?? "")
                                .font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        Button("修改") { editingKey = item }
                    }
                    .padding(9)
                    .background(.quaternary.opacity(0.28),
                                in: RoundedRectangle(cornerRadius: 7))
                }
            }
        }
    }

    // ── web 访问 ──
    private var accessPanel: some View {
        Panel(title: "面板访问范围") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Pill(text: access?.mode == "lan" ? "局域网可访问" : "仅本机",
                         tint: access?.mode == "lan" ? .orange : .green)
                    Pill(text: access?.passwordSet == true ? "已设密码" : "无密码",
                         tint: access?.passwordSet == true ? .green : .secondary)
                    if let ip = access?.lanIp, access?.mode == "lan" {
                        Pill(text: "\(ip):\(access?.port ?? 8770)")
                    }
                    if let ts = access?.tailscaleIp {
                        Pill(text: "Tailscale \(ts)", tint: .blue)
                    }
                    Spacer()
                }
                Text("局域网为明文 HTTP，密码/会话在同网段可被嗅探；公共 WiFi 下建议走 Tailscale 地址。开放局域网前必须先设 WEB_PASSWORD。")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Button(access?.mode == "lan" ? "改回仅本机" : "开放到局域网") {
                        let target = access?.mode == "lan" ? "local" : "lan"
                        act {
                            try await APIClient.shared.setWebAccess(mode: target)
                            // The server restarts itself; BackendController re-adopts it.
                        }
                        note = "已切换访问范围 — Web 服务器正在重启，界面会自动重连"
                    }
                    .disabled(busy)
                    Spacer()
                }
            }
        }
    }

    private var maintenancePanel: some View {
        Panel(title: "维护") {
            HStack {
                Button(poller.caffeinate?.on == true ? "关闭防睡眠" : "开启防睡眠") {
                    poller.toggleCaffeinate()
                }
                Button("重置交易统计", role: .destructive) { confirmReset = true }
                Spacer()
            }
        }
    }

    // ── data ──
    private func load() async {
        env = try? await APIClient.shared.tradeEnv()
        autoBudget = try? await APIClient.shared.autoBudget()
        ai = try? await APIClient.shared.aiProvider()
        keys = (try? await APIClient.shared.settingsKeys())?.keys ?? []
        access = try? await APIClient.shared.webAccess()
        if budgetText.isEmpty, let b = poller.status?.budget {
            budgetText = String(format: "%.0f", b)
        }
        if let provider = ai?.provider {
            aiModels = (try? await APIClient.shared.aiModels(provider: provider)) ?? []
        }
    }

    /// Run a mutation, surface its error, then reload everything.
    private func act(_ body: @escaping () async throws -> Void) {
        busy = true
        Task {
            defer { busy = false }
            do { try await body() } catch { note = "操作失败：\(error.localizedDescription)" }
            await load()
            await poller.refresh()
        }
    }

    private func setEnv(_ target: String, confirm: Bool = false) {
        busy = true
        Task {
            defer { busy = false }
            do {
                try await APIClient.shared.setTradeEnv(target, confirm: confirm)
                note = target == "REAL"
                    ? "已切到实盘 — 点「重启调度器」后生效"
                    : "已切回模拟盘 — 点「重启调度器」后生效"
            } catch let e as APIClient.HTTPError {
                note = e.status == 409
                    ? "拒绝切换：还有未平仓持仓，请先全部平仓"
                    : "拒绝切换（HTTP \(e.status)）— 检查是否已设 6 位交易密码 MOO_TRADE_PWD"
            } catch {
                note = "切换失败：\(error.localizedDescription)"
            }
            await load()
        }
    }

    private func applyBudget() {
        guard let v = Double(budgetText.trimmingCharacters(in: .whitespaces)) else {
            note = "预算需要是数字"
            return
        }
        act {
            try await APIClient.shared.setBudget(v)
        }
        note = "预算已更新为 \(Fmt.money(v, decimals: 0))"
    }

    private func switchProvider(_ id: String) {
        busy = true
        Task {
            defer { busy = false }
            let models = (try? await APIClient.shared.aiModels(provider: id)) ?? []
            let model = models.first ?? ""
            do {
                try await APIClient.shared.setAIProvider(id, model: model)
                note = "AI 引擎已切换为 \(id)"
            } catch {
                note = "切换失败 — 该引擎可能未配置 API Key"
            }
            await load()
        }
    }

    private func save(key: String, value: String) async {
        do {
            try await APIClient.shared.setSettingsKey(key, value: value)
            note = key == "WEB_PASSWORD"
                ? "访问密码已更新 — 下次打开面板需要重新登录"
                : "已写入 .env — 重启 bot 后生效"
        } catch {
            note = "写入失败：\(error.localizedDescription)"
        }
        await load()
    }
}

/// Secret entry sheet. The stored value is never fetched or prefilled — the
/// server only ever returns a masked form.
struct KeyEditor: View {
    @Environment(\.dismiss) private var dismiss
    let item: SettingsKeys.Item
    let onSave: (String) -> Void
    @State private var value = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(item.key).font(.headline)
            Text(item.desc ?? "")
                .font(.caption).foregroundStyle(.secondary)
                .frame(width: 360, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            SecureField("留空则清除该项", text: $value)
                .textFieldStyle(.roundedBorder)
                .frame(width: 360)
            HStack {
                Button("取消") { dismiss() }
                Spacer()
                Button("保存") { onSave(value); dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
    }
}
