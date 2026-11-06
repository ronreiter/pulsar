import AVFoundation
import AppKit
import SwiftUI

/// One result of `pulsar library search --json`.
struct LibraryItem: Decodable, Identifiable, Hashable {
    var id: String { ref }
    let ref: String
    let title: String
    let source: String
    let license: String
    let creator: String
    let url: String
    let duration: Double?
    let name: String
    let track: String
    let play: String
}

/// The open sound library: search openly licensed samples and sound effects, preview them, and
/// insert the code that uses one into the song (a sampler track, plus a play line at the cursor).
@MainActor
final class SoundBrowserModel: ObservableObject {
    let studio: StudioModel
    @Published var query = ""
    @Published var source = "all"
    @Published var results: [LibraryItem] = []
    @Published var selection: LibraryItem.ID?
    @Published var busy = false
    @Published var status = "Search openly licensed sounds: drums, hits, instruments, foley, nature..."
    @Published var previewing: String?
    private var player: AVAudioPlayer?
    private var searchID = 0

    init(studio: StudioModel) { self.studio = studio }

    func search() {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        searchID += 1
        let id = searchID
        busy = true
        status = "searching for \(q)…"
        var args = ["library", "search", q, "--json", "--limit", "60"]
        if source != "all" { args += ["--source", source] }
        Task.detached {
            let r = CLI.run(args, stdin: "")
            let items = r.stdout.flatMap { try? JSONDecoder().decode([LibraryItem].self, from: $0) }
            await MainActor.run {
                guard id == self.searchID else { return }
                self.busy = false
                if let items = items {
                    self.results = items
                    self.selection = items.first?.id
                    self.status = items.isEmpty ? "nothing found for \(q)" :
                        "\(items.count) sounds -- double-click or ⌘↩ to insert, space to preview"
                } else {
                    self.results = []
                    self.status = "search failed: " + (r.stderr.isEmpty ? "no response" : r.stderr)
                }
            }
        }
    }

    var selected: LibraryItem? { results.first { $0.id == selection } }

    func preview(_ item: LibraryItem? = nil) {
        guard let it = item ?? selected else { return }
        if previewing == it.ref, player?.isPlaying == true {
            player?.stop()
            previewing = nil
            return
        }
        player?.stop()
        previewing = it.ref
        status = "fetching \(it.title)…"
        Task.detached {
            let r = CLI.run(["library", "fetch", it.ref, "--json"], stdin: "")
            struct Fetched: Decodable { let path: String? }
            let path = r.stdout.flatMap { try? JSONDecoder().decode(Fetched.self, from: $0) }?.path
            await MainActor.run {
                guard self.previewing == it.ref else { return }
                guard let path = path, let p = try? AVAudioPlayer(contentsOf: URL(fileURLWithPath: path)) else {
                    self.status = "couldn't fetch \(it.title): \(r.stderr)"
                    self.previewing = nil
                    return
                }
                self.player = p
                p.play()
                self.status = "\(it.title) -- \(it.license) -- \(it.creator)"
            }
        }
    }

    /// Put the sound into the song: a sampler track after the other tracks, and -- if the cursor
    /// is inside a section -- a play line there.
    func insert(_ item: LibraryItem? = nil) {
        guard let it = item ?? selected else { return }
        var src = studio.source
        let trackRe = try! NSRegularExpression(pattern: "^track\\s+(\\w+)", options: .anchorsMatchLines)
        let taken = Set(trackRe.matches(in: src, range: NSRange(location: 0, length: (src as NSString).length))
            .map { (src as NSString).substring(with: $0.range(at: 1)) })
        var name = it.name, n = 2
        while taken.contains(name) { name = "\(it.name)_\(n)"; n += 1 }
        let trackLine = it.track.replacingOccurrences(of: "track \(it.name) ", with: "track \(name) ")
        let playLine = it.play.replacingOccurrences(of: "play \(it.name) ", with: "play \(name) ")

        let cursor = min(studio.cursorOffset, src.utf16.count)
        let ns = src as NSString
        // the play line goes at the cursor's line if that line is inside a { ... } block
        let before = ns.substring(to: cursor)
        let depth = before.filter { $0 == "{" }.count - before.filter { $0 == "}" }.count
        var playAt: Int?
        if depth > 0 {
            let line = ns.lineRange(for: NSRange(location: cursor, length: 0))
            playAt = line.location + line.length
            if line.length > 0, ns.substring(with: line).hasSuffix("\n") == false { src += "\n"; playAt = (src as NSString).length }
        }
        // the track goes after the last top-level `track ...` declaration (and its continuation lines)
        var lines = src.components(separatedBy: "\n")
        var trackIdx: Int?
        var i = 0
        while i < lines.count {
            if lines[i].hasPrefix("track ") {
                var j = i + 1
                while j < lines.count, let f = lines[j].first, f == " " || f == "\t",
                      !lines[j].trimmingCharacters(in: .whitespaces).isEmpty { j += 1 }
                trackIdx = j
                i = j
            } else { i += 1 }
        }
        if let at = playAt {
            let indent = String(lines.first { $0.hasPrefix("  play ") }?.prefix { $0 == " " } ?? "  ")
            let prefix = (src as NSString).substring(to: at)
            let lineNo = prefix.components(separatedBy: "\n").count - 1
            lines.insert(indent + playLine, at: lineNo)
            if let t = trackIdx, t > lineNo { trackIdx = t + 1 }
        }
        let tIndex = trackIdx ?? 0
        lines.insert(trackLine, at: tIndex)
        studio.source = lines.joined(separator: "\n")
        status = "added track \(name)" + (playAt != nil ? " and a play line at the cursor" :
                                          " -- put `\(playLine)` inside a section to hear it")
    }
}

struct SoundBrowserView: View {
    @ObservedObject var model: SoundBrowserModel

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                TextField("kick, snare, glass, whoosh, thunder, marimba…", text: $model.query)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { model.search() }
                Picker("", selection: $model.source) {
                    Text("All sources").tag("all")
                    Text("VCSL (CC0)").tag("vcsl")
                    Text("Wikimedia Commons").tag("commons")
                    Text("Freesound").tag("freesound")
                }
                .frame(width: 170)
                Button("Search") { model.search() }.disabled(model.busy)
            }
            .padding(10)
            .background(Color(red: 0.11, green: 0.115, blue: 0.13))
            List(model.results, selection: $model.selection) { it in
                HStack(spacing: 10) {
                    Button { model.preview(it) } label: {
                        Image(systemName: model.previewing == it.ref ? "stop.circle.fill" : "play.circle")
                    }
                    .buttonStyle(.borderless)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(it.title).lineLimit(1)
                        Text(it.ref).font(.system(size: 10, design: .monospaced)).foregroundColor(.secondary).lineLimit(1)
                    }
                    Spacer()
                    if let d = it.duration { Text(String(format: "%.1fs", d)).foregroundColor(.secondary).font(.system(size: 11)) }
                    Text(it.license).font(.system(size: 10, weight: .medium))
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(it.license.contains("CC0") || it.license.lowercased().contains("public") ?
                                    Color.green.opacity(0.25) : Color.orange.opacity(0.25))
                        .cornerRadius(4)
                    Text(it.source).font(.system(size: 10)).foregroundColor(.secondary).frame(width: 64, alignment: .trailing)
                }
                .contentShape(Rectangle())
                .onTapGesture(count: 2) { model.insert(it) }
            }
            HStack {
                Text(model.status).lineLimit(1).foregroundColor(.secondary)
                Spacer()
                Button("Preview") { model.preview() }.keyboardShortcut(.space, modifiers: []).disabled(model.selected == nil)
                Button("Insert into Song") { model.insert() }
                    .keyboardShortcut(.return, modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(model.selected == nil)
            }
            .font(.system(size: 12))
            .padding(10)
            .background(Color(red: 0.11, green: 0.115, blue: 0.13))
        }
        .frame(minWidth: 640, minHeight: 420)
    }
}

@MainActor
enum SoundBrowserWindow {
    private static var window: NSWindow?
    private static var model: SoundBrowserModel?

    static func show(studio: StudioModel) {
        if let w = window { w.makeKeyAndOrderFront(nil); return }
        let m = SoundBrowserModel(studio: studio)
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 520),
                         styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "Sound Library"
        w.contentView = NSHostingView(rootView: SoundBrowserView(model: m))
        w.isReleasedWhenClosed = false
        w.center()
        w.setFrameAutosaveName("SoundLibrary")
        window = w
        model = m
        w.makeKeyAndOrderFront(nil)
    }

    /// For the self-test: the open browser's model.
    static var current: SoundBrowserModel? { model }
}

/// PULSAR_LIBRARY_SELFTEST="query" with PULSAR_LIBRARY_SELFTEST_OUT=dir: opens the library, searches, previews
/// the first result, inserts it with the cursor inside the first section, and writes snapshots and the song.
@MainActor
enum SoundBrowserSelfTest {
    private static var started = false
    static func runIfRequested(studio: StudioModel) {
        let env = ProcessInfo.processInfo.environment
        guard !started, let q = env["PULSAR_LIBRARY_SELFTEST"], let out = env["PULSAR_LIBRARY_SELFTEST_OUT"] else { return }
        started = true
        let dir = URL(fileURLWithPath: out)
        func log(_ s: String) {
            let f = dir.appendingPathComponent("log.txt")
            let line = s + "\n"
            if let h = try? FileHandle(forWritingTo: f) { h.seekToEndOfFile(); h.write(Data(line.utf8)); try? h.close() }
            else { try? line.write(to: f, atomically: true, encoding: .utf8) }
        }
        func waitFor(_ cond: @escaping () -> Bool, _ then: @escaping () -> Void, tries: Int = 300) {
            if cond() || tries == 0 { then(); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { waitFor(cond, then, tries: tries - 1) }
        }
        waitFor({ studio.score != nil }) {
            SoundBrowserWindow.show(studio: studio)
            guard let m = SoundBrowserWindow.current else { log("no browser"); return }
            m.query = q
            m.search()
            waitFor({ !m.busy }) {
                log("results: \(m.results.count); first: \(m.results.first?.ref ?? "-") \(m.results.first?.license ?? "")")
                NoteEditorSelfTest.snapshot(NSApp.windows.first { $0.title == "Sound Library" }!, dir.appendingPathComponent("1-results.png"))
                guard let first = m.results.first else { log("done"); return }
                m.preview(first)
                waitFor({ m.status.contains(first.license) || m.status.hasPrefix("couldn't") }) {
                    log("preview: \(m.status)")
                    // the cursor at the end of the first line that starts a section
                    let ns = studio.source as NSString
                    let r = ns.range(of: "section ")
                    let line = ns.lineRange(for: NSRange(location: r.location, length: 0))
                    studio.cursorOffset = line.location + line.length - 1
                    m.insert(first)
                    log("insert: \(m.status)")
                    try? studio.source.write(to: dir.appendingPathComponent("song.pulse"), atomically: true, encoding: .utf8)
                    waitFor({ !studio.compiling }) {
                        log("compiles: \(studio.error == nil ? "yes" : studio.error!.message)")
                        NoteEditorSelfTest.snapshot(NSApp.windows.first { $0.title == "Sound Library" }!, dir.appendingPathComponent("2-after.png"))
                        log("done")
                    }
                }
            }
        }
    }
}
