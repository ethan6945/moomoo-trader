import SwiftUI

/// Native SwiftUI shell for Moo Trader. Phase 1: hosts the existing Flask web
/// dashboard in a WKWebView and owns the web-server lifecycle; a MenuBarExtra
/// replaces the old rumps icon. The trading scheduler / OpenD stay independent
/// detached processes — closing this app never stops trading.
@main
struct MooTraderApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var backend = BackendController.shared
    @StateObject private var poller = StatusPoller.shared

    var body: some Scene {
        WindowGroup("MooMoo Trader", id: "main") {
            ContentView()
                .environmentObject(backend)
                .environmentObject(poller)
                .frame(minWidth: 940, minHeight: 620)
        }
        .defaultSize(width: 1300, height: 880)
        // Reclaim the empty title-bar strip: the traffic lights float over the
        // content and the sidebar/detail fill all the way to the top edge.
        .windowStyle(.hiddenTitleBar)

        MenuBarExtra {
            MenuBarView()
                .environmentObject(backend)
                .environmentObject(poller)
        } label: {
            // The app icon's bull, as a template image — no more "MAT" text.
            Image(nsImage: AppGlyph.menuBar)
        }
    }
}
