import SwiftUI

struct ContentView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller
    @ObservedObject private var l10n = L10n.shared

    var body: some View {
        Group {
            switch backend.phase {
            case .starting(let note):
                message {
                    ProgressView()
                        .controlSize(.large)
                        .tint(Theme.blue)
                    Text(note)
                        .font(.system(size: 15))
                        .multilineTextAlignment(.center)
                        .foregroundStyle(Theme.muted)
                }

            case .running:
                if poller.needsLogin {
                    // Never load the web view while unauthenticated: it would
                    // land on /login and stay there after the sheet wins.
                    message {
                        BrandMark(size: 44)
                        Text(L("等待登录…", "Waiting for login…"))
                            .font(.system(size: 15))
                            .foregroundStyle(Theme.muted)
                    }
                } else {
                    RootView()
                }

            case .failed(let text):
                message {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 34))
                        .foregroundStyle(Theme.amber)
                    Text(text)
                        .font(Theme.Font_.body)
                        .foregroundStyle(Theme.text)
                        .multilineTextAlignment(.center)
                        .textSelection(.enabled)
                        .frame(maxWidth: 480)
                    Button(L("重新启动引擎", "Restart engine")) { backend.start() }
                        .buttonStyle(BrandButtonStyle())
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .sheet(isPresented: $poller.needsLogin) {
            LoginSheet().environmentObject(poller)
        }
    }

    private func message<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(spacing: Theme.Space.lg, content: content)
            .padding(Theme.Space.xl)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .pageBackground()
    }
}

/// The app's bull mark in the brand gradient. Template rendering lets the
/// gradient paint straight through the glyph.
struct BrandMark: View {
    var size: CGFloat = 26

    var body: some View {
        Theme.brand
            .mask {
                Image(nsImage: AppGlyph.sidebar)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fit)
            }
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

/// Sidebar navigation. Native views cover everything day to day; the full web
/// dashboard stays one click away for the backtest tab and the sector heatmap.
struct RootView: View {
    @EnvironmentObject var backend: BackendController
    @EnvironmentObject var poller: StatusPoller
    @ObservedObject private var l10n = L10n.shared
    @State private var section: Section? = .overview

    enum Section: String, CaseIterable, Identifiable {
        case overview, approvals, signals, history, settings, web
        var id: String { rawValue }

        @MainActor var title: String {
            switch self {
            case .overview: return L("总览", "Overview")
            case .approvals: return L("审批", "Approvals")
            case .signals: return L("信号", "Signals")
            case .history: return L("历史", "History")
            case .settings: return L("设置", "Settings")
            case .web: return L("完整面板", "Web panel")
            }
        }

        var icon: String {
            switch self {
            case .overview: return "chart.pie.fill"
            case .approvals: return "checkmark.seal.fill"
            case .signals: return "antenna.radiowaves.left.and.right"
            case .history: return "clock.arrow.circlepath"
            case .settings: return "gearshape.fill"
            case .web: return "globe"
            }
        }

        /// One hue per destination, so the eye can find a row by colour alone.
        var tint: Color {
            switch self {
            case .overview: return Theme.blue
            case .approvals: return Theme.amber
            case .signals: return Theme.purple
            case .history: return Theme.green
            case .settings: return Theme.muted
            case .web: return Theme.blueUp
            }
        }
    }

    var body: some View {
        NavigationSplitView {
            VStack(spacing: 0) {
                header
                List(Section.allCases, selection: $section) { item in
                    NavigationLink(value: item) {
                        Label {
                            HStack(spacing: Theme.Space.sm) {
                                Text(item.title)
                                    .font(.system(size: 13, weight: .medium))
                                if item == .approvals, !poller.pending.isEmpty {
                                    Spacer(minLength: Theme.Space.xs)
                                    Text(String(poller.pending.count))
                                        .font(.system(size: 10, weight: .semibold))
                                        .monospacedDigit()
                                        .padding(.horizontal, 6)
                                        .padding(.vertical, 2)
                                        .background(Theme.amber, in: Capsule())
                                        .foregroundStyle(.black)
                                }
                            }
                        } icon: {
                            Image(systemName: item.icon)
                                .foregroundStyle(item.tint)
                        }
                    }
                }
                .listStyle(.sidebar)
                .scrollContentBackground(.hidden)
            }
            .background(Theme.bg)
            .navigationSplitViewColumnWidth(min: 150, ideal: 176, max: 220)
        } detail: {
            switch section ?? .overview {
            case .overview:  OverviewView()
            case .approvals: ApprovalsView()
            case .signals:   SignalsView()
            case .history:   HistoryView()
            case .settings:  SettingsPanelView()
            case .web:       DashboardWebView(url: backend.baseURL).navigationTitle(L("完整面板", "Web panel"))
            }
        }
        .tint(Theme.blue)
    }

    /// Wordmark plus a live scheduler dot — the one place the running state is
    /// visible no matter which section is open.
    private var header: some View {
        HStack(spacing: Theme.Space.sm) {
            BrandMark(size: 22)
            VStack(alignment: .leading, spacing: 1) {
                Text("Moo Trader")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Theme.strong)
                HStack(spacing: 4) {
                    Circle()
                        .fill(schedulerTint)
                        .frame(width: 5, height: 5)
                    Text(schedulerNote)
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.muted)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Theme.Space.md)
        .padding(.top, Theme.Space.sm)
        .padding(.bottom, Theme.Space.md)
    }

    private var schedulerTint: Color {
        if poller.schedulerPID != nil { return Theme.green }
        return poller.schedulerBusy ? Theme.amber : Theme.muted
    }

    private var schedulerNote: String {
        if poller.schedulerPID != nil { return L("调度器运行中", "Scheduler running") }
        return poller.schedulerBusy ? L("切换中…", "Switching…") : L("调度器已停止", "Scheduler stopped")
    }
}
