import Combine
import Foundation

/// Polls /api/status (+ /api/caffeinate) every few seconds while the backend
/// is up. Feeds the menu bar and any native views; also detects the 401 →
/// login-needed edge case (WEB_PASSWORD set).
@MainActor
final class StatusPoller: ObservableObject {
    static let shared = StatusPoller()

    @Published private(set) var status: TraderStatus?
    @Published private(set) var caffeinate: CaffeinateStatus?
    @Published var needsLogin = false

    private var task: Task<Void, Never>?

    private init() {}

    func start() {
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
            status = try await APIClient.shared.status()
            needsLogin = false
        } catch let e as APIClient.HTTPError where e.isAuthRequired {
            needsLogin = true
        } catch {
            // transient network error — BackendController's monitor owns this
        }
        caffeinate = try? await APIClient.shared.caffeinate()
    }
}
