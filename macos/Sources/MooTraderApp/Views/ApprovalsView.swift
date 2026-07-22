import SwiftUI

/// Approval queue. Pending items get ✓ / ✗ (same single-click semantics as the
/// web modal); resolved ones stay listed for context.
struct ApprovalsView: View {
    @EnvironmentObject var poller: StatusPoller

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Space.md) {
                if poller.pending.isEmpty {
                    Panel(title: "待处理") {
                        EmptyNote(text: "✅ 没有待处理的审批")
                    }
                } else {
                    Panel(title: "待处理", subtitle: "\(poller.pending.count) 条") {
                        VStack(spacing: Theme.Space.sm) {
                            ForEach(poller.pending) { row($0, actionable: true) }
                        }
                    }
                }

                let resolved = poller.approvals.filter { !$0.isPending }
                if !resolved.isEmpty {
                    Panel(title: "已处理", subtitle: "最近 \(min(resolved.count, 30)) 条") {
                        VStack(spacing: Theme.Space.sm) {
                            ForEach(resolved.prefix(30)) { row($0, actionable: false) }
                        }
                    }
                }
            }
            .padding(Theme.Space.xl)
        }
        .pageBackground()
        .navigationTitle("审批")
    }

    private func row(_ item: Approval, actionable: Bool) -> some View {
        HStack(alignment: .top, spacing: Theme.Space.md) {
            // Status rail: amber while it waits on you, quiet once resolved.
            RoundedRectangle(cornerRadius: 2)
                .fill(actionable ? Theme.amber
                      : (item.status == "approved" ? Theme.green : Theme.red).opacity(0.5))
                .frame(width: 3)

            VStack(alignment: .leading, spacing: Theme.Space.xs) {
                HStack(spacing: Theme.Space.sm) {
                    Pill(text: item.kind ?? "—", tint: actionable ? Theme.amber : Theme.muted)
                    Text(Fmt.stamp(item.createdAt))
                        .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                    if !actionable, let status = item.status {
                        Pill(text: status == "approved" ? "已通过" : "已拒绝",
                             tint: status == "approved" ? Theme.green : Theme.red)
                    }
                }
                Text(item.detail ?? "")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.text)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                if let action = item.action, !action.isEmpty {
                    Text("→ \(action)")
                        .font(Theme.Font_.body)
                        .foregroundStyle(Theme.muted)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: Theme.Space.sm)
            if actionable {
                HStack(spacing: Theme.Space.sm) {
                    Button("✓ 通过") { poller.resolveApproval(item.id, action: "approve") }
                        .buttonStyle(BrandButtonStyle(
                            tint: LinearGradient(colors: [Theme.green, Theme.green.opacity(0.75)],
                                                 startPoint: .top, endPoint: .bottom)))
                    Button("✗ 拒绝") { poller.resolveApproval(item.id, action: "reject") }
                        .buttonStyle(QuietButtonStyle(tint: Theme.red))
                }
            }
        }
        .padding(Theme.Space.md)
        .rowSurface()
    }
}
