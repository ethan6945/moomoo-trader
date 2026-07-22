import SwiftUI

/// Shown only when WEB_PASSWORD is set (native API calls get 401). Successful
/// login stores the wt_auth cookie in the shared cookie jar; all later
/// URLSession requests carry it automatically.
struct LoginSheet: View {
    @EnvironmentObject var poller: StatusPoller
    @State private var password = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        VStack(spacing: Theme.Space.lg) {
            BrandMark(size: 40)

            VStack(spacing: Theme.Space.xs) {
                Text("需要登录")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Theme.strong)
                Text("请输入 .env 中的 WEB_PASSWORD")
                    .font(Theme.Font_.label)
                    .foregroundStyle(Theme.muted)
            }

            SecureField("Web 密码", text: $password)
                .textFieldStyle(.plain)
                .font(Theme.Font_.body)
                .padding(.horizontal, Theme.Space.md)
                .padding(.vertical, Theme.Space.sm)
                .frame(width: 280)
                .rowSurface()
                .overlay(RoundedRectangle(cornerRadius: Theme.Radius.row, style: .continuous)
                    .strokeBorder(error == nil ? Theme.border : Theme.red.opacity(0.6)))
                .onSubmit { submit() }

            // Reserved line, so the sheet doesn't jump when an error appears.
            Text(error ?? " ")
                .font(Theme.Font_.label)
                .foregroundStyle(Theme.red)
                .opacity(error == nil ? 0 : 1)

            Button(busy ? "登录中…" : "登录") { submit() }
                .buttonStyle(BrandButtonStyle())
                .keyboardShortcut(.defaultAction)
                .disabled(busy || password.isEmpty)
                .opacity(busy || password.isEmpty ? 0.5 : 1)
        }
        .padding(Theme.Space.xl + Theme.Space.xs)
        .frame(width: 340)
        .pageBackground()
    }

    private func submit() {
        busy = true
        error = nil
        Task {
            do {
                try await APIClient.shared.login(password: password)
                await poller.refresh()   // clears needsLogin → dismisses sheet
            } catch let e as APIClient.HTTPError where e.status == 429 {
                error = "尝试次数过多，已锁定 5 分钟"
            } catch {
                self.error = "密码错误"
            }
            busy = false
        }
    }
}
