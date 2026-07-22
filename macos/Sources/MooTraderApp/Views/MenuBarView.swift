import AppKit
import SwiftUI

/// Menu-bar menu (replaces the rumps icon). Unlike rumps it is always present
/// while this app runs, so it also shows "scheduler stopped" instead of
/// disappearing. All state comes from StatusPoller.
struct MenuBarView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        // ── status (disabled = plain text rows) ──
        Group {
            // Same emoji dots as the OpenD row below, so the two status lines
            // carry equal visual weight (●/○ text glyphs rendered much smaller).
            if let pid = poller.schedulerPID {
                Text("🟢 Scheduler 运行中 (PID \(String(pid)))")
            } else if poller.schedulerBusy {
                Text("🟡 Scheduler 启动/停止中")
            } else {
                Text("⚪️ Scheduler 已停止")
            }
            if let s = poller.status, let label = s.opendLabel {
                Text("\(opendDot(s.opendStatus)) OpenD: \(label)")
            }
            if !poller.pending.isEmpty {
                Text("⚠ \(poller.pending.count) 个审批待处理")
            }
        }

        Divider()

        Button(caffeinateTitle) { poller.toggleCaffeinate() }
        Button("🌐 打开仪表盘") { showDashboard() }

        Divider()

        if poller.schedulerPID != nil {
            Button("■ 停止 Scheduler") { confirmStopScheduler() }
                .disabled(poller.schedulerBusy)
        } else {
            Button("▶ 启动 Scheduler") { poller.scheduler("start") }
                .disabled(poller.schedulerBusy || backend.phase != .running)
        }

        Divider()

        Button("退出 App（交易继续）") {
            NSApplication.shared.terminate(nil)
        }
        Button("⏻ 全部退出（停止交易 + OpenD + Web）") { confirmExitEverything() }
    }

    private var caffeinateTitle: String {
        guard let c = poller.caffeinate else { return "☕ 防睡眠 …" }
        if c.on {
            return "☕ 防睡眠: 开" + ((c.lid ?? false) ? "（含合盖）" : "（屏幕可熄灭）")
        }
        return "☕ 防睡眠: 关 — 点击开启"
    }

    private func opendDot(_ status: String?) -> String {
        switch status {
        case "green": return "🟢"
        case "blue": return "🔵"
        case "yellow": return "🟡"
        case "red": return "🔴"
        default: return "⚪️"
        }
    }

    private func showDashboard() {
        openWindow(id: "main")
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    private func confirmStopScheduler() {
        let alert = NSAlert()
        alert.messageText = "停止 Scheduler？"
        alert.informativeText = "将停止后台交易调度（持仓不动，只是不再下新单/管理），并同时关闭 OpenD。"
        alert.addButton(withTitle: "停止")
        alert.addButton(withTitle: "取消")
        NSApplication.shared.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            poller.scheduler("stop")
        }
    }

    private func confirmExitEverything() {
        let alert = NSAlert()
        alert.messageText = "全部退出？"
        alert.informativeText = "将停止 OpenD、交易调度和 Web 服务器，然后退出本应用。"
        alert.addButton(withTitle: "全部退出")
        alert.addButton(withTitle: "取消")
        NSApplication.shared.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            Task { await backend.exitEverything() }
        }
    }
}
