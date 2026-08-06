import AppKit
import SwiftUI

/// Menu-bar menu (replaces the rumps icon). Unlike rumps it is always present
/// while this app runs, so it also shows "scheduler stopped" instead of
/// disappearing. All state comes from StatusPoller.
struct MenuBarView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller
    @ObservedObject private var l10n = L10n.shared
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        // ── status (disabled = plain text rows) ──
        Group {
            // Same emoji dots as the OpenD row below, so the two status lines
            // carry equal visual weight (●/○ text glyphs rendered much smaller).
            if let pid = poller.schedulerPID {
                Text("🟢 " + L("调度器运行中", "Scheduler running") + " (PID \(String(pid)))")
            } else if poller.schedulerBusy {
                Text("🟡 " + L("调度器启动/停止中", "Scheduler starting/stopping"))
            } else {
                Text("⚪️ " + L("调度器已停止", "Scheduler stopped"))
            }
            if let s = poller.status, let label = s.opendLabel {
                Text("\(opendDot(s.opendStatus)) OpenD: \(l10n.opend(label))")
            }
            if !poller.pending.isEmpty {
                Text("⚠ " + L("\(poller.pending.count) 个审批待处理", "\(poller.pending.count) approval(s) pending"))
            }
        }

        Divider()

        Button(caffeinateTitle) { poller.toggleCaffeinate() }
        Button("🌐 " + L("打开仪表盘", "Open dashboard")) { showDashboard() }

        Divider()

        if poller.schedulerPID != nil {
            Button("■ " + L("停止调度器", "Stop scheduler")) { confirmStopScheduler() }
                .disabled(poller.schedulerBusy)
        } else {
            Button("▶ " + L("启动调度器", "Start scheduler")) { poller.startWithPreflight() }
                .disabled(poller.schedulerBusy || backend.phase != .running)
        }

        Divider()

        Button(L("退出 App（交易继续）", "Quit app (trading continues)")) {
            NSApplication.shared.terminate(nil)
        }
        Button("⏻ " + L("全部退出（停止交易 + OpenD + Web）", "Exit everything (stop trading + OpenD + Web)")) { confirmExitEverything() }
    }

    private var caffeinateTitle: String {
        guard let c = poller.caffeinate else { return "☕ " + L("防睡眠 …", "Keep awake …") }
        if c.on {
            return "☕ " + L("防睡眠: 开", "Keep awake: on") + ((c.lid ?? false) ? L("（含合盖）", " (incl. lid closed)") : L("（屏幕可熄灭）", " (screen may sleep)"))
        }
        return "☕ " + L("防睡眠: 关 — 点击开启", "Keep awake: off — click to enable")
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
        alert.messageText = L("停止调度器？", "Stop the scheduler?")
        alert.informativeText = L("将停止后台交易调度（持仓不动，只是不再下新单/管理），并同时关闭 OpenD。", "Stops the background trading scheduler (positions untouched — just no new orders/management) and closes OpenD.")
        alert.addButton(withTitle: L("停止", "Stop"))
        alert.addButton(withTitle: L("取消", "Cancel"))
        NSApplication.shared.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            poller.scheduler("stop")
        }
    }

    private func confirmExitEverything() {
        let alert = NSAlert()
        alert.messageText = L("全部退出？", "Exit everything?")
        alert.informativeText = L("将停止 OpenD、交易调度和 Web 服务器，然后退出本应用。", "Stops OpenD, the scheduler and the web server, then quits this app.")
        alert.addButton(withTitle: L("全部退出", "Exit everything"))
        alert.addButton(withTitle: L("取消", "Cancel"))
        NSApplication.shared.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            Task { await backend.exitEverything() }
        }
    }
}
