import AVFoundation
import AppKit
import CryptoKit
import Foundation

/// Everything the window shows: the source, the compiled song, render and playback state.
@MainActor
final class StudioModel: ObservableObject {
    @Published var source: String = "" { didSet { if source != oldValue { dirty = true; scheduleCompile() } } }
    @Published var fileURL: URL?
    @Published var dirty = false
    @Published var score: ScoreData?
    @Published var error: ErrorData?
    @Published var compiling = false
    @Published var rendering = false
    @Published var renderStatus = ""
    @Published var isPlaying = false
    @Published var selectedTrack: String?
    @Published var jumpToLine: Int?
    @Published var pixelsPerBar: CGFloat = 26
    /// Vertical zoom: multiplies every lane's height (notes and drum rows stretch with it).
    @Published var laneScale: CGFloat = 1
    @Published var followPlayhead = true

    private var compileTask: Task<Void, Never>?
    private var renderProcess: Process?
    private var player: AVAudioPlayer?
    private let playerDelegate = PlayerDelegate()
    private var playerSourceHash: String?
    /// Bar position when not playing (cursor), and the bar playback started from.
    @Published var cursorBar: Double = 0

    // MARK: files

    func open(_ url: URL) {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return }
        stop()
        fileURL = url
        source = text
        dirty = false
        UserDefaults.standard.set(url.path, forKey: "lastFile")
        compileNow()
    }

    func save() {
        guard let url = fileURL else { saveAs(); return }
        try? source.write(to: url, atomically: true, encoding: .utf8)
        dirty = false
    }

    func saveAs() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.init(filenameExtension: "pulse") ?? .plainText]
        panel.nameFieldStringValue = fileURL?.lastPathComponent ?? "song.pulse"
        if panel.runModal() == .OK, let url = panel.url {
            fileURL = url
            save()
        }
    }

    func showOpenPanel() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.init(filenameExtension: "pulse") ?? .plainText]
        if let dir = fileURL?.deletingLastPathComponent() { panel.directoryURL = dir }
        if panel.runModal() == .OK, let url = panel.url { open(url) }
    }

    func newSong() {
        stop()
        fileURL = nil
        source = Self.template
        dirty = true
    }

    /// Path handed to the CLI so relative sample paths resolve next to the document.
    private var cliPath: String {
        fileURL?.path ?? (FileManager.default.homeDirectoryForCurrentUser.path + "/untitled.pulse")
    }

    // MARK: compile

    private func scheduleCompile() {
        compileTask?.cancel()
        compileTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled else { return }
            await self?.compile()
        }
    }

    func compileNow() {
        compileTask?.cancel()
        compileTask = Task { [weak self] in await self?.compile() }
    }

    private func compile() async {
        compiling = true
        let src = source
        let path = cliPath
        let result = await Task.detached { () -> Data? in
            CLI.run(["score", path, "--stdin"], stdin: src).stdout
        }.value
        compiling = false
        guard let data = result, !data.isEmpty else {
            error = ErrorData(message: "could not run the pulsar CLI at \(CLI.executable)", line: nil, column: nil,
                              hint: "set PULSAR_BIN or reinstall", text: "pulsar CLI not found: \(CLI.executable)")
            return
        }
        do {
            let s = try JSONDecoder().decode(ScoreData.self, from: data)
            if s.ok {
                score = s
                error = nil
            } else {
                error = s.error
            }
        } catch {
            self.error = ErrorData(message: "bad output from pulsar: \(error)", line: nil, column: nil, hint: nil,
                                   text: String(data: data, encoding: .utf8) ?? "")
        }
    }

    // MARK: render & playback

    private var sourceHash: String {
        let digest = SHA256.hash(data: Data((cliPath + "\n" + source).utf8))
        return digest.map { String(format: "%02x", $0) }.joined().prefix(24).description
    }

    private var cacheDir: URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("PulsarStudio", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    var renderIsCurrent: Bool { playerSourceHash == sourceHash && player != nil }

    func togglePlay() {
        if isPlaying { pause() } else { play() }
    }

    func play() {
        guard error == nil, score != nil else { NSSound.beep(); return }
        let hash = sourceHash
        let wav = cacheDir.appendingPathComponent("\(hash).wav")
        if playerSourceHash == hash, let p = player {
            start(p)
            return
        }
        if FileManager.default.fileExists(atPath: wav.path) {
            load(wav, hash: hash)
            return
        }
        render(to: wav, hash: hash)
    }

    private func render(to wav: URL, hash: String) {
        renderProcess?.terminate()
        rendering = true
        renderStatus = "rendering…"
        let src = source
        let path = cliPath
        let tmp = wav.appendingPathExtension("part.wav")
        let proc = CLI.start(["render", path, "--stdin", "-o", tmp.path], stdin: src) { [weak self] line in
            Task { @MainActor in
                if line.contains("rendered ") || line.contains("rendering ") {
                    // show the file name, never the full local path
                    var text = line.trimmingCharacters(in: .whitespaces)
                    if let range = text.range(of: path) { text.replaceSubrange(range, with: (path as NSString).lastPathComponent) }
                    self?.renderStatus = text
                }
            }
        } completion: { [weak self] status, stderr in
            Task { @MainActor in
                guard let self else { return }
                self.rendering = false
                if status == 0 {
                    try? FileManager.default.removeItem(at: wav)
                    try? FileManager.default.moveItem(at: tmp, to: wav)
                    self.renderStatus = "rendered"
                    self.load(wav, hash: hash)
                } else if status != 15 {
                    self.renderStatus = "render failed"
                    self.error = ErrorData(message: "render failed", line: nil, column: nil, hint: nil,
                                           text: stderr.isEmpty ? "render failed" : stderr)
                }
            }
        }
        renderProcess = proc
    }

    private func load(_ wav: URL, hash: String) {
        guard let p = try? AVAudioPlayer(contentsOf: wav) else { renderStatus = "can't open audio"; return }
        playerDelegate.onFinish = { [weak self] in
            Task { @MainActor in self?.isPlaying = false; self?.cursorBar = 0 }
        }
        p.delegate = playerDelegate
        p.prepareToPlay()
        player = p
        playerSourceHash = hash
        start(p)
    }

    private func start(_ p: AVAudioPlayer) {
        guard let bs = score?.bar_seconds else { return }
        p.currentTime = min(max(0, cursorBar * bs), max(0, p.duration - 0.05))
        p.play()
        isPlaying = true
    }

    func pause() {
        if let p = player, isPlaying, let bs = score?.bar_seconds {
            cursorBar = p.currentTime / bs
            p.pause()
        }
        isPlaying = false
    }

    func stop() {
        player?.stop()
        isPlaying = false
        cursorBar = 0
    }

    func seek(toBar bar: Double) {
        cursorBar = max(0, bar)
        if isPlaying, let p = player, let bs = score?.bar_seconds {
            p.currentTime = cursorBar * bs
        }
    }

    /// Current playhead in bars (read every frame by the arrangement's timeline view).
    /// The end of playback is reported by the player's delegate, never inferred here:
    /// AVAudioPlayer starts asynchronously, so `isPlaying` can read false for the first
    /// frames after `play()`, and treating that as "finished" froze the playhead while
    /// the audio kept going.
    func playheadBar() -> Double {
        if isPlaying, let p = player, let bs = score?.bar_seconds {
            return p.currentTime / bs
        }
        return cursorBar
    }

    func renderOnly() {
        let hash = sourceHash
        render(to: cacheDir.appendingPathComponent("\(hash).wav"), hash: hash)
    }

    static let template = """
    // A new Pulsar song. Press ⌘R to render and play.
    tempo 122
    key "A minor"

    track kick = drums("house")
    track bass = synth("bass", duck: 0.5)
    track pad  = synth("pad", reverb: 0.4, duck: 0.6)

    section intro 8 {
      play kick "bd*4, [~ hh]*4"
      play pad "<Am9 Fmaj7 C G>".voicing("smooth")
    }

    section groove 16 extends intro {
      play kick "bd*4, ~ cp ~ cp, [~ hh]*4"
      play bass "<a1 f1 c2 g1>".struct("x ~ ~ x ~ ~ x ~")
      automate pad.cutoff 0.3 -> 0.6
    }

    arrange intro, groove
    """
}


/// Forwards AVAudioPlayer's "finished" callback (the model itself isn't an NSObject).
final class PlayerDelegate: NSObject, AVAudioPlayerDelegate {
    var onFinish: (() -> Void)?
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) { onFinish?() }
}
