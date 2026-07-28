import AppKit
import Combine
import Foundation

/// Owns the Flask web server's lifecycle — the ONE process the native shell
/// manages itself. Scheduler / OpenD / signal reporter / keep-awake all stay
/// behind the Flask API (PID files in logs/), exactly as before.
///
/// Launch semantics (mirrors start-web.command + src/desktop_app.py):
///   • If something already answers on the port → adopt it, never respawn.
///   • Else spawn the backend detached via bash, writing logs/web.pid — so
///     quitting this app never kills the backend.
///   • `/api/web-access` makes the server restart itself: on connection loss
///     we quietly re-probe for a while and re-adopt before declaring failure.
///
/// Two ways the backend can exist, resolved in `discover()`:
///   • Packaged — a frozen binary inside this .app. Needs no repo, no venv and
///     no Python on the machine, and its state lives in Application Support.
///   • Dev — a repo checkout with a .venv, found by walking up from the bundle
///     or the CWD. Unchanged, so `swift run` still works.
///
/// IMPORTANT: no disk IO in init. In dev the repo usually lives under ~/Desktop,
/// and the app's first file access there blocks on the macOS privacy consent
/// prompt (TCC) — doing that during SwiftUI scene init hangs the whole app.
/// All discovery happens off the main thread in run(). (Packaged installs skip
/// that hazard entirely: Application Support is not TCC-protected.)
@MainActor
final class BackendController: ObservableObject {
    static let shared = BackendController()

    enum Phase: Equatable {
        case starting(String)     // with the message to show while we wait
        case running
        case failed(String)

        var isStarting: Bool { if case .starting = self { return true }; return false }
    }

    /// How to start the backend, and therefore which of the two layouts we're in.
    enum Launch {
        /// Frozen binary inside this .app (Contents/Resources/backend/).
        case bundled(URL)
        /// Repo checkout — run web/server.py under the repo's own venv.
        case repo
    }

    @Published private(set) var phase: Phase = .starting(L("引擎启动中…", "Starting engine…"))
    /// Where .env, logs/ and data/ live: the repo root in dev, the app home
    /// (~/Library/Application Support/MooMooTrader) in a packaged install.
    @Published private(set) var workDir: URL?
    @Published private(set) var port = 8770

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)")! }

    private var launch: Launch?
    private var monitorTask: Task<Void, Never>?
    private var restartAttempted = false

    private init() {}

    // ── discovery (runs detached — in dev it may block on the TCC prompt) ──
    /// Packaged build wins: if this .app carries a frozen backend, use it and
    /// ignore any repo that happens to be lying around, so a developer's
    /// checkout can never shadow what a user actually installed.
    ///
    /// Otherwise fall back to repo discovery: user-set path (Settings) → walk
    /// up from the .app bundle (dist/MooTrader.app lives inside macos/, two
    /// levels below the repo) → walk up from CWD (covers `swift run` in macos/).
    private nonisolated static func discover() -> (launch: Launch?, work: URL?, port: Int) {
        let fm = FileManager.default
        var launch: Launch?
        var work: URL?

        if let backend = Bundle.main.resourceURL?
                .appendingPathComponent("backend/MooTraderBackend"),
           fm.isExecutableFile(atPath: backend.path) {
            launch = .bundled(backend)
            work = appHome()
        } else {
            func isRepo(_ url: URL) -> Bool {
                fm.fileExists(atPath: url.appendingPathComponent("web/server.py").path)
            }
            if let saved = UserDefaults.standard.string(forKey: "repoPath"),
               isRepo(URL(fileURLWithPath: saved)) {
                work = URL(fileURLWithPath: saved)
            } else {
                outer: for start in [Bundle.main.bundleURL,
                              URL(fileURLWithPath: fm.currentDirectoryPath)] {
                    var url = start
                    for _ in 0..<8 {
                        if isRepo(url) { work = url; break outer }
                        url.deleteLastPathComponent()
                    }
                }
            }
            if work != nil { launch = .repo }
        }

        var port = 8770
        if let env = ProcessInfo.processInfo.environment["WEB_PORT"], let p = Int(env) {
            port = p
        } else if let work,
                  let text = try? String(contentsOf: work.appendingPathComponent(".env"),
                                         encoding: .utf8) {
            // dotenv format, no quotes expected. A packaged first run has no
            // .env at all yet — the default stands until the wizard writes one.
            for line in text.split(separator: "\n") {
                let t = line.trimmingCharacters(in: .whitespaces)
                if t.hasPrefix("WEB_PORT="), let p = Int(t.dropFirst("WEB_PORT=".count)
                        .trimmingCharacters(in: .whitespaces)) {
                    port = p
                    break
                }
            }
        }
        return (launch, work, port)
    }

    /// Mirrors src/config.py's _app_home() so both sides land on the same
    /// directory without having to agree on it over any channel.
    private nonisolated static func appHome() -> URL {
        let env = ProcessInfo.processInfo.environment["MMT_HOME"]?
            .trimmingCharacters(in: .whitespaces) ?? ""
        if !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MooMooTrader")
    }

    // ── lifecycle ─────────────────────────────────────────────────────
    func start() {
        monitorTask?.cancel()
        monitorTask = Task { await run() }
    }

    private func run() async {
        phase = .starting(L("引擎启动中…", "Starting engine…"))
        // Discovery touches the repo, which usually sits under ~/Desktop — a
        // TCC-protected location. The read BLOCKS until the user answers the
        // privacy prompt (which reappears after every rebuild, since ad-hoc
        // signing changes the app's identity). Detached so the UI stays live,
        // plus a watchdog that explains the wait instead of spinning silently.
        let discovery = Task.detached { Self.discover() }
        let watchdog = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard let self, self.phase.isStarting else { return }
            self.phase = .starting(L("等待 macOS 文件访问授权 —\n请在系统弹窗中点『允许』", "Waiting for macOS file-access permission —\nclick “Allow” in the system prompt"))
        }
        let found = await discovery.value
        watchdog.cancel()
        guard let foundLaunch = found.launch, let work = found.work else {
            // Only reachable in a dev checkout — a packaged .app always carries
            // its own backend.
            phase = .failed(L("找不到 moo-trader 仓库（web/server.py）。请把 MooTrader.app 放在仓库的 macos/dist/ 内。", "Can’t find the moo-trader repo (web/server.py). Put MooTrader.app inside the repo’s macos/dist/."))
            return
        }
        launch = foundLaunch
        workDir = work
        port = found.port
        APIClient.shared.baseURL = baseURL
        switch foundLaunch {
        case .bundled(let bin): debugLog("run: bundled backend=\(bin.path) home=\(work.path) port=\(port)")
        case .repo:             debugLog("run: repo=\(work.path) port=\(port)")
        }

        if await serverResponds(timeout: 0.8) {
            debugLog("adopted existing server")
            phase = .running
        } else {
            spawnServer()
            if await waitForServer(seconds: 25) {
                debugLog("spawned server is up")
                phase = .running
            } else {
                debugLog("spawned server did not come up in 25s")
                phase = .failed(L("Web 服务器 25 秒内未就绪 — 查看 logs/web.log", "Web server didn’t come up within 25s — see logs/web.log"))
                return
            }
        }
        await monitorLoop()
    }

    /// Detached spawn identical to start-web.command: nohup + background +
    /// pid file ($! is the backend's pid — mkdir is a separate statement so the
    /// background job isn't a compound wrapped in a lingering subshell).
    /// bash exits immediately; the backend survives this app quitting.
    private func spawnServer() {
        guard let launch, let work = workDir else { return }
        // On a packaged first run the home doesn't exist yet, and the shell
        // redirect into logs/web.log happens before Python can create it.
        try? FileManager.default.createDirectory(
            at: work.appendingPathComponent("logs"), withIntermediateDirectories: true)
        let exe: String
        switch launch {
        case .bundled(let bin): exe = Self.shellQuote(bin.path)
        case .repo:             exe = ".venv/bin/python web/server.py"
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c",
            "mkdir -p logs; nohup \(exe) >> logs/web.log 2>&1 & echo $! > logs/web.pid"]
        p.currentDirectoryURL = work
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            debugLog("spawned web server via bash")
        } catch {
            debugLog("spawn failed: \(error.localizedDescription)")
        }
    }

    /// Single-quote for /bin/bash. The bundle path is user-chosen — it can hold
    /// spaces ("MooTrader 2.app") long before it holds a quote.
    private nonisolated static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
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
                phase = .starting(L("Web 服务器重启中…", "Restarting web server…"))
                spawnServer()
                if await waitForServer(seconds: 25) {
                    phase = .running
                    misses = 0
                    continue
                }
            }
            phase = .failed(L("Web 服务器已停止且自动重启失败 — 查看 logs/web.log", "Web server stopped and auto-restart failed — see logs/web.log"))
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

    /// Lifecycle breadcrumbs → logs/shell-debug.log (gitignored) + Console.
    /// Detached: in dev, writing into the repo can block on the TCC prompt, and
    /// the main actor must never be the thread that waits for it.
    private func debugLog(_ msg: String) {
        NSLog("MooTrader: %@", msg)
        guard let work = workDir else { return }
        let url = work.appendingPathComponent("logs/shell-debug.log")
        let line = "\(Date()) \(msg)\n"
        Task.detached {
            guard let data = line.data(using: .utf8) else { return }
            if let h = try? FileHandle(forWritingTo: url) {
                h.seekToEndOfFile(); h.write(data); try? h.close()
            } else {
                try? data.write(to: url)
            }
        }
    }
}
