import SwiftUI

struct ContentView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller

    var body: some View {
        Group {
            switch backend.phase {
            case .starting(let note):
                message {
                    ProgressView()
                    Text(note)
                        .font(.title3)
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.secondary)
                }

            case .running:
                if poller.needsLogin {
                    // Never load the web view while unauthenticated: it would
                    // land on /login and stay there after the sheet wins.
                    message { Text("等待登录…").font(.title3).foregroundStyle(.secondary) }
                } else {
                    RootView()
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

/// Sidebar navigation. Native views cover the day-to-day surfaces; the full web
/// dashboard stays one click away for signals, sectors, backtest and settings.
struct RootView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller
    @State private var section: Section? = .overview

    enum Section: String, CaseIterable, Identifiable {
        case overview, approvals, history, web
        var id: String { rawValue }

        var title: String {
            switch self {
            case .overview: return "总览"
            case .approvals: return "审批"
            case .history: return "历史"
            case .web: return "完整面板"
            }
        }

        var icon: String {
            switch self {
            case .overview: return "chart.pie"
            case .approvals: return "checkmark.seal"
            case .history: return "clock.arrow.circlepath"
            case .web: return "globe"
            }
        }
    }

    var body: some View {
        NavigationSplitView {
            List(Section.allCases, selection: $section) { item in
                NavigationLink(value: item) {
                    Label {
                        HStack {
                            Text(item.title)
                            if item == .approvals, !poller.pending.isEmpty {
                                Spacer()
                                Text(String(poller.pending.count))
                                    .font(.caption2).monospacedDigit()
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(.orange, in: Capsule())
                                    .foregroundStyle(.white)
                            }
                        }
                    } icon: {
                        Image(systemName: item.icon)
                    }
                }
            }
            .navigationSplitViewColumnWidth(min: 150, ideal: 170, max: 220)
        } detail: {
            switch section ?? .overview {
            case .overview:  OverviewView()
            case .approvals: ApprovalsView()
            case .history:   HistoryView()
            case .web:       DashboardWebView(url: backend.baseURL).navigationTitle("完整面板")
            }
        }
    }
}
