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
}
