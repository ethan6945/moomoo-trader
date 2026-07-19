import SwiftUI
import WebKit

/// The existing web dashboard, hosted natively. Replaces pywebview.
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
        web.load(URLRequest(url: url))
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        // Reload after a backend restart left the view on an error page.
        if web.url == nil {
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
