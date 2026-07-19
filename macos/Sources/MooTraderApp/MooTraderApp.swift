import SwiftUI

/// Native SwiftUI shell for Moo Trader. Phase 1: hosts the existing Flask web
/// dashboard in a WKWebView and owns the web-server lifecycle. The trading
/// scheduler / OpenD stay independent detached processes — closing this app
/// never stops trading.
@main
struct MooTraderApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var backend = BackendController.shared
    @StateObject private var poller = StatusPoller.shared

    var body: some Scene {
        WindowGroup("MooMoo Trader") {
            ContentView()
                .environmentObject(backend)
                .environmentObject(poller)
                .frame(minWidth: 940, minHeight: 620)
        }
        .defaultSize(width: 1300, height: 880)
    }
}
