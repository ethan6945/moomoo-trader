import Combine
import Foundation
import UserNotifications

/// Polls the backend every few seconds while it is up and publishes the state
/// every native view reads. Also detects the 401 → login-needed edge case
/// (WEB_PASSWORD set) and notifies when new approvals appear.
@MainActor
final class StatusPoller: ObservableObject {
    static let shared = StatusPoller()

    @Published private(set) var status: TraderStatus?
    @Published private(set) var caffeinate: CaffeinateStatus?
    @Published private(set) var approvals: [Approval] = []
    @Published private(set) var activity: [String] = []
    @Published private(set) var sectors: SectorOverview?
    @Published private(set) var closed: [ClosedTrade] = []   // for the overview sparklines
    @Published private(set) var schedulerPID: Int32?
    @Published var needsLogin = false
    @Published var schedulerBusy = false   // start/stop in flight (can take ~60 s)
    /// Non-nil while the preflight dialog should be on screen. Lives here rather
    /// than in a view so EVERY start path (overview button, menu bar) shares one
    /// guarded route — a second entry point that skipped the check would defeat
    /// the whole point of having it.
    @Published var preflight: PreflightResult?
    @Published var preflightBusy = false

    var pending: [Approval] { approvals.filter(\.isPending) }

    private var task: Task<Void, Never>?
    private var lastPendingCount: Int?

    /// UNUserNotificationCenter requires a real .app bundle — it traps in a
    /// bare `swift run` binary.
    private let canNotify = Bundle.main.bundleURL.pathExtension == "app"

    private init() {}

    func start() {
        if canNotify {
            UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }
        }
        task?.cancel()
        task = Task { await loop() }
    }

    func stop() { task?.cancel() }

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
            status = try await APIClient.shared.status()
            needsLogin = false
        } catch let e as APIClient.HTTPError where e.isAuthRequired {
            needsLogin = true
            return                       // nothing else will succeed either
        } catch {
            return                       // transient — BackendController owns recovery
        }
        caffeinate = try? await APIClient.shared.caffeinate()
        if let items = try? await APIClient.shared.approvals() {
            approvals = items
            notifyIfNewApprovals(items.filter(\.isPending).count)
        }
        activity = (try? await APIClient.shared.activityLog(lines: 40)) ?? activity
        closed = (try? await APIClient.shared.closedTrades(limit: 400)) ?? closed
        // Server caches sectors ~60 s, so polling every cycle is cheap; keep the
        // last good value on a transient miss rather than blanking the panel.
        if let s = try? await APIClient.shared.sectors() { sectors = s }
        if let work = BackendController.shared.workDir {
            schedulerPID = await Self.alivePID(at: work.appendingPathComponent("logs/scheduler.pid"))
        }
    }

    /// PID from a pid file, but only if that process actually exists.
    /// Detached: reading inside the repo can block on the macOS privacy
    /// prompt, and blocking the main actor would freeze the whole UI.
    static func alivePID(at url: URL) async -> Int32? {
        await Task.detached {
            guard let text = try? String(contentsOf: url, encoding: .utf8),
                  let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)),
                  kill(pid, 0) == 0 else { return nil }
            return pid
        }.value
    }

    private func notifyIfNewApprovals(_ count: Int) {
        defer { lastPendingCount = count }
        guard canNotify, count > 0, let last = lastPendingCount, count > last else { return }
        let content = UNMutableNotificationContent()
        content.title = "Moo Trader"
        content.body = L("有 \(count) 个审批待处理", "\(count) approval(s) pending")
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    // ── actions ───────────────────────────────────────────────────────
    func toggleCaffeinate() {
        let target = !(caffeinate?.on ?? false)
        Task {
            try? await APIClient.shared.setCaffeinate(on: target)
            caffeinate = try? await APIClient.shared.caffeinate()
        }
    }

    /// action ∈ start / stop / restart. start blocks while OpenD launches.
    /// Start, but check readiness first. A perfectly clean install starts with
    /// no dialog at all — friction that only ever says "everything is fine" is
    /// friction people learn to click through, which would blunt the dialog for
    /// the times it has something to say. Fails OPEN: if the check itself errors
    /// we start anyway rather than stranding the user behind a broken probe.
    func startWithPreflight() {
        guard !preflightBusy, !schedulerBusy else { return }
        preflightBusy = true
        Task {
            defer { preflightBusy = false }
            guard let r = try? await APIClient.shared.preflight(fresh: true) else {
                scheduler("start"); return
            }
            if r.canStart && r.blockers == 0 && r.degraded == 0 {
                scheduler("start")
            } else {
                preflight = r
            }
        }
    }

    func scheduler(_ action: String) {
        guard !schedulerBusy else { return }
        schedulerBusy = true
        Task {
            defer { schedulerBusy = false }
            try? await APIClient.shared.scheduler(action)
            await refresh()
        }
    }

    func resolveApproval(_ id: String, action: String) {
        Task {
            try? await APIClient.shared.resolveApproval(id, action: action)
            if let items = try? await APIClient.shared.approvals() { approvals = items }
        }
    }
}
