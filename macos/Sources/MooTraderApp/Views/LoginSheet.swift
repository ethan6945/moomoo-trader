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
        VStack(spacing: 16) {
            Text("需要登录")
                .font(.headline)
            SecureField("Web 密码（.env 中的 WEB_PASSWORD）", text: $password)
                .textFieldStyle(.roundedBorder)
                .frame(width: 280)
                .onSubmit { submit() }
            if let error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            Button(busy ? "登录中…" : "登录") { submit() }
                .keyboardShortcut(.defaultAction)
                .disabled(busy || password.isEmpty)
        }
        .padding(24)
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
