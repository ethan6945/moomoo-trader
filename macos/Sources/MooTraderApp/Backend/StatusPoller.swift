import Combine
import Foundation
import UserNotifications

/// Polls /api/status (+ /api/caffeinate) every few seconds while the backend
/// is up. Feeds the menu bar and any native views; detects the 401 →
/// login-needed edge case (WEB_PASSWORD set); and fires a system notification
/// when new pending approvals appear.
@MainActor
final class StatusPoller: ObservableObject {
    static let shared = StatusPoller()

    @Published private(set) var status: TraderStatus?
    @Published private(set) var caffeinate: CaffeinateStatus?
    @Published private(set) var schedulerPID: Int32?
    @Published var needsLogin = false
    @Published var schedulerBusy = false   // start/stop in flight (can take ~60 s)

    private var task: Task<Void, Never>?
    private var lastPendingCount: Int?

    /// UNUserNotificationCenter requires a real .app bundle — crashes under a
    /// bare `swift run` binary.
    private let canNotify = Bundle.main.bundleIdentifier != nil
        && Bundle.main.bundleURL.pathExtension == "app"

    private init() {}

    func start() {
        if canNotify {
            UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }
        }
        task?.cancel()
        task = Task { await loop() }
    }

    func stop() {
        task?.cancel()
    }

    private func loop() async {
        while !Task.isCancelled {
            guard BackendController.shared.phase == .running else {
                try? await Task.sleep(for: .seconds(2))
                continue
            }
            await refresh()
            try? await Task.sleep(for: .seconds(4))
        }
    }

    func refresh() async {
        do {
            let st = try await APIClient.shared.status()
            status = st
            needsLogin = false
            notifyIfNewApprovals(pending: st.pendingApprovals)
        } catch let e as APIClient.HTTPError where e.isAuthRequired {
            needsLogin = true
        } catch {
            // transient network error — BackendController's monitor owns this
        }
        caffeinate = try? await APIClient.shared.caffeinate()
        if let root = BackendController.shared.repoRoot {
            schedulerPID = Self.alivePID(
                at: root.appendingPathComponent("logs/scheduler.pid"))
        }
    }

    /// PID from a pid file, but only if the process actually exists.
    static func alivePID(at url: URL) -> Int32? {
        guard let text = try? String(contentsOf: url, encoding: .utf8),
              let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              kill(pid, 0) == 0 else { return nil }
        return pid
    }

    private func notifyIfNewApprovals(pending: Int?) {
        defer { lastPendingCount = pending }
        guard canNotify, let pending, pending > 0,
              let last = lastPendingCount, pending > last else { return }
        let content = UNMutableNotificationContent()
        content.title = "Moo Trader"
        content.body = "有 \(pending) 个交易审批待处理"
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString,
                                  content: content, trigger: nil))
    }

    // ── actions used by the menu bar ──────────────────────────────────
    func toggleCaffeinate() {
        let target = !(caffeinate?.on ?? false)
        Task {
            try? await APIClient.shared.setCaffeinate(on: target)
            caffeinate = try? await APIClient.shared.caffeinate()
        }
    }

    /// action ∈ start / stop / restart. start blocks while OpenD launches.
    func scheduler(_ action: String) {
        guard !schedulerBusy else { return }
        schedulerBusy = true
        Task {
            defer { schedulerBusy = false }
            try? await APIClient.shared.scheduler(action)
            await refresh()
        }
    }
}
