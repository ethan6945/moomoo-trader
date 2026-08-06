import SwiftUI

/// Startup readiness dialog, shown when the user presses ▶ 启动.
///
/// The point is not to gate the bot — src/preflight.py treats exactly one thing
/// (the broker connection) as a blocker. The point is that everything else in
/// this system degrades SILENTLY: a missing API key makes a layer return
/// neutral and the bot keeps trading, so an owner can run for days believing
/// they have signal review and news analysis when they have neither. This sheet
/// is where that becomes visible, once, at the moment they choose to start.
///
/// So a degraded item is never presented as an error. It states what is switched
/// off, offers the key that switches it on, and lets the user retry that one
/// item without restarting anything — or start anyway, knowingly.
struct PreflightSheet: View {
    @ObservedObject private var l10n = L10n.shared
    let result: PreflightResult
    /// Called when the user accepts and wants the scheduler to start.
    var onStart: () -> Void
    var onCancel: () -> Void

    @State private var checks: [PreflightCheck]
    @State private var editing: [String: String] = [:]   // fix_key -> typed value
    @State private var busy: Set<String> = []
    @State private var note: String?

    init(result: PreflightResult, onStart: @escaping () -> Void, onCancel: @escaping () -> Void) {
        self.result = result
        self.onStart = onStart
        self.onCancel = onCancel
        _checks = State(initialValue: result.checks)
    }

    private var blocked: Bool { checks.contains { $0.severity == "blocker" && !$0.ok } }
    private var degradedCount: Int { checks.filter { $0.severity == "degraded" && !$0.ok }.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: Theme.Space.sm) {
                    ForEach(checks) { c in row(c) }
                }
                .padding(Theme.Space.lg)
            }
            Divider()
            footer
        }
        .frame(width: 620, height: 560)
        .pageBackground()
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(L("启动前检查", "Preflight check"))
                .font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.strong)
            Text(headline)
                .font(Theme.Font_.body)
                .foregroundStyle(blocked ? Theme.red : (degradedCount > 0 ? Theme.amber : Theme.green))
            if let note {
                Text(note).font(Theme.Font_.label).foregroundStyle(Theme.muted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Space.lg)
    }

    private var headline: String {
        if blocked { return L("无法启动 — 下面标红的项目必须先解决", "Cannot start — the item in red must be resolved first") }
        if degradedCount > 0 {
            return L("可以启动，但有 \(degradedCount) 项功能是关闭的（见下）",
                     "Ready to start — \(degradedCount) capability(ies) are switched off (below)")
        }
        return L("全部就绪", "All systems ready")
    }

    @ViewBuilder
    private func row(_ c: PreflightCheck) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Space.sm) {
                Text(badge(c)).font(.system(size: 10, weight: .bold))
                    .foregroundStyle(tint(c))
                    .frame(width: 54, alignment: .leading)
                VStack(alignment: .leading, spacing: 3) {
                    Text(l10n.lang == "en" ? c.titleEn : c.title)
                        .font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.strong)
                    Text(l10n.lang == "en" ? c.detailEn : c.detail)
                        .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    // The sentence that matters: what this costs you.
                    if !(l10n.lang == "en" ? c.impactEn : c.impact).isEmpty {
                        Text(l10n.lang == "en" ? c.impactEn : c.impact)
                            .font(Theme.Font_.label).foregroundStyle(Theme.text)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
                if c.retryable && !c.ok {
                    Button(L("重试", "Retry")) { recheck(c.id) }
                        .buttonStyle(QuietButtonStyle(tint: Theme.blue))
                        .disabled(busy.contains(c.id))
                }
            }
            // Inline fix: only when the server named a key it will actually
            // accept. A field that 400s on save is worse than no field.
            if let key = c.fixKey, !c.ok {
                HStack(spacing: 6) {
                    SecureField(key, text: Binding(
                        get: { editing[key] ?? "" },
                        set: { editing[key] = $0 }))
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 11, design: .monospaced))
                    Button(L("保存并重试", "Save & retry")) { saveAndRetry(key: key, checkID: c.id) }
                        .buttonStyle(QuietButtonStyle(tint: Theme.green))
                        .disabled(busy.contains(c.id) || (editing[key] ?? "").isEmpty)
                }
                if !(l10n.lang == "en" ? c.fixHintEn : c.fixHint).isEmpty {
                    Text(l10n.lang == "en" ? c.fixHintEn : c.fixHint)
                        .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else if !(l10n.lang == "en" ? c.fixHintEn : c.fixHint).isEmpty, !c.ok {
                Text(l10n.lang == "en" ? c.fixHintEn : c.fixHint)
                    .font(Theme.Font_.label).foregroundStyle(Theme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Theme.Space.md)
        .rowSurface()
    }

    private func badge(_ c: PreflightCheck) -> String {
        if c.ok && c.severity != "degraded" { return "OK" }
        switch c.severity {
        case "blocker":  return L("必须", "BLOCK")
        case "degraded": return L("已关闭", "OFF")
        default:         return L("说明", "INFO")
        }
    }

    private func tint(_ c: PreflightCheck) -> Color {
        if c.ok && c.severity != "degraded" { return Theme.green }
        switch c.severity {
        case "blocker":  return Theme.red
        case "degraded": return Theme.amber
        default:         return Theme.muted
        }
    }

    private var footer: some View {
        HStack {
            Button(L("重新全部检查", "Re-check all")) { recheckAll() }
                .buttonStyle(QuietButtonStyle(tint: Theme.muted))
                .disabled(!busy.isEmpty)
            Spacer()
            Button(L("取消", "Cancel")) { onCancel() }
                .buttonStyle(QuietButtonStyle(tint: Theme.muted))
            // Deliberately says "start anyway" when degraded: the user is
            // choosing to run with things off, and the button should admit it.
            Button(blocked ? L("无法启动", "Cannot start")
                           : (degradedCount > 0 ? L("仍然启动", "Start anyway")
                                                : L("启动", "Start"))) { onStart() }
                .buttonStyle(BrandButtonStyle())
                .disabled(blocked || !busy.isEmpty)
        }
        .padding(Theme.Space.lg)
    }

    // MARK: - actions

    private func replace(_ c: PreflightCheck) {
        if let i = checks.firstIndex(where: { $0.id == c.id }) { checks[i] = c }
    }

    private func recheck(_ id: String) {
        busy.insert(id); note = nil
        Task {
            defer { busy.remove(id) }
            do { replace(try await APIClient.shared.preflightRecheck(id)) }
            catch { note = L("重试失败: ", "Retry failed: ") + error.localizedDescription }
        }
    }

    private func saveAndRetry(key: String, checkID: String) {
        guard let value = editing[key], !value.isEmpty else { return }
        busy.insert(checkID); note = nil
        Task {
            defer { busy.remove(checkID) }
            do {
                try await APIClient.shared.setSettingsKey(key, value: value)
                editing[key] = ""
                // The server drops its probe cache on save, so this really
                // re-tests the new value rather than echoing the old verdict.
                replace(try await APIClient.shared.preflightRecheck(checkID))
                note = L("已保存到 .env。调度器重启后这项才会真正被使用。",
                         "Saved to .env. It takes effect once the scheduler restarts.")
            } catch {
                note = L("保存失败: ", "Save failed: ") + error.localizedDescription
            }
        }
    }

    private func recheckAll() {
        let ids = checks.map(\.id)
        ids.forEach { busy.insert($0) }
        note = nil
        Task {
            defer { busy.removeAll() }
            if let fresh = try? await APIClient.shared.preflight(fresh: true), !fresh.checks.isEmpty {
                checks = fresh.checks
            }
        }
    }
}
