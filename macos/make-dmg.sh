#!/bin/bash
# Package dist/MooTrader.app into a drag-to-Applications DMG for a GitHub
# release. Run ./build.sh first.
#
# The app is ad-hoc signed — there is no Developer ID certificate on this
# machine — so it is NOT notarized and Gatekeeper will refuse the first launch
# with "Apple could not verify…". That is expected: the user opens it once via
# System Settings → Privacy & Security → "Open Anyway". The release notes have
# to say so, or the download looks broken.
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/MooTrader.app"
[[ -d "$APP" ]] || { echo "✗ no $APP — run ./build.sh first"; exit 1; }

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
DMG="dist/MooTrader-${VERSION}-arm64.dmg"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # drag-here target

rm -f "$DMG"
hdiutil create \
    -volname "MooMoo Trader $VERSION" \
    -srcfolder "$STAGE" \
    -fs HFS+ \
    -format UDZO \
    -quiet \
    "$DMG"

echo "✓ $DMG  ($(du -sh "$DMG" | cut -f1))"
echo "  shasum: $(shasum -a 256 "$DMG" | cut -d' ' -f1)"
