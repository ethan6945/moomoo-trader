// swift-tools-version: 5.9
// Native macOS shell for Moo Trader. Built with `swift build` (no Xcode needed);
// `./build.sh` assembles dist/MooTrader.app.
import PackageDescription

let package = Package(
    name: "MooTraderApp",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "MooTraderApp",
            path: "Sources/MooTraderApp"
        )
    ]
)
