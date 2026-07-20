import Foundation

/// Thin URLSession wrapper for the Flask API on 127.0.0.1.
///
/// Auth: when WEB_PASSWORD is set, `POST /api/login` stores the `wt_auth`
/// cookie in the shared HTTPCookieStorage; every later request carries it
/// automatically. On localhost without a password there is no auth at all.
final class APIClient: @unchecked Sendable {
    static let shared = APIClient()

    var baseURL = URL(string: "http://127.0.0.1:8770")!

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 15
        cfg.httpCookieStorage = .shared
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

    struct HTTPError: Error {
        let status: Int
        var isAuthRequired: Bool { status == 401 }
    }

    // ── core ──────────────────────────────────────────────────────────
    /// `appendingPathComponent` would percent-escape a "?" into the path
    /// (/api/log%3Fn=40 → 404), so query strings are resolved as a relative
    /// URL against the base instead.
    private func url(for path: String) -> URL {
        URL(string: path, relativeTo: baseURL)?.absoluteURL
            ?? baseURL.appendingPathComponent(path)
    }

    private func request(_ path: String, method: String, json: [String: Any]?,
                         timeout: TimeInterval) async throws -> Data {
        var req = URLRequest(url: url(for: path), timeoutInterval: timeout)
        req.httpMethod = method
        if let json {
            req.httpBody = try JSONSerialization.data(withJSONObject: json)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await session.data(for: req)
        if let http = resp as? HTTPURLResponse, http.statusCode >= 400 {
            throw HTTPError(status: http.statusCode)
        }
        return data
    }

    func get(_ path: String, timeout: TimeInterval = 10) async throws -> Data {
        try await request(path, method: "GET", json: nil, timeout: timeout)
    }

    @discardableResult
    func post(_ path: String, json: [String: Any]? = nil,
              timeout: TimeInterval = 15) async throws -> Data {
        try await request(path, method: "POST", json: json, timeout: timeout)
    }

    func getJSON<T: Decodable>(_ path: String, as type: T.Type = T.self,
                               timeout: TimeInterval = 10) async throws -> T {
        try JSONDecoder().decode(T.self, from: await get(path, timeout: timeout))
    }

    // ── typed endpoints ───────────────────────────────────────────────
    func status() async throws -> TraderStatus {
        try await getJSON("api/status")
    }

    func caffeinate() async throws -> CaffeinateStatus {
        try await getJSON("api/caffeinate")
    }

    func approvals() async throws -> [Approval] {
        try await getJSON("api/approvals")
    }

    /// action ∈ approve / reject.
    func resolveApproval(_ id: String, action: String) async throws {
        try await post("api/approvals/\(id)/\(action)")
    }

    func closedTrades(limit: Int = 400) async throws -> [ClosedTrade] {
        try await getJSON("api/closed?n=\(limit)")
    }

    func activityLog(lines: Int = 40) async throws -> [String] {
        try await getJSON("api/log?n=\(lines)")
    }

    func setCaffeinate(on: Bool) async throws {
        try await post("api/caffeinate", json: ["on": on])
    }

    /// start also auto-launches OpenD and can block up to ~60 s waiting for
    /// port 11111 — hence the long timeout.
    func scheduler(_ action: String) async throws {
        try await post("api/scheduler/\(action)", timeout: 90)
    }

    /// Stops OpenD + scheduler + web server (the whole system).
    func exitEverything() async throws {
        try await post("api/exit", timeout: 20)
    }

    func login(password: String) async throws {
        try await post("api/login", json: ["password": password])
    }

    // ── signals (盯盘) ────────────────────────────────────────────────
    func monitorTicks() async throws -> [MonitorTick] {
        let raw: [String: MonitorTick.Payload] = try await getJSON("api/signal-monitor")
        return raw.map { MonitorTick(symbol: $0.key, p: $0.value) }
            .sorted { ($0.net ?? 0, $0.symbol) > ($1.net ?? 0, $1.symbol) }
    }

    func signalAlerts(limit: Int = 80) async throws -> [SignalAlert] {
        try await getJSON("api/signal-alerts?n=\(limit)")
    }

    func signalStatus() async throws -> RunningState {
        try await getJSON("api/signal-status")
    }

    /// action ∈ start / stop.
    func signalScheduler(_ action: String) async throws {
        try await post("api/signal-scheduler/\(action)")
    }

    /// mode ∈ brief / review / close / premarket / intraday / monitor.
    func signalRun(_ mode: String) async throws {
        try await post("api/signal-run/\(mode)", timeout: 30)
    }

    func signalWatchlist() async throws -> [String] {
        struct Reply: Decodable { var tickers: [String] }
        let r: Reply = try await getJSON("api/signal-watchlist")
        return r.tickers
    }

    func setSignalWatchlist(_ tickers: [String]) async throws {
        try await post("api/signal-watchlist", json: ["tickers": tickers])
    }

    // ── settings ─────────────────────────────────────────────────────
    func tradeEnv() async throws -> TradeEnvState {
        try await getJSON("api/trade-env")
    }

    /// REAL requires confirm:true and is refused (409) with open positions.
    func setTradeEnv(_ env: String, confirm: Bool = false) async throws {
        try await post("api/trade-env", json: ["env": env, "confirm": confirm])
    }

    func setBudget(_ value: Double) async throws {
        try await post("api/budget", json: ["value": value])
    }

    func autoBudget() async throws -> AutoBudgetState {
        try await getJSON("api/auto-budget")
    }

    /// action ∈ arm / disarm / recompute.
    func autoBudgetAction(_ action: String) async throws {
        try await post("api/auto-budget", json: ["action": action])
    }

    func setAutoBudget(enabled: Bool) async throws {
        try await post("api/auto-budget", json: ["enabled": enabled])
    }

    func aiProvider() async throws -> AIProviderState {
        try await getJSON("api/ai-provider")
    }

    func setAIProvider(_ provider: String, model: String) async throws {
        try await post("api/ai-provider", json: ["provider": provider, "model": model])
    }

    func aiModels(provider: String) async throws -> [String] {
        struct Reply: Decodable { var models: [String]? }
        let r: Reply = try await getJSON("api/ai-models?provider=\(provider)", timeout: 20)
        return r.models ?? []
    }

    func settingsKeys() async throws -> SettingsKeys {
        try await getJSON("api/settings")
    }

    /// Writes one allowed key into .env. Values come from the user typing into
    /// the sheet — the app never reads or displays the stored secret.
    func setSettingsKey(_ key: String, value: String) async throws {
        try await post("api/settings/key", json: ["key": key, "value": value])
    }

    func webAccess() async throws -> WebAccessState {
        try await getJSON("api/web-access")
    }

    func setWebAccess(mode: String) async throws {
        try await post("api/web-access", json: ["mode": mode])
    }

    func resetStats() async throws {
        try await post("api/reset-stats")
    }
}
