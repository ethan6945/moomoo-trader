import SwiftUI

struct ContentView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller

    var body: some View {
        Group {
            switch backend.phase {
            case .starting:
                VStack(spacing: 14) {
                    ProgressView()
                    Text("引擎启动中…")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
            case .running:
                DashboardWebView(url: backend.baseURL)
            case .failed(let message):
                VStack(spacing: 14) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 40))
                        .foregroundStyle(.orange)
                    Text(message)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 480)
                    Button("重新启动引擎") { backend.start() }
                        .keyboardShortcut(.defaultAction)
                }
                .padding()
            }
        }
        .sheet(isPresented: $poller.needsLogin) {
            LoginSheet()
        }
    }
}
