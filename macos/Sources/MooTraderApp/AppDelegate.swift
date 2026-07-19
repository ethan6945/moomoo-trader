import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            BackendController.shared.start()
            StatusPoller.shared.start()
        }
    }

    /// pywebview parity: closing the window quits the GUI only — the nohup'd
    /// Flask server, scheduler and OpenD keep running untouched.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}
