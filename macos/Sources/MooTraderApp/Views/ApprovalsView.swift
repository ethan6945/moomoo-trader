import SwiftUI

/// Approval queue. Pending items get ✓ / ✗ (same single-click semantics as the
/// web modal); resolved ones stay listed for context.
struct ApprovalsView: View {
    @EnvironmentObject var poller: StatusPoller

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if poller.pending.isEmpty {
                    Text("✅ 没有待处理的审批")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.vertical, 20)
                } else {
                    Panel(title: "待处理 · \(poller.pending.count)") {
                        VStack(spacing: 8) {
                            ForEach(poller.pending) { item in
                                row(item, actionable: true)
                            }
                        }
                    }
                }

                let resolved = poller.approvals.filter { !$0.isPending }
                if !resolved.isEmpty {
                    Panel(title: "已处理") {
                        VStack(spacing: 8) {
                            ForEach(resolved.prefix(30)) { item in
                                row(item, actionable: false)
                            }
                        }
                    }
                }
            }
            .padding(16)
        }
        .navigationTitle("审批")
    }

    private func row(_ item: Approval, actionable: Bool) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Pill(text: item.kind ?? "—",
                         tint: actionable ? .orange : .secondary)
                    Text(Fmt.stamp(item.createdAt))
                        .font(.caption2).foregroundStyle(.tertiary)
                    if !actionable, let status = item.status {
                        Text(status == "approved" ? "已通过" : "已拒绝")
                            .font(.caption2)
                            .foregroundStyle(status == "approved" ? .green : .red)
                    }
                }
                Text(item.detail ?? "")
                    .font(.callout)
                    .textSelection(.enabled)
                if let action = item.action, !action.isEmpty {
                    Text("→ \(action)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            Spacer(minLength: 8)
            if actionable {
                Button("✓") { poller.resolveApproval(item.id, action: "approve") }
                    .buttonStyle(.borderedProminent)
                Button("✗") { poller.resolveApproval(item.id, action: "reject") }
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
    }
}
