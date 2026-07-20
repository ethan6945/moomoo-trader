import SwiftUI

struct ContentView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller

    var body: some View {
        Group {
            switch backend.phase {
            case .starting:
                message { ProgressView(); Text("引擎启动中…").font(.title3).foregroundStyle(.secondary) }

            case .running:
                if poller.needsLogin {
                    // Never load the page while unauthenticated: it would land
                    // on /login and stay there after the native sheet wins.
                    message { Text("等待登录…").font(.title3).foregroundStyle(.secondary) }
                } else {
                    DashboardWebView(url: backend.baseURL)
                }

            case .failed(let text):
                message {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 40)).foregroundStyle(.orange)
                    Text(text).multilineTextAlignment(.center).frame(maxWidth: 480)
                    Button("重新启动引擎") { backend.start() }
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .sheet(isPresented: $poller.needsLogin) {
            LoginSheet().environmentObject(poller)
        }
    }

    private func message<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(spacing: 14, content: content).padding()
    }
}
