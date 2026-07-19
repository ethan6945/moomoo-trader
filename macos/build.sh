#!/bin/bash
# Build dist/MooTrader.app from the SwiftPM package — no Xcode needed, only
# Command Line Tools. Ad-hoc signed (first launch: right-click → Open).
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release

APP="dist/MooTrader.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/MooTraderApp "$APP/Contents/MacOS/"
cp Resources/Info.plist "$APP/Contents/"
cp Resources/MooTrader.icns "$APP/Contents/Resources/"
codesign --force -s - "$APP"

echo "✓ built $APP"
