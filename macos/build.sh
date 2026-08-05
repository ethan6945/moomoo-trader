#!/bin/bash
# Build dist/MooTrader.app — the SwiftUI shell plus the frozen Python backend,
# so the result runs on a Mac with no repo, no venv and no Python installed.
# No Xcode needed, only Command Line Tools.
#
# Ad-hoc signed (there is no Developer ID certificate on this machine), so the
# first launch needs System Settings → Privacy & Security → "Open Anyway".
#
#   ./build.sh                  full build
#   ./build.sh --skip-backend   Swift shell only — reuses the last backend build
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"

SKIP_BACKEND=0
[[ "${1:-}" == "--skip-backend" ]] && SKIP_BACKEND=1

# ── 1. frozen Python backend ─────────────────────────────────────────────────
if [[ $SKIP_BACKEND -eq 0 ]]; then
    echo "▸ freezing the Python backend (this takes ~1 min)…"
    "$REPO/.venv/bin/pyinstaller" "$REPO/packaging/mmt-backend.spec" \
        --noconfirm \
        --distpath "$REPO/packaging/dist" \
        --workpath "$REPO/packaging/build" > /tmp/mmt-pyinstaller.log 2>&1 \
        || { echo "✗ PyInstaller failed — see /tmp/mmt-pyinstaller.log"; exit 1; }
fi
[[ -x "$REPO/packaging/dist/backend/MooTraderBackend" ]] \
    || { echo "✗ no backend build at packaging/dist/backend — run without --skip-backend"; exit 1; }

# Fail the build rather than ship a backend with a missing hidden import: the
# alternative is discovering it when the user's scheduler dies mid-session.
echo "▸ checking the frozen dependency graph…"
"$REPO/packaging/dist/backend/MooTraderBackend" --import-check \
    || { echo "✗ frozen backend is missing modules (see above)"; exit 1; }

# ── 2. Swift shell ───────────────────────────────────────────────────────────
echo "▸ building the Swift shell…"
swift build -c release

# ── 3. assemble the bundle ───────────────────────────────────────────────────
APP="dist/MooTrader.app"

# Refuse to delete a bundle something is currently executing from. 2026-08-05: a
# bot had been launched out of dist/ (rather than /Applications), and the rm -rf
# below removed the binary from under the live process — the scheduler died
# mid-session while holding a position, silently. dist/ is build output and gets
# wiped every run, so anything running from here is by definition about to lose
# its executable; say so and stop instead of doing it.
APP_ABS="$(cd "$(dirname "$APP")" 2>/dev/null && pwd)/$(basename "$APP")"
if RUNNING="$(pgrep -fl "$APP_ABS/" 2>/dev/null)" && [[ -n "$RUNNING" ]]; then
    echo "✗ REFUSING TO BUILD — a process is running from $APP_ABS:"
    echo "$RUNNING" | sed 's/^/    /'
    echo
    echo "  Quit it first (the app's own Quit, or: pkill -f '$APP_ABS/')."
    echo "  dist/ is build output and is wiped on every build — for anything"
    echo "  long-running, copy the .app to /Applications and launch it there."
    exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/MooTraderApp "$APP/Contents/MacOS/"
cp Resources/Info.plist "$APP/Contents/"
cp Resources/MooTrader.icns "$APP/Contents/Resources/"
cp -R "$REPO/packaging/dist/backend" "$APP/Contents/Resources/backend"

# ── 4. refuse to ship secrets ────────────────────────────────────────────────
# .env holds live API keys and data/ holds real trade history. Nothing in the
# spec bundles them, but this build output gets published to a public GitHub
# release — so verify rather than assume.
echo "▸ scanning the bundle for secrets…"
LEAKS="$(find "$APP" \( -name '.env' -o -name '*.db' -o -name 'account.json' \
        -o -name 'open_trades.json' -o -name '*.log' \) -print)"
if [[ -n "$LEAKS" ]]; then
    echo "✗ REFUSING TO BUILD — private files ended up in the bundle:"
    echo "$LEAKS" | sed 's/^/    /'
    rm -rf "$APP"
    exit 1
fi

# ── 5. ad-hoc signature ──────────────────────────────────────────────────────
# Inside-out: PyInstaller already ad-hoc signed its own binaries, but the copy
# above invalidates the outer seal, so re-sign the executables and then the
# bundle as a whole.
codesign --force -s - "$APP/Contents/Resources/backend/MooTraderBackend"
codesign --force -s - "$APP/Contents/MacOS/MooTraderApp"
codesign --force -s - "$APP"
codesign --verify --deep --strict "$APP" \
    || { echo "✗ signature verification failed"; exit 1; }

echo "✓ built $APP  ($(du -sh "$APP" | cut -f1))"
