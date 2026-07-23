import AppKit
import SwiftUI

/// 设置 — trade env, budget, AI engine, .env keys, web exposure, appearance,
/// support. Backend settings go through the same endpoints the web panel uses,
/// so the Python side stays the single source of truth (and keeps its guards).
struct SettingsPanelView: View {
    @EnvironmentObject var poller: StatusPoller
    @ObservedObject private var l10n = L10n.shared
    @AppStorage(Appearance.key) private var appearance = "system"

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
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                if let note {
                    Text(note)
                        .font(Theme.Font_.body)
                        .foregroundStyle(Theme.text)
                        .padding(Theme.Space.md)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .rowSurface()
                }
                // Two columns for the compact panels — they each used only the
                // left half, so the right half of the window sat empty. Top
                // alignment lets paired panels keep independent heights. Keys
                // stays full-width: its descriptions need the room.
                HStack(alignment: .top, spacing: Theme.Space.md) {
                    tradeEnvPanel
                    budgetPanel
                }
                HStack(alignment: .top, spacing: Theme.Space.md) {
                    aiPanel
                    accessPanel
                }
                keysPanel
                HStack(alignment: .top, spacing: Theme.Space.md) {
                    maintenancePanel
                    appearancePanel
                }
                supportPanel
            }
            .padding(Theme.Space.xl)
        }
        .pageBackground()
        .navigationTitle(L("设置", "Settings"))
        .task { await load() }
        .sheet(item: $editingKey) { item in
            KeyEditor(item: item) { value in
                Task { await save(key: item.key, value: value) }
            }
        }
        .alert(L("切换到实盘？", "Switch to LIVE?"), isPresented: $confirmReal) {
            Button(L("取消", "Cancel"), role: .cancel) {}
            Button(L("切到实盘", "Go LIVE"), role: .destructive) { setEnv("REAL", confirm: true) }
        } message: {
            Text(L("实盘用的是真钱。需要已设置 6 位交易密码 MOO_TRADE_PWD，且当前无未平仓持仓；切换后要点「重启调度器」才生效。首次实盘务必只放小额。", "LIVE uses real money. Requires the 6-digit trade password MOO_TRADE_PWD set and no open positions; takes effect after you restart the scheduler. Start with a tiny amount."))
        }
        .alert(L("重置交易统计？", "Reset trade stats?"), isPresented: $confirmReset) {
            Button(L("取消", "Cancel"), role: .cancel) {}
            Button(L("重置", "Reset"), role: .destructive) {
                Task {
                    try? await APIClient.shared.resetStats()
                    note = L("已重置统计基线 — 之后平仓的交易才计入战绩", "Stats baseline reset — only trades closed from now count")
                    await poller.refresh()
                }
            }
        } message: {
            Text(L("战绩/胜率从此刻重新开始统计。历史交易记录本身不会被删除。", "Record/win-rate restart from now. Trade history itself is not deleted."))
        }
    }

    // ── 交易环境 ──
    private var tradeEnvPanel: some View {
        Panel(title: L("交易环境", "Trade env"), fillHeight: true) {
            VStack(alignment: .leading, spacing: Theme.Space.sm) {
                HStack(spacing: Theme.Space.sm) {
                    Pill(text: ".env: \(env?.envFile ?? "—")",
                         tint: env?.envFile == "REAL" ? Theme.red : Theme.blue)
                    if let live = env?.envLive {
                        Pill(text: L("运行中: ", "live: ") + live, tint: live == "REAL" ? Theme.red : Theme.blue)
                    }
                    if env?.pendingRestart == true {
                        Pill(text: L("需重启调度器生效", "needs scheduler restart"), tint: Theme.amber, icon: "⚠")
                    }
                    Pill(text: L("未平仓 ", "open ") + "\(env?.openPositions ?? 0)")
                    Spacer()
                }
                HStack(spacing: Theme.Space.sm) {
                    Button(L("切到模拟盘", "Go PAPER")) { setEnv("SIMULATE") }
                        .buttonStyle(QuietButtonStyle(tint: Theme.blue))
                        .disabled(busy || env?.envFile == "SIMULATE")
                    Button(L("切到实盘 💵", "Go LIVE 💵")) { confirmReal = true }
                        .buttonStyle(QuietButtonStyle(tint: Theme.red))
                        .disabled(busy || env?.envFile == "REAL")
                    if env?.pendingRestart == true {
                        Button(L("重启调度器", "Restart scheduler")) { poller.scheduler("restart") }
                            .buttonStyle(BrandButtonStyle())
                            .disabled(poller.schedulerBusy)
                    }
                    Spacer()
                }
            }
        }
    }

    // ── 预算 ──
    private var budgetPanel: some View {
        Panel(title: L("预算", "Budget"), fillHeight: true) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                HStack(spacing: Theme.Space.sm) {
                    Text(L("当前预算", "Budget"))
                        .font(Theme.Font_.body).foregroundStyle(Theme.muted)
                        .frame(width: 64, alignment: .leading)
                    TextField("USD", text: $budgetText)
                        .textFieldStyle(.roundedBorder)
                        .font(Theme.Font_.body)
                        .frame(width: 110)
                        .onSubmit { applyBudget() }
                    Button(L("应用", "Apply")) { applyBudget() }
                        .buttonStyle(BrandButtonStyle())
                        .disabled(busy)
                    Spacer()
                }
                Divider()
                HStack(spacing: Theme.Space.sm) {
                    Text(L("自动复利", "Auto-comp"))
                        .font(Theme.Font_.body).foregroundStyle(Theme.muted)
                        .frame(width: 64, alignment: .leading)
                    Pill(text: autoBudget?.enabled == true ? L("已开启", "on") : L("已关闭", "off"),
                         tint: autoBudget?.enabled == true ? Theme.green : Theme.muted)
                    if autoBudget?.armed == true {
                        Pill(text: L("已锁定基准 ", "seed ") + Fmt.money(autoBudget?.seed, decimals: 0),
                             tint: Theme.blue)
                    }
                    if let target = autoBudget?.target {
                        Pill(text: L("目标 ", "target ") + Fmt.money(target, decimals: 0))
                    }
                    if let p = autoBudget?.profitSinceArm {
                        Pill(text: L("累计 ", "P&L ") + Fmt.signed(p, decimals: 0),
                             tint: p >= 0 ? Theme.green : Theme.red)
                    }
                    Spacer()
                }
                HStack {
                    Button(autoBudget?.enabled == true ? L("关闭复利", "Disable") : L("开启复利", "Enable")) {
                        act { try await APIClient.shared.setAutoBudget(
                            enabled: !(autoBudget?.enabled ?? false)) }
                    }
                    .buttonStyle(QuietButtonStyle(
                        tint: autoBudget?.enabled == true ? Theme.muted : Theme.green))
                    Button(autoBudget?.armed == true ? L("解除锁定", "Unlock") : L("锁定当前为基准", "Lock as seed")) {
                        act { try await APIClient.shared.autoBudgetAction(
                            autoBudget?.armed == true ? "disarm" : "arm") }
                    }
                    .buttonStyle(QuietButtonStyle(tint: Theme.blue))
                    Button(L("重算", "Recompute")) { act { try await APIClient.shared.autoBudgetAction("recompute") } }
                        .buttonStyle(QuietButtonStyle())
                    Spacer()
                }
                .disabled(busy)
            }
        }
    }

    // ── AI 引擎 ──
    // Single engine (DeepSeek) — no provider picker. Just the fixed engine and
    // its live-fetched model list.
    private var aiPanel: some View {
        Panel(title: L("AI 引擎", "AI engine"), fillHeight: true) {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                HStack(spacing: Theme.Space.sm) {
                    Pill(text: "DeepSeek", tint: Theme.purple)
                    Pill(text: ai?.model ?? "—")
                    Spacer()
                }
                Text(L("所有需要 AI 的功能（盯盘 / 情绪 / 退出 / 优化 / 入场验证）都用 DeepSeek。", "Every AI feature (monitor / sentiment / exit / optimize / entry check) uses DeepSeek."))
                    .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                if !aiModels.isEmpty {
                    // Own label + labelsHidden: a Picker's built-in label is
                    // laid out as one unit with the control, which floats the
                    // pair off the leading edge the rows above line up on.
                    HStack(spacing: Theme.Space.sm) {
                        Text(L("模型", "Model"))
                            .font(Theme.Font_.label)
                            .foregroundStyle(Theme.muted)
                        Picker(L("模型", "Model"), selection: Binding(
                            get: { ai?.model ?? "" },
                            set: { model in
                                guard let provider = ai?.provider else { return }
                                act { try await APIClient.shared.setAIProvider(provider, model: model) }
                            })) {
                            ForEach(aiModels, id: \.self) { Text($0).tag($0) }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 280)
                        Spacer()
                    }
                }
            }
        }
    }

    // ── .env keys ──
    private var keysPanel: some View {
        Panel(title: L("API Key / 密码（写入 .env）", "API keys / passwords (.env)")) {
            VStack(spacing: Theme.Space.sm) {
                ForEach(keys) { item in
                    HStack(alignment: .top, spacing: Theme.Space.md) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: Theme.Space.sm) {
                                Text(item.key).font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.strong)
                                Pill(text: item.isSet == true ? (item.masked ?? L("已设置", "set")) : L("未设置", "unset"),
                                     tint: item.isSet == true ? Theme.green : Theme.muted)
                            }
                            Text(l10n.keyDesc(item.key, fallback: item.desc ?? ""))
                                .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 8)
                        Button(L("修改", "Edit")) { editingKey = item }
                            .buttonStyle(QuietButtonStyle(tint: Theme.blue))
                    }
                    .padding(Theme.Space.md)
                    .rowSurface()
                }
            }
        }
    }

    // ── web 访问 ──
    private var accessPanel: some View {
        Panel(title: L("面板访问范围", "Panel access"), fillHeight: true) {
            VStack(alignment: .leading, spacing: Theme.Space.sm) {
                HStack(spacing: Theme.Space.sm) {
                    Pill(text: access?.mode == "lan" ? L("局域网可访问", "LAN reachable") : L("仅本机", "Local only"),
                         tint: access?.mode == "lan" ? Theme.amber : Theme.green)
                    Pill(text: access?.passwordSet == true ? L("已设密码", "password set") : L("无密码", "no password"),
                         tint: access?.passwordSet == true ? Theme.green : Theme.muted)
                    if let ip = access?.lanIp, access?.mode == "lan" {
                        Pill(text: "\(ip):\(access?.port ?? 8770)")
                    }
                    if let ts = access?.tailscaleIp {
                        Pill(text: "Tailscale \(ts)", tint: Theme.blue)
                    }
                    Spacer()
                }
                Text(L("局域网为明文 HTTP，密码/会话在同网段可被嗅探；公共 WiFi 下建议走 Tailscale 地址。开放局域网前必须先设 WEB_PASSWORD。", "LAN is plain HTTP — password/session can be sniffed on the same network; prefer the Tailscale address on public WiFi. Set WEB_PASSWORD before exposing to LAN."))
                    .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: Theme.Space.sm) {
                    Button(access?.mode == "lan" ? L("改回仅本机", "Back to local") : L("开放到局域网", "Expose to LAN")) {
                        let target = access?.mode == "lan" ? "local" : "lan"
                        act {
                            try await APIClient.shared.setWebAccess(mode: target)
                            // The server restarts itself; BackendController re-adopts it.
                        }
                        note = L("已切换访问范围 — Web 服务器正在重启，界面会自动重连", "Access scope changed — web server restarting, the UI will reconnect")
                    }
                    .buttonStyle(QuietButtonStyle(
                        tint: access?.mode == "lan" ? Theme.muted : Theme.amber))
                    .disabled(busy)
                    // The password shown here lives in .env under WEB_PASSWORD;
                    // edit it in place instead of hunting for it in the key list.
                    Button(access?.passwordSet == true ? L("修改密码", "Change password") : L("设置密码", "Set password")) {
                        editingKey = passwordItem
                    }
                    .buttonStyle(QuietButtonStyle(tint: Theme.blue))
                    // The full dashboard (backtest, sector heatmap, everything)
                    // lives in the browser now — open it in the default browser
                    // rather than embedding a WKWebView.
                    Button(L("在浏览器打开完整面板", "Open web panel in browser")) {
                        open(BackendController.shared.baseURL.absoluteString)
                    }
                    .buttonStyle(QuietButtonStyle(tint: Theme.blueUp))
                    Spacer()
                }
            }
        }
    }

    private var maintenancePanel: some View {
        Panel(title: L("维护", "Maintenance"), fillHeight: true) {
            HStack(spacing: Theme.Space.sm) {
                Button(poller.caffeinate?.on == true ? L("关闭防睡眠", "Disable keep-awake") : L("开启防睡眠", "Keep awake")) {
                    poller.toggleCaffeinate()
                }
                .buttonStyle(QuietButtonStyle(
                    tint: poller.caffeinate?.on == true ? Theme.amber : Theme.muted))
                Button(L("重置交易统计", "Reset stats")) { confirmReset = true }
                    .buttonStyle(QuietButtonStyle(tint: Theme.red))
                Spacer()
            }
        }
    }

    // ── 外观 ──
    // Native adds a "跟随系统" option the web theme toggle doesn't have; it drives
    // NSApp.appearance so native colours and the WKWebView both follow it.
    private var appearancePanel: some View {
        Panel(title: L("外观 & 语言", "Appearance & language"), fillHeight: true) {
            VStack(alignment: .leading, spacing: Theme.Space.sm) {
                HStack(spacing: Theme.Space.sm) {
                    Text(L("主题", "Theme"))
                        .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                        .frame(width: 40, alignment: .leading)
                    Picker("", selection: $appearance) {
                        Text(L("跟随系统", "System")).tag("system")
                        Text(L("深色", "Dark")).tag("dark")
                        Text(L("浅色", "Light")).tag("light")
                    }
                    .pickerStyle(.segmented).labelsHidden()
                    .onChange(of: appearance) { _, mode in Appearance.apply(mode) }
                }
                HStack(spacing: Theme.Space.sm) {
                    Text(L("语言", "Lang"))
                        .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                        .frame(width: 40, alignment: .leading)
                    Picker("", selection: $l10n.lang) {
                        Text("中文").tag("zh")
                        Text("English").tag("en")
                    }
                    .pickerStyle(.segmented).labelsHidden()
                }
                Text(L("界面配色与语言。立即生效，记在本机。", "Theme and language. Applies immediately, saved on this Mac."))
                    .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // ── 支持 ──
    private var supportPanel: some View {
        Panel(title: L("支持", "Support")) {
            HStack(alignment: .center, spacing: Theme.Space.md) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(L("支持这个项目", "Support this project"))
                        .font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.strong)
                    Text(L("本软件免费开源。如果它帮到了你，欢迎请作者喝杯咖啡 ☕", "This app is free & open source. If it helped you, buy the author a coffee ☕"))
                        .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                }
                Spacer(minLength: Theme.Space.sm)
                Button("☕ Buy me a coffee") {
                    open("https://buymeacoffee.com/ethan6945")
                }
                .buttonStyle(QuietButtonStyle(tint: Theme.amber))
                Button("⭐ GitHub") {
                    open("https://github.com/ethan6945/moomoo-trader")
                }
                .buttonStyle(QuietButtonStyle(tint: Theme.blue))
            }
        }
    }

    private func open(_ url: String) {
        if let u = URL(string: url) { NSWorkspace.shared.open(u) }
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
            do { try await body() } catch { note = L("操作失败：", "Action failed: ") + error.localizedDescription }
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
                    ? L("已切到实盘 — 点「重启调度器」后生效", "Switched to LIVE — restart the scheduler to apply")
                    : L("已切回模拟盘 — 点「重启调度器」后生效", "Switched to PAPER — restart the scheduler to apply")
            } catch let e as APIClient.HTTPError {
                note = e.status == 409
                    ? L("拒绝切换：还有未平仓持仓，请先全部平仓", "Refused: open positions exist — close them all first")
                    : L("拒绝切换（HTTP \(e.status)）— 检查是否已设 6 位交易密码 MOO_TRADE_PWD", "Refused (HTTP \(e.status)) — check the 6-digit trade password MOO_TRADE_PWD is set")
            } catch {
                note = L("切换失败：", "Switch failed: ") + error.localizedDescription
            }
            await load()
        }
    }

    private func applyBudget() {
        guard let v = Double(budgetText.trimmingCharacters(in: .whitespaces)) else {
            note = L("预算需要是数字", "Budget must be a number")
            return
        }
        act {
            try await APIClient.shared.setBudget(v)
        }
        note = L("预算已更新为 ", "Budget set to ") + Fmt.money(v, decimals: 0)
    }

    /// The WEB_PASSWORD row for the editor — reuse the loaded item (keeps its
    /// description) or synthesise one if the key list hasn't arrived yet.
    private var passwordItem: SettingsKeys.Item {
        keys.first { $0.key == "WEB_PASSWORD" }
            ?? SettingsKeys.Item(key: "WEB_PASSWORD",
                                 desc: L("网页访问密码 — 手机/局域网打开面板要先登录(本机也是)。留空=清除密码，面板将不再需要登录。", "Web access password — phone/LAN must log in (local too). Empty = clear it, no login needed."),
                                 masked: nil, isSet: access?.passwordSet)
    }

    private func save(key: String, value: String) async {
        do {
            try await APIClient.shared.setSettingsKey(key, value: value)
            note = key == "WEB_PASSWORD"
                ? L("访问密码已更新 — 下次打开面板需要重新登录", "Access password updated — you'll need to log in again next time")
                : L("已写入 .env — 重启 bot 后生效", "Written to .env — takes effect after a bot restart")
        } catch {
            note = L("写入失败：", "Write failed: ") + error.localizedDescription
        }
        await load()
    }
}

/// Secret entry sheet. The stored value is never fetched or prefilled — the
/// server only ever returns a masked form.
struct KeyEditor: View {
    @ObservedObject private var l10n = L10n.shared
    @Environment(\.dismiss) private var dismiss
    let item: SettingsKeys.Item
    let onSave: (String) -> Void
    @State private var value = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(item.key).font(.headline)
            Text(item.desc ?? "")
                .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                .frame(width: 360, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            SecureField(L("留空则清除该项", "Empty to clear"), text: $value)
                .textFieldStyle(.roundedBorder)
                .frame(width: 360)
            HStack {
                Button(L("取消", "Cancel")) { dismiss() }
                Spacer()
                Button(L("保存", "Save")) { onSave(value); dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
    }
}
