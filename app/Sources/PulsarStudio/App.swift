import AppKit
import SwiftUI

@main
struct PulsarStudioApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = StudioModel()

    var body: some Scene {
        WindowGroup("Pulsar Studio") {
            ContentView(model: model)
                .frame(minWidth: 1100, minHeight: 640)
                .onAppear { openInitialFile(); NoteEditorSelfTest.runIfRequested(studio: model) }
                .onOpenURL { model.open($0) }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1600, height: 950)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Song") { model.newSong() }.keyboardShortcut("n")
                Button("Open…") { model.showOpenPanel() }.keyboardShortcut("o")
            }
            CommandGroup(replacing: .saveItem) {
                Button("Save") { model.save() }.keyboardShortcut("s")
                Button("Save As…") { model.saveAs() }.keyboardShortcut("s", modifiers: [.command, .shift])
            }
            CommandGroup(after: .textEditing) {
                Divider()
                Button("Find…") { pulsarFind(.showFindInterface) }.keyboardShortcut("f")
                Button("Find and Replace…") { pulsarFind(.showReplaceInterface) }.keyboardShortcut("f", modifiers: [.command, .option])
                Button("Find Next") { pulsarFind(.nextMatch) }.keyboardShortcut("g")
                Button("Find Previous") { pulsarFind(.previousMatch) }.keyboardShortcut("g", modifiers: [.command, .shift])
                Button("Use Selection for Find") { pulsarFind(.setSearchString) }.keyboardShortcut("e")
            }
            CommandMenu("Song") {
                Button("Play / Pause") { model.togglePlay() }.keyboardShortcut("r")
                Button("Stop") { model.stop() }.keyboardShortcut(".")
                Button("Re-render") { model.renderOnly() }.keyboardShortcut("r", modifiers: [.command, .shift])
                Divider()
                Button("Zoom In") { model.pixelsPerBar = min(400, model.pixelsPerBar * 1.25) }.keyboardShortcut("=")
                Button("Zoom Out") { model.pixelsPerBar = max(4, model.pixelsPerBar / 1.25) }.keyboardShortcut("-")
                Button("Taller Lanes") { model.laneScale = min(4, model.laneScale * 1.25) }
                    .keyboardShortcut("=", modifiers: [.command, .option])
                Button("Shorter Lanes") { model.laneScale = max(0.6, model.laneScale / 1.25) }
                    .keyboardShortcut("-", modifiers: [.command, .option])
                Button("Reset Zoom") { model.pixelsPerBar = 26; model.laneScale = 1 }.keyboardShortcut("0")
                Toggle("Follow Playhead", isOn: $model.followPlayhead)
            }
        }
    }

    /// The song the app opens with (override with PULSAR_DEFAULT_SONG).
    static var defaultSong: String {
        if let env = ProcessInfo.processInfo.environment["PULSAR_DEFAULT_SONG"], !env.isEmpty { return env }
        if let cli = Bundle.main.object(forInfoDictionaryKey: "PulsarCLI") as? String {
            let root = URL(fileURLWithPath: cli).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            return root.appendingPathComponent("examples/deep_house_agent.pulse").path
        }
        return FileManager.default.homeDirectoryForCurrentUser.path + "/GitHub/pulsar/examples/deep_house_agent.pulse"
    }

    private func openInitialFile() {
        guard model.fileURL == nil, model.source.isEmpty else { return }
        let args = CommandLine.arguments.dropFirst().filter { $0.hasSuffix(".pulse") }
        if let path = args.first {
            model.open(URL(fileURLWithPath: path))
        } else if FileManager.default.fileExists(atPath: Self.defaultSong) {
            model.open(URL(fileURLWithPath: Self.defaultSong))
        } else if let last = UserDefaults.standard.string(forKey: "lastFile"), FileManager.default.fileExists(atPath: last) {
            model.open(URL(fileURLWithPath: last))
        } else {
            model.newSong()
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        if ProcessInfo.processInfo.environment["PULSAR_EDITOR_SELFTEST"] == nil { NSApp.activate(ignoringOtherApps: true) }
        NSApp.appearance = NSAppearance(named: .darkAqua)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

struct ContentView: View {
    @ObservedObject var model: StudioModel

    var body: some View {
        VStack(spacing: 0) {
            TransportBar(model: model)
            HSplitView {
                VStack(spacing: 0) {
                    EditorView(text: $model.source, errorLine: model.error?.line, jumpToLine: $model.jumpToLine)
                    if let err = model.error {
                        ErrorBanner(error: err) { if let l = err.line { model.jumpToLine = l } }
                    }
                }
                .frame(minWidth: 320, idealWidth: 460)
                ArrangementView(model: model)
                    .frame(minWidth: 500)
            }
        }
        .background(Palette.bg)
        .preferredColorScheme(.dark)
    }
}

struct ErrorBanner: View {
    let error: ErrorData
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.red)
                VStack(alignment: .leading, spacing: 2) {
                    Text(error.line.map { "line \($0): \(error.message)" } ?? error.message)
                        .font(.system(size: 12, weight: .medium)).foregroundColor(.white)
                    if let h = error.hint {
                        Text(h).font(.system(size: 11)).foregroundColor(.white.opacity(0.65))
                    }
                }
                Spacer()
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(red: 0.32, green: 0.1, blue: 0.11))
        }
        .buttonStyle(.plain)
    }
}

struct TransportBar: View {
    @ObservedObject var model: StudioModel

    var body: some View {
        HStack(spacing: 14) {
            Text(model.fileURL?.lastPathComponent ?? "Untitled")
                .font(.system(size: 13, weight: .semibold)).foregroundColor(.white.opacity(0.9))
                + Text(model.dirty ? "  •" : "").foregroundColor(.orange)
            Spacer()
            Button { model.stop() } label: { Image(systemName: "stop.fill") }
                .buttonStyle(TransportButton())
                .help("Stop (⌘.)")
            Button { model.togglePlay() } label: {
                Image(systemName: model.rendering ? "hourglass" : (model.isPlaying ? "pause.fill" : "play.fill"))
            }
            .buttonStyle(TransportButton(accent: true))
            .help("Render & play (⌘R)")
            .disabled(model.rendering)
            TimelineView(.animation(minimumInterval: 0.05, paused: !model.isPlaying)) { _ in
                Text(positionText()).font(.system(size: 13, weight: .medium, design: .monospaced))
                    .foregroundColor(Color(red: 1, green: 0.85, blue: 0.3))
                    .frame(width: 150)
            }
            Spacer()
            if model.rendering {
                ProgressView().controlSize(.small)
            }
            Text(statusText()).font(.system(size: 11)).foregroundColor(.white.opacity(0.55)).lineLimit(1)
                .frame(maxWidth: 280, alignment: .trailing)
            ZoomControls(model: model)
            Toggle("Follow", isOn: $model.followPlayhead).toggleStyle(.switch).controlSize(.mini)
                .font(.system(size: 11))
        }
        .padding(.horizontal, 14)
        .padding(.leading, 64)
        .frame(height: 40)
        .background(Color(red: 0.1, green: 0.105, blue: 0.12))
    }

    private func positionText() -> String {
        let bar = model.playheadBar()
        let secs = bar * (model.score?.bar_seconds ?? 2)
        let b = Int(bar) + 1, beat = Int((bar - floor(bar)) * 4) + 1
        return String(format: "%3d.%d   %d:%04.1f", b, beat, Int(secs) / 60, secs.truncatingRemainder(dividingBy: 60))
    }

    private func statusText() -> String {
        if model.rendering { return model.renderStatus }
        if model.error != nil { return "fix the error to render" }
        if model.compiling { return "compiling…" }
        guard let s = model.score else { return "" }
        let notes = (s.tracks ?? []).reduce(0) { $0 + $1.notes.count }
        let len = s.seconds ?? 0
        return "\((s.tracks ?? []).count) tracks · \(notes) notes · \(Int(len) / 60):\(String(format: "%02d", Int(len) % 60))"
            + (model.renderIsCurrent ? " · rendered" : "")
    }
}

struct TransportButton: ButtonStyle {
    var accent = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .bold))
            .foregroundColor(accent ? .black : .white)
            .frame(width: 34, height: 26)
            .background(RoundedRectangle(cornerRadius: 6).fill(accent ? Color(red: 1, green: 0.78, blue: 0.3) : Color.white.opacity(0.12)))
            .opacity(configuration.isPressed ? 0.7 : 1)
    }
}


/// Horizontal (time) and vertical (lane height) zoom buttons.
struct ZoomControls: View {
    @ObservedObject var model: StudioModel

    var body: some View {
        HStack(spacing: 4) {
            zoomButton("arrow.left.and.right", "−") { model.pixelsPerBar = max(4, model.pixelsPerBar / 1.25) }
                .help("Zoom out in time (⌘-)")
            zoomButton("arrow.left.and.right", "+") { model.pixelsPerBar = min(400, model.pixelsPerBar * 1.25) }
                .help("Zoom in in time (⌘=, pinch, ⌘-scroll)")
            Divider().frame(height: 16)
            zoomButton("arrow.up.and.down", "−") { model.laneScale = max(0.6, model.laneScale / 1.25) }
                .help("Shorter lanes (⌥⌘-)")
            zoomButton("arrow.up.and.down", "+") { model.laneScale = min(4, model.laneScale * 1.25) }
                .help("Taller lanes (⌥⌘=, ⌥-pinch, ⌥-scroll)")
        }
    }

    private func zoomButton(_ icon: String, _ sign: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 1) {
                Image(systemName: icon).font(.system(size: 9, weight: .semibold))
                Text(sign).font(.system(size: 11, weight: .bold))
            }
            .foregroundColor(.white.opacity(0.8))
            .frame(width: 30, height: 20)
            .background(RoundedRectangle(cornerRadius: 5).fill(Color.white.opacity(0.1)))
        }
        .buttonStyle(.plain)
    }
}
