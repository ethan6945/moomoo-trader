import Foundation

/// Slice of `/api/status` the native shell cares about. The payload is the full
/// account snapshot (data/account.json enriched by web/server.py); everything is
/// optional so schema drift on the Python side never crashes the shell.
struct TraderStatus: Decodable {
    var schedulerRunning: Bool?
    var opendStatus: String?      // red / green / blue / yellow
    var opendLabel: String?       // 中文状态标签
    var cash: Double?
    var invested: Double?
    var budget: Double?
    var unrealizedPnl: Double?
    var totalPnl: Double?
    var realizedPnlToday: Double?
    var tradeEnv: String?         // SIMULATE / REAL
    var pendingApprovals: Int?
    var positionsCount: Int?

    enum CodingKeys: String, CodingKey {
        case schedulerRunning = "scheduler_running"
        case opendStatus = "opend_status"
        case opendLabel = "opend_label"
        case cash, invested, budget
        case unrealizedPnl = "unrealized_pnl"
        case totalPnl = "total_pnl"
        case realizedPnlToday = "realized_pnl_today"
        case tradeEnv = "trade_env"
        case pendingApprovals = "pending_approvals"
        case positionsCount = "positions_count"
    }
}

/// `/api/caffeinate` GET → src/keepawake.py status().
struct CaffeinateStatus: Decodable {
    var on: Bool
    var lid: Bool?
}

/// Generic `{"ok": true, ...}` envelope used by most POST endpoints.
struct OkReply: Decodable {
    var ok: Bool?
    var error: String?
    var note: String?
}
