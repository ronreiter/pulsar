// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "PulsarStudio",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "PulsarStudio", path: "Sources/PulsarStudio")
    ]
)
