import AppKit
import Combine
import Foundation

/// Owns the Flask web server's lifecycle — the ONE process the native shell
/// manages itself. Scheduler / OpenD / signal reporter / keep-awake all stay
/// behind the Flask API (PID files in logs/), exactly as before.
///
/// Launch semantics (mirrors start-web.command + src/desktop_app.py):
///   • If something already answers on the port → adopt it, never respawn.
///   • Else spawn `nohup .venv/bin/python web/server.py` detached via bash,
///     writing logs/web.pid — so quitting this app never kills the backend.
///   • `/api/web-access` makes the server restart itself: on connection loss
///     we quietly re-probe for a while and re-adopt before declaring failure.
@MainActor
final class BackendController: ObservableObject {
    static let shared = BackendController()

    enum Phase: Equatable {
        case starting
        case running
        case failed(String)
    }

    @Published private(set) var phase: Phase = .starting

    let repoRoot: URL
    let port: Int
    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    private var monitorTask: Task<Void, Never>?
    private var restartAttempted = false

    private init() {
        repoRoot = Self.locateRepoRoot()
        port = Self.readPort(repoRoot: repoRoot)
        APIClient.shared.baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    // ── repo discovery ────────────────────────────────────────────────
    /// Priority: user-set path (Settings) → walk up from the .app bundle
    /// (dist/MooTrader.app lives inside macos/, two levels below the repo)
    /// → walk up from CWD (covers `swift run` in macos/).
    private static func locateRepoRoot() -> URL {
        func isRepo(_ url: URL) -> Bool {
            FileManager.default.fileExists(
                atPath: url.appendingPathComponent("web/server.py").path)
        }
        if let saved = UserDefaults.standard.string(forKey: "repoPath") {
            let url = URL(fileURLWithPath: saved)
            if isRepo(url) { return url }
        }
        for start in [Bundle.main.bundleURL,
                      URL(fileURLWithPath: FileManager.default.currentDirectoryPath)] {
            var url = start
            for _ in 0..<8 {
                if isRepo(url) { return url }
                url.deleteLastPathComponent()
            }
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    }

    private static func readPort(repoRoot: URL) -> Int {
        if let env = ProcessInfo.processInfo.environment["WEB_PORT"], let p = Int(env) {
            return p
        }
        // Parse WEB_PORT from the repo's .env (dotenv format, no quotes expected).
        if let text = try? String(contentsOf: repoRoot.appendingPathComponent(".env"),
                                  encoding: .utf8) {
            for line in text.split(separator: "\n") {
                let t = line.trimmingCharacters(in: .whitespaces)
                if t.hasPrefix("WEB_PORT="), let p = Int(t.dropFirst("WEB_PORT=".count)
                        .trimmingCharacters(in: .whitespaces)) {
                    return p
                }
            }
        }
        return 8770
    }

    var repoLooksValid: Bool {
        FileManager.default.fileExists(
            atPath: repoRoot.appendingPathComponent("web/server.py").path)
    }

    // ── lifecycle ─────────────────────────────────────────────────────
    func start() {
        monitorTask?.cancel()
        monitorTask = Task { await run() }
    }

    private func run() async {
        phase = .starting
        guard repoLooksValid else {
            phase = .failed("找不到 moo-trader 仓库（web/server.py）。请把 MooTrader.app 放在仓库内，或在设置中指定仓库路径。")
            return
        }
        if await serverResponds(timeout: 0.8) {
            phase = .running
        } else {
            spawnServer()
            if await waitForServer(seconds: 25) {
                phase = .running
            } else {
                phase = .failed("Web 服务器 25 秒内未就绪 — 查看 logs/web.log")
                return
            }
        }
        await monitorLoop()
    }

    /// Detached spawn identical to start-web.command (minus the fresh-kill):
    /// nohup + background + pid file. bash exits immediately; the python
    /// process survives this app quitting.
    private func spawnServer() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c",
            "mkdir -p logs && nohup .venv/bin/python web/server.py >> logs/web.log 2>&1 & echo $! > logs/web.pid"]
        p.currentDirectoryURL = repoRoot
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
    }

    /// Any HTTP answer (including 401/redirect) proves the server is up.
    /// /login is public and import-light.
    private func serverResponds(timeout: TimeInterval) async -> Bool {
        var req = URLRequest(url: baseURL.appendingPathComponent("login"),
                             timeoutInterval: timeout)
        req.httpMethod = "HEAD"
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            return resp is HTTPURLResponse
        } catch {
            return false
        }
    }

    private func waitForServer(seconds: Int) async -> Bool {
        for _ in 0..<seconds {
            if Task.isCancelled { return false }
            if await serverResponds(timeout: 1.0) { return true }
            try? await Task.sleep(for: .seconds(1))
        }
        return false
    }

    /// Health loop. On loss: re-probe ~10 s (web-access self-restart shows up
    /// here as a brief gap → re-adopt silently), then one auto-respawn, then
    /// give up with a banner.
    private func monitorLoop() async {
        var misses = 0
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(3))
            if await serverResponds(timeout: 2.0) {
                misses = 0
                restartAttempted = false
                if phase != .running { phase = .running }
                continue
            }
            misses += 1
            guard misses >= 3 else { continue }   // ~10 s of grace
            if !restartAttempted {
                restartAttempted = true
                phase = .starting
                spawnServer()
                if await waitForServer(seconds: 25) {
                    phase = .running
                    misses = 0
                    continue
                }
            }
            phase = .failed("Web 服务器已停止且自动重启失败 — 查看 logs/web.log")
            return
        }
    }

    // ── actions ───────────────────────────────────────────────────────
    /// "全部退出": stop OpenD + scheduler + web server via the API, then quit.
    func exitEverything() async {
        try? await APIClient.shared.exitEverything()
        monitorTask?.cancel()
        try? await Task.sleep(for: .milliseconds(800))
        NSApplication.shared.terminate(nil)
    }
}
