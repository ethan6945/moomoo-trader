import AppKit

/// The bull mark from the app icon, redrawn as a menu-bar template image.
///
/// Paths are the ones in web/static/favicon.svg (128×128 viewBox), minus the
/// rounded-square background and the nostrils — at 18pt those two only muddy
/// the silhouette. Template rendering means macOS tints it automatically, so it
/// stays legible in light and dark menu bars and inverts while the menu is open.
enum AppGlyph {
    static let menuBar: NSImage = make(size: NSSize(width: 18, height: 18))

    // Glyph bounds in favicon coordinates: horns spread x[15.5,112.5]
    // (the 13pt round caps widen them), arrow top y=13.6 → head bottom y=102.
    private static let box = CGRect(x: 15.5, y: 13.6, width: 97, height: 88.4)

    private static func make(size: NSSize) -> NSImage {
        let image = NSImage(size: size, flipped: false) { _ in
            guard let ctx = NSGraphicsContext.current?.cgContext else { return false }
            draw(in: ctx, canvas: size)
            return true
        }
        image.isTemplate = true
        return image
    }

    private static func draw(in ctx: CGContext, canvas: NSSize) {
        let scale = min((canvas.width - 1) / box.width, (canvas.height - 1) / box.height)
        let w = box.width * scale, h = box.height * scale

        ctx.saveGState()
        // Centre the glyph, then flip into SVG's y-down space so the published
        // path data can be used verbatim.
        ctx.translateBy(x: (canvas.width - w) / 2, y: (canvas.height - h) / 2 + h)
        ctx.scaleBy(x: scale, y: -scale)
        ctx.translateBy(x: -box.minX, y: -box.minY)

        ctx.setFillColor(.black)
        ctx.setStrokeColor(.black)

        // ── horns (strokes, round caps — drawn under the head) ──
        ctx.setLineWidth(13)
        ctx.setLineCap(.round)
        for horn in [(start: CGPoint(x: 44, y: 58), c1: CGPoint(x: 30, y: 56),
                      c2: CGPoint(x: 22, y: 46), end: CGPoint(x: 22, y: 30)),
                     (start: CGPoint(x: 84, y: 58), c1: CGPoint(x: 98, y: 56),
                      c2: CGPoint(x: 106, y: 46), end: CGPoint(x: 106, y: 30))] {
            let path = CGMutablePath()
            path.move(to: horn.start)
            path.addCurve(to: horn.end, control1: horn.c1, control2: horn.c2)
            ctx.addPath(path)
            ctx.strokePath()
        }

        // ── head ──
        let head = CGMutablePath()
        head.move(to: CGPoint(x: 36, y: 62))
        head.addQuadCurve(to: CGPoint(x: 48, y: 50), control: CGPoint(x: 36, y: 50))
        head.addLine(to: CGPoint(x: 80, y: 50))
        head.addQuadCurve(to: CGPoint(x: 92, y: 62), control: CGPoint(x: 92, y: 50))
        head.addLine(to: CGPoint(x: 92, y: 74))
        head.addQuadCurve(to: CGPoint(x: 64, y: 102), control: CGPoint(x: 92, y: 102))
        head.addQuadCurve(to: CGPoint(x: 36, y: 74), control: CGPoint(x: 36, y: 102))
        head.closeSubpath()
        ctx.addPath(head)
        ctx.fillPath()

        // ── eyes: punched out so the menu bar shows through. Slightly larger
        // than the favicon's r5 — at 18pt the head needs the negative space to
        // read as a face rather than a blob.
        ctx.setBlendMode(.destinationOut)
        for eye in [CGPoint(x: 52, y: 68), CGPoint(x: 76, y: 68)] {
            ctx.fillEllipse(in: CGRect(x: eye.x - 6, y: eye.y - 6, width: 12, height: 12))
        }
        ctx.setBlendMode(.normal)

        // ── the up arrow between the horns (favicon's arrow, 1.2× about its
        // centre so it survives being 3px tall) ──
        let arrow = CGMutablePath()
        arrow.addLines(between: [
            CGPoint(x: 64, y: 13.6), CGPoint(x: 74.8, y: 28), CGPoint(x: 68.4, y: 28),
            CGPoint(x: 68.4, y: 42.4), CGPoint(x: 59.6, y: 42.4), CGPoint(x: 59.6, y: 28),
            CGPoint(x: 53.2, y: 28),
        ])
        arrow.closeSubpath()
        ctx.addPath(arrow)
        ctx.fillPath()

        ctx.restoreGState()
    }
}
