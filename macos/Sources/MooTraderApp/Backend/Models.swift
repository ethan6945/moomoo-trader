import Foundation

/// Every field is optional and decoded leniently: the Python side owns these
/// payloads, and one unexpected type must never blank the whole dashboard.
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
    var realizedPnlTotal: Double?
    var openRisk: Double?
    var heatCap: Double?
    var vix: Double?
    var regime: String?
    var regimeSub: String?
    var regimeNote: String?
    var phase: String?
    var tradeEnv: String?         // SIMULATE / REAL
    var lastScanUtc: String?
    var aiProvider: String?
    var aiModel: String?
    var positionsCount: Int?
    // Build identity of the BACKEND (which can differ from the shell's own
    // bundle — see the version row in Settings).
    var version: String?
    var runningFrom: String?
    var home: String?
    var summary: TradeSummary?
    var positions: [Position] = []

    var equity: Double { (cash ?? 0) + (invested ?? 0) }

    enum CodingKeys: String, CodingKey {
        case schedulerRunning = "scheduler_running"
        case opendStatus = "opend_status"
        case opendLabel = "opend_label"
        case cash, invested, budget, vix, regime, phase, summary
        case unrealizedPnl = "unrealized_pnl"
        case totalPnl = "total_pnl"
        case realizedPnlToday = "realized_pnl_today"
        case realizedPnlTotal = "realized_pnl_total"
        case openRisk = "open_risk"
        case heatCap = "heat_cap"
        case regimeSub = "regime_sub"
        case regimeNote = "regime_note"
        case tradeEnv = "trade_env"
        case lastScanUtc = "last_scan_utc"
        case aiProvider = "ai_provider"
        case aiModel = "ai_model"
        case positionsCount = "positions_count"
        case version
        case runningFrom = "running_from"
        case home
        case perPosition = "per_position"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func d<T: Decodable>(_ key: CodingKeys) -> T? {
            (try? c.decodeIfPresent(T.self, forKey: key)) ?? nil
        }
        schedulerRunning = d(.schedulerRunning)
        opendStatus = d(.opendStatus)
        opendLabel = d(.opendLabel)
        cash = d(.cash)
        invested = d(.invested)
        budget = d(.budget)
        unrealizedPnl = d(.unrealizedPnl)
        totalPnl = d(.totalPnl)
        realizedPnlToday = d(.realizedPnlToday)
        realizedPnlTotal = d(.realizedPnlTotal)
        openRisk = d(.openRisk)
        heatCap = d(.heatCap)
        vix = d(.vix)
        regime = d(.regime)
        regimeSub = d(.regimeSub)
        regimeNote = d(.regimeNote)
        phase = d(.phase)
        tradeEnv = d(.tradeEnv)
        lastScanUtc = d(.lastScanUtc)
        aiProvider = d(.aiProvider)
        aiModel = d(.aiModel)
        positionsCount = d(.positionsCount)
        version = d(.version)
        runningFrom = d(.runningFrom)
        home = d(.home)
        summary = d(.summary)
        // per_position is a {symbol: {...}} map — flatten into rows.
        let raw: [String: Position.Payload] = d(.perPosition) ?? [:]
        positions = raw.map { Position(symbol: $0.key, p: $0.value) }
            .sorted { $0.symbol < $1.symbol }
    }
}

struct TradeSummary: Decodable {
    var count: Int?
    var wins: Int?
    var winRate: Double?
    var grossWon: Double?
    var grossLost: Double?
    var net: Double?
    var streak: Int?
    var streakKind: String?      // win / loss / none

    enum CodingKeys: String, CodingKey {
        case count, wins, net, streak
        case winRate = "win_rate"
        case grossWon = "gross_won"
        case grossLost = "gross_lost"
        case streakKind = "streak_kind"
    }

    /// Profit factor — gross won ÷ gross lost (nil when nothing has closed).
    var profitFactor: Double? {
        let lost = abs(grossLost ?? 0), won = grossWon ?? 0
        guard (count ?? 0) > 0 else { return nil }
        return lost > 0 ? won / lost : (won > 0 ? .infinity : 0)
    }
}

struct Position: Identifiable {
    let symbol: String
    var qty: Double?
    var entryPrice: Double?
    var last: Double?
    var stopLoss: Double?
    var takeProfit: Double?
    var strategy: String?
    var pattern: String?
    var manualAdopted = false
    var userManaged = false
    var plValue: Double?
    var plRatio: Double?

    var id: String { symbol }

    /// Where `last` sits between stop (0) and take-profit (1) — the web UI's
    /// range bar. nil when the levels aren't usable.
    var rangeFraction: Double? {
        guard let sl = stopLoss, let tp = takeProfit, let last, tp > sl else { return nil }
        return min(1, max(0, (last - sl) / (tp - sl)))
    }

    var entryFraction: Double? {
        guard let sl = stopLoss, let tp = takeProfit, let e = entryPrice, tp > sl else { return nil }
        return min(1, max(0, (e - sl) / (tp - sl)))
    }

    struct Payload: Decodable {
        var qty: Double?
        var entryPrice: Double?
        var last: Double?
        var stopLoss: Double?
        var takeProfit: Double?
        var strategy: String?
        var pattern: String?
        var manualAdopted: Bool?
        var userManaged: Bool?
        var plValue: Double?
        var plRatio: Double?

        enum CodingKeys: String, CodingKey {
            case qty, last, strategy, pattern
            case entryPrice = "entry_price"
            case stopLoss = "stop_loss"
            case takeProfit = "take_profit"
            case manualAdopted = "manual_adopted"
            case userManaged = "user_managed"
            case plValue = "pl_val"
            case plRatio = "pl_ratio"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            func d<T: Decodable>(_ key: CodingKeys) -> T? {
                (try? c.decodeIfPresent(T.self, forKey: key)) ?? nil
            }
            qty = d(.qty); last = d(.last); strategy = d(.strategy); pattern = d(.pattern)
            entryPrice = d(.entryPrice); stopLoss = d(.stopLoss); takeProfit = d(.takeProfit)
            manualAdopted = d(.manualAdopted); userManaged = d(.userManaged)
            plValue = d(.plValue); plRatio = d(.plRatio)
        }
    }

    init(symbol: String, p: Payload) {
        self.symbol = symbol
        qty = p.qty; entryPrice = p.entryPrice; last = p.last
        stopLoss = p.stopLoss; takeProfit = p.takeProfit
        strategy = p.strategy; pattern = p.pattern
        manualAdopted = p.manualAdopted ?? false
        userManaged = p.userManaged ?? false
        plValue = p.plValue; plRatio = p.plRatio
    }
}

struct Approval: Decodable, Identifiable {
    let id: String
    var kind: String?
    var detail: String?
    var action: String?
    var status: String?
    var createdAt: String?
    var resolvedAt: String?

    var isPending: Bool { status == "pending" }

    enum CodingKeys: String, CodingKey {
        case id, kind, detail, action, status
        case createdAt = "created_at"
        case resolvedAt = "resolved_at"
    }
}

struct ClosedTrade: Decodable, Identifiable {
    var ts: String?
    var symbol: String?
    var qty: Double?
    var entry: Double?
    var exitPrice: Double?
    var exitReason: String?
    var pnl: Double?
    var pnlPct: Double?
    var rMultiple: Double?
    var strategy: String?

    // The SQLite row id's JSON type isn't guaranteed — identify by ts+symbol.
    var id: String { (ts ?? "") + "-" + (symbol ?? "") }

    enum CodingKeys: String, CodingKey {
        case ts, symbol, qty, entry, pnl, strategy
        case exitPrice = "exit"
        case exitReason = "exit_reason"
        case pnlPct = "pnl_pct"
        case rMultiple = "r_multiple"
    }

    var day: String { String((ts ?? "").prefix(10)) }
}

/// `/api/caffeinate` GET → src/keepawake.py status().
struct CaffeinateStatus: Decodable {
    var on: Bool
    var lid: Bool?
}

// ── US sector overview (/api/sectors) ────────────────────────────────
struct SectorOverview: Decodable {
    var sectors: [SectorTile] = []
    var indices: [SectorTile] = []
    var session: String?      // open / premarket / afterhours / weekend / holiday
    var asof: String?
    var stale: Bool?

    /// Session label + as-of date, matching the web panel's header line.
    @MainActor var sessionLabel: String {
        let map = ["open": "🟢 " + L("盘中", "Open"), "premarket": "🌅 " + L("盘前", "Pre-mkt"),
                   "afterhours": "🌙 " + L("盘后", "After-hrs"), "weekend": "⚪️ " + L("周末", "Weekend"),
                   "holiday": "⚪️ " + L("休市", "Holiday")]
        var s = map[session ?? ""] ?? ""
        if let asof, !asof.isEmpty { s += " · \(asof.dropFirst(5))" }
        if stale == true { s += " · " + L("延迟", "stale") }
        return s
    }
}

struct SectorTile: Decodable, Identifiable {
    var sym: String
    var zh: String?
    var en: String?
    var price: Double?
    var pct: Double?

    var id: String { sym }
}

// ── signals (盯盘) ────────────────────────────────────────────────────
/// One symbol's latest tick from data/signal_monitor_state.json.
struct MonitorTick: Identifiable {
    let symbol: String
    var ts: String?
    var price: Double?
    var chg: Double?
    var rsi: Double?
    var vol: Double?          // 量比
    var vwap: Double?
    var aboveVwap: Bool?
    var buy: Int?
    var sell: Int?
    var net: Int?
    var mom: Double?
    var dayHigh: Double?
    var dayLow: Double?
    var alertsToday: Int?
    var lastAlert: String?

    var id: String { symbol }

    struct Payload: Decodable {
        var ts: String?
        var price: Double?
        var chg: Double?
        var rsi: Double?
        var vol: Double?
        var vwap: Double?
        var above_vwap: Bool?
        var buy: Int?
        var sell: Int?
        var net: Int?
        var mom: Double?
        var day_high: Double?
        var day_low: Double?
        var alerts_today: Int?
        var last_alert: String?
    }

    init(symbol: String, p: Payload) {
        self.symbol = symbol
        ts = p.ts; price = p.price; chg = p.chg; rsi = p.rsi; vol = p.vol
        vwap = p.vwap; aboveVwap = p.above_vwap; buy = p.buy; sell = p.sell
        net = p.net; mom = p.mom; dayHigh = p.day_high; dayLow = p.day_low
        alertsToday = p.alerts_today; lastAlert = p.last_alert
    }
}

/// One entry of the alert feed (newest first from the API).
struct SignalAlert: Decodable, Identifiable {
    var tsIso: String?
    var sym: String?
    var type: String?
    var level: String?        // push / info
    var emoji: String?
    var title: String?
    var detail: String?
    var price: Double?
    var chg: Double?

    var id: String { (tsIso ?? "") + "-" + (sym ?? "") + "-" + (type ?? "") }
    var isPush: Bool { level == "push" }

    enum CodingKeys: String, CodingKey {
        case sym, type, level, emoji, title, detail, price, chg
        case tsIso = "ts_iso"
    }
}

struct RunningState: Decodable {
    var running: Bool?
    var pid: Int32?
}

// ── settings ─────────────────────────────────────────────────────────
/// `/api/trade-env` GET — .env value, live value, and the go-live guards.
struct TradeEnvState: Decodable {
    var envFile: String?
    var envLive: String?
    var running: Bool?
    var openPositions: Int?
    var pendingRestart: Bool?

    enum CodingKeys: String, CodingKey {
        case running
        case envFile = "env_file"
        case envLive = "env_live"
        case openPositions = "open_positions"
        case pendingRestart = "pending_restart"
    }
}

/// `/api/auto-budget` — compounding budget state.
struct AutoBudgetState: Decodable {
    var enabled: Bool?
    var armed: Bool?
    var seed: Double?
    var currentBudget: Double?
    var target: Double?
    var profitSinceArm: Double?

    enum CodingKeys: String, CodingKey {
        case enabled, armed, seed, target
        case currentBudget = "current_budget"
        case profitSinceArm = "profit_since_arm"
    }
}

/// `/api/ai-provider` — active engine plus the pickable ones.
struct AIProviderState: Decodable {
    var provider: String?
    var model: String?
    var supportsVision: Bool?
    var providers: [Provider]?

    struct Provider: Decodable, Identifiable {
        var id: String
        var label: String?
        var hasKey: Bool?
        var defaultModel: String?
        var vision: Bool?

        enum CodingKeys: String, CodingKey {
            case id, label, vision
            case hasKey = "has_key"
            case defaultModel = "default_model"
        }
    }

    enum CodingKeys: String, CodingKey {
        case provider, model, providers
        case supportsVision = "supports_vision"
    }
}

/// `/api/settings` — .env keys with masked values (never the real secret).
struct SettingsKeys: Decodable {
    var keys: [Item]

    struct Item: Decodable, Identifiable {
        var key: String
        var desc: String?
        var masked: String?
        var isSet: Bool?

        var id: String { key }

        enum CodingKeys: String, CodingKey {
            case key, desc, masked
            case isSet = "set"
        }
    }
}

/// `/api/web-access` — localhost vs LAN exposure.
struct WebAccessState: Decodable {
    var mode: String?          // local / lan
    var passwordSet: Bool?
    var port: Int?
    var lanIp: String?
    var tailscaleIp: String?

    enum CodingKeys: String, CodingKey {
        case mode, port
        case passwordSet = "password_set"
        case lanIp = "lan_ip"
        case tailscaleIp = "tailscale_ip"
    }
}
