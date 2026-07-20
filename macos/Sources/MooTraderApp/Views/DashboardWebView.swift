import SwiftUI
import WebKit

/// The existing web dashboard, hosted natively. Replaces pywebview.
///
/// Auth: the native URLSession and WKWebView keep SEPARATE cookie jars, so the
/// `wt_auth` cookie won from the native login sheet is copied into the web
/// view's store before the first load — otherwise the page would just bounce
/// to /login while the native side is happily authenticated.
struct DashboardWebView: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.uiDelegate = context.coordinator
        web.allowsBackForwardNavigationGestures = false
        #if DEBUG
        web.isInspectable = true
        #endif
        loadAuthenticated(web)
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        // Reload after a backend restart left the view on an error page.
        if web.url == nil {
            loadAuthenticated(web)
        }
    }

    /// Copy any cookies for this origin from the shared (URLSession) jar into
    /// the web view's jar, then load. Runs on the main actor; the load happens
    /// once every cookie is stored.
    private func loadAuthenticated(_ web: WKWebView) {
        let cookies = HTTPCookieStorage.shared.cookies(for: url) ?? []
        guard !cookies.isEmpty else {
            web.load(URLRequest(url: url))
            return
        }
        let store = web.configuration.websiteDataStore.httpCookieStore
        Task { @MainActor in
            for cookie in cookies {
                await store.setCookie(cookie)
            }
            web.load(URLRequest(url: url))
        }
    }

    final class Coordinator: NSObject, WKUIDelegate {
        /// target=_blank / window.open → external links go to the default
        /// browser (e.g. the GitHub sponsor link); same-origin stays put.
        func webView(_ webView: WKWebView,
                     createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction,
                     windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let target = navigationAction.request.url {
                if target.host == webView.url?.host {
                    webView.load(navigationAction.request)
                } else {
                    NSWorkspace.shared.open(target)
                }
            }
            return nil
        }
    }
}
