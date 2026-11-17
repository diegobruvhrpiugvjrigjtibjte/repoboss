// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "RepoBossMac",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "RepoBossMac", path: "Sources/RepoBossMac")
    ]
)
