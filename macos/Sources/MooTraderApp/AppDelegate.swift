import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            BackendController.shared.start()
            StatusPoller.shared.start()
        }
    }

    /// The menu-bar icon stays alive after the window closes (unlike pywebview,
    /// where closing the window quit the GUI). Trading is unaffected either
    /// way — the Flask server / scheduler / OpenD are detached processes.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
