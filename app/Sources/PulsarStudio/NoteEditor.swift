import AppKit
import Combine
import SwiftUI

// MARK: data

/// One band as `pulsar clip` reads it: a track's notes in one section.
struct BandData: Decodable {
    let ok: Bool
    let section: String?
    let track: String?
    let bars: Double?
    let beats_per_bar: Int?
    let kind: String?  // notes | drums | hits
    let editable: Bool?
    let reason: String?
    let inherited: Bool?
    let flats: Bool?
    let notes: [BandNote]?
    let error: String?
    let hint: String?
}

struct BandNote: Codable {
    var beat: String
    var len: String
    var vel: Double
    var flags: [String]
    var note: String
    var pitch: Double?
}

struct WriteResult: Decodable {
    let ok: Bool
    let source: String?
    let error: String?
    let hint: String?
}

/// A note while it's being edited. Times are in beats from the start of the section.
struct EditNote: Identifiable, Equatable {
    let id = UUID()
    var beat: Double
    var len: Double
    var row: Int  // index into the editor's rows (pitch rows go from high to low)
    var vel: Double
    var flags: [String]
    /// The exact text the note was read with, kept while it's unchanged (so triplets stay 1/3, not 0.333).
    var beatText: String?
    var lenText: String?
}

/// A beat value as the clip format writes it: a short decimal, or a fraction for triplets and other odd grids.
func beatText(_ x: Double) -> String {
    for d in [1, 2, 4, 8, 16, 32, 64, 3, 6, 12, 24, 48, 96, 5, 10] {
        let n = (x * Double(d)).rounded()
        if abs(x * Double(d) - n) < 1e-6 {
            if d & (d - 1) == 0 || d == 5 || d == 10 {
                var s = String(format: "%.6f", n / Double(d))
                while s.hasSuffix("0") { s.removeLast() }
                if s.hasSuffix(".") { s.removeLast() }
                return s == "-0" ? "0" : s
            }
            let g = gcd(Int(n), d)
            return "\(Int(n) / g)/\(d / g)"
        }
    }
    return String(format: "%.6f", x)
}

private func gcd(_ a: Int, _ b: Int) -> Int { b == 0 ? max(1, abs(a)) : gcd(b, a % b) }

private func parseBeats(_ s: String) -> Double {
    let parts = s.split(separator: "/")
    if parts.count == 2, let a = Double(parts[0]), let b = Double(parts[1]), b != 0 { return a / b }
    return Double(s) ?? 0
}

let noteNamesSharp = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]
let noteNamesFlat = ["c", "db", "d", "eb", "e", "f", "gb", "g", "ab", "a", "bb", "b"]
let allDrums = ["bd", "sd", "cp", "rim", "lt", "mt", "ht", "hh", "oh", "sh", "cb", "pc", "rd", "cr"]

/// The editor's state: rows, notes, selection, grid and an undo history.
@MainActor
final class NoteEditorModel: ObservableObject {
    let studio: StudioModel
    let section: String
    let track: String
    let startBar: Double  // where the edited instance of the section starts in the song
    @Published var bars: Double = 4
    @Published var kind = "notes"
    @Published var rows: [String] = []  // labels, top to bottom
    @Published var rowPitch: [Int] = []  // MIDI per row for note lanes
    @Published var notes: [EditNote] = [] {
        didSet {
            guard notes != oldValue else { return }
            changed = notes != pristine
            if readOnlyReason == nil { status = "\(notes.count) notes" + (changed ? " · not applied yet" : "") }
        }
    }
    @Published var selection: Set<UUID> = []
    @Published var grid: Double = 0.25  // in beats
    @Published var changed = false
    @Published var status = ""
    @Published var readOnlyReason: String?
    @Published var inherited = false
    var flats = false
    private var pristine: [EditNote] = []
    private var undoStack: [[EditNote]] = []
    private var redoStack: [[EditNote]] = []
    var lastLength: Double = 1

    init(studio: StudioModel, section: String, track: String, startBar: Double) {
        self.studio = studio
        self.section = section
        self.track = track
        self.startBar = startBar
    }

    var beatsTotal: Double { bars * 4 }

    // MARK: loading and applying

    func load() {
        status = "reading \(track) in \(section)…"
        let src = studio.source
        let path = studio.fileURL?.path ?? "song.pulse"
        let args = ["clip", path, "--stdin", "--section", section, "--track", track, "--at", String(startBar)]
        Task.detached {
            let r = CLI.run(args, stdin: src)
            let band = r.stdout.flatMap { try? JSONDecoder().decode(BandData.self, from: $0) }
            await MainActor.run {
                guard let band = band, band.ok else {
                    self.status = band?.error ?? (r.stderr.isEmpty ? "couldn't read the band" : r.stderr)
                    self.readOnlyReason = self.status
                    return
                }
                self.setBand(band)
            }
        }
    }

    private func setBand(_ b: BandData) {
        bars = b.bars ?? 4
        kind = b.kind ?? "notes"
        flats = b.flats ?? false
        inherited = b.inherited ?? false
        readOnlyReason = (b.editable ?? false) ? nil : (b.reason ?? "this band can't be edited")
        let src = b.notes ?? []
        if kind == "notes" {
            let ps = src.compactMap { $0.pitch.map { Int($0.rounded()) } }
            var lo = (ps.min() ?? 60) - 7, hi = (ps.max() ?? 72) + 7
            if hi - lo < 24 { let mid = (hi + lo) / 2; lo = mid - 12; hi = mid + 12 }
            lo = max(12, lo); hi = min(115, hi)
            rowPitch = Array((lo...hi).reversed())
            rows = rowPitch.map { name($0) }
        } else if kind == "drums" {
            let used = Set(src.map { $0.note })
            rows = allDrums.filter { used.contains($0) } + used.subtracting(allDrums).sorted()
                + allDrums.filter { !used.contains($0) }
            rowPitch = []
        } else {
            let used = Set(src.map { $0.note })
            rows = used.isEmpty ? ["x"] : used.sorted()
            rowPitch = []
        }
        let ns = src.compactMap { n -> EditNote? in
            let row: Int?
            if kind == "notes", let p = n.pitch { row = rowPitch.firstIndex(of: Int(p.rounded())) } else { row = rows.firstIndex(of: n.note) }
            guard let r = row else { return nil }
            return EditNote(beat: parseBeats(n.beat), len: parseBeats(n.len), row: r, vel: n.vel, flags: n.flags,
                            beatText: n.beat, lenText: n.len)
        }
        pristine = ns
        notes = ns
        changed = false
        undoStack = []
        redoStack = []
        selection = []
        let lens = ns.map { $0.len }
        lastLength = lens.isEmpty ? 1 : lens.sorted()[lens.count / 2]
        status = readOnlyReason ?? "\(ns.count) notes"
    }

    func name(_ midi: Int) -> String {
        (flats ? noteNamesFlat : noteNamesSharp)[((midi % 12) + 12) % 12] + "\(midi / 12 - 1)"
    }

    private func token(for n: EditNote) -> String {
        kind == "notes" ? name(rowPitch[n.row]) : rows[n.row]
    }

    func apply(thenPlay: Bool = false) {
        guard readOnlyReason == nil else { NSSound.beep(); return }
        let out = notes.map { n in
            BandNote(beat: n.beatText ?? beatText(n.beat), len: n.lenText ?? beatText(n.len), vel: (n.vel * 100).rounded() / 100,
                     flags: n.flags, note: token(for: n), pitch: kind == "notes" ? Double(rowPitch[n.row]) : nil)
        }
        guard let data = try? JSONEncoder().encode(out) else { return }
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("pulsar-clip-\(UUID().uuidString).json")
        do { try data.write(to: tmp) } catch { status = "couldn't write \(tmp.path)"; return }
        let src = studio.source
        let path = studio.fileURL?.path ?? "song.pulse"
        let args = ["clip", path, "--stdin", "--section", section, "--track", track, "--write", tmp.path]
        status = "applying…"
        Task.detached {
            let r = CLI.run(args, stdin: src)
            try? FileManager.default.removeItem(at: tmp)
            let res = r.stdout.flatMap { try? JSONDecoder().decode(WriteResult.self, from: $0) }
            await MainActor.run {
                guard let res = res, res.ok, let new = res.source else {
                    self.status = "not applied: " + (res?.error ?? r.stderr)
                    NSSound.beep()
                    return
                }
                if self.studio.source != src {
                    self.status = "the song changed while applying; try again"
                    return
                }
                self.studio.source = new
                self.pristine = self.notes
                self.changed = false
                self.status = "applied to \(self.section) in the song (⌘Z in the code editor undoes it)"
                if thenPlay {
                    self.studio.seek(toBar: self.startBar)
                    if !self.studio.isPlaying { self.studio.play() }
                }
            }
        }
    }

    func revert() {
        record()
        notes = pristine
        selection = []
    }

    // MARK: editing (every change goes through `record` first, for undo)

    func record() {
        undoStack.append(notes)
        if undoStack.count > 200 { undoStack.removeFirst() }
        redoStack = []
    }

    func undo() {
        guard let last = undoStack.popLast() else { NSSound.beep(); return }
        redoStack.append(notes)
        notes = last
        selection = selection.filter { id in notes.contains { $0.id == id } }
    }

    func redo() {
        guard let next = redoStack.popLast() else { NSSound.beep(); return }
        undoStack.append(notes)
        notes = next
    }

    func snap(_ beat: Double) -> Double { (beat / grid).rounded(.down) * grid }

    func deleteSelected() {
        guard !selection.isEmpty else { return }
        record()
        notes.removeAll { selection.contains($0.id) }
        selection = []
    }

    func nudge(beats: Double, rows dr: Int) {
        guard !selection.isEmpty else { return }
        record()
        for i in notes.indices where selection.contains(notes[i].id) {
            let nb = max(0, min(beatsTotal - grid, notes[i].beat + beats))
            let nr = max(0, min(rows.count - 1, notes[i].row + dr))
            if nb != notes[i].beat { notes[i].beat = nb; notes[i].beatText = nil }
            notes[i].row = nr
        }
    }

    func setVelocity(_ v: Double, for ids: Set<UUID>) {
        for i in notes.indices where ids.contains(notes[i].id) { notes[i].vel = max(0.01, min(1, v)) }
    }

    func selectAll() { selection = Set(notes.map { $0.id }) }
}

// MARK: piano roll

/// The piano roll: an AppKit view so dragging, resizing and keys are exact.
final class PianoRollView: NSView {
    weak var model: NoteEditorModel?
    var color: NSColor = .systemOrange
    var pxPerBeat: CGFloat = 48
    var rowH: CGFloat = 16
    var scroll = CGPoint.zero  // x in beats * px, y in rows * px
    let keysW: CGFloat = 64, rulerH: CGFloat = 22, velH: CGFloat = 72

    private enum Drag { case none, move(start: CGPoint, orig: [UUID: (Double, Int)]), resize(start: CGPoint, orig: [UUID: Double]),
                        create(id: UUID, beat: Double), marquee(start: CGPoint, now: CGPoint), velocity }
    private var drag: Drag = .none

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    private var gridRect: NSRect { NSRect(x: keysW, y: rulerH, width: bounds.width - keysW, height: bounds.height - rulerH - velH) }
    private var velRect: NSRect { NSRect(x: keysW, y: bounds.height - velH, width: bounds.width - keysW, height: velH) }

    func x(_ beat: Double) -> CGFloat { keysW + CGFloat(beat) * pxPerBeat - scroll.x }
    func beat(_ x: CGFloat) -> Double { Double((x - keysW + scroll.x) / pxPerBeat) }
    func y(_ row: Int) -> CGFloat { rulerH + CGFloat(row) * rowH - scroll.y }
    func row(_ y: CGFloat) -> Int { Int(floor((y - rulerH + scroll.y) / rowH)) }

    func clampScroll() {
        guard let m = model else { return }
        let maxX = max(0, CGFloat(m.beatsTotal) * pxPerBeat - gridRect.width + 40)
        let maxY = max(0, CGFloat(m.rows.count) * rowH - gridRect.height)
        scroll.x = min(max(0, scroll.x), maxX)
        scroll.y = min(max(0, scroll.y), maxY)
    }

    /// Start with the notes in view (or the middle of the keyboard).
    func centerOnNotes() {
        guard let m = model else { return }
        let rowsUsed = m.notes.map { $0.row }
        let mid = rowsUsed.isEmpty ? m.rows.count / 2 : (rowsUsed.min()! + rowsUsed.max()!) / 2
        scroll.y = CGFloat(mid) * rowH - gridRect.height / 2
        let fit = (bounds.width - keysW - 20) / CGFloat(max(1, m.beatsTotal))
        pxPerBeat = max(12, min(96, fit))
        clampScroll()
        needsDisplay = true
    }

    // MARK: drawing

    override func draw(_ dirty: NSRect) {
        guard let m = model else { return }
        NSColor(calibratedRed: 0.145, green: 0.15, blue: 0.165, alpha: 1).setFill()
        bounds.fill()
        let g = gridRect
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: g).addClip()
        // rows
        for r in 0..<m.rows.count {
            let ry = y(r)
            if ry > g.maxY || ry + rowH < g.minY { continue }
            let black = m.kind == "notes" && [1, 3, 6, 8, 10].contains(((m.rowPitch[r] % 12) + 12) % 12)
            (black ? NSColor(white: 0.13, alpha: 1) : NSColor(white: 0.175, alpha: 1)).setFill()
            NSRect(x: g.minX, y: ry, width: g.width, height: rowH).fill()
            if m.kind == "notes" && m.rowPitch[r] % 12 == 0 {
                NSColor(white: 1, alpha: 0.12).setFill()
                NSRect(x: g.minX, y: ry + rowH - 1, width: g.width, height: 1).fill()
            }
        }
        // beat grid
        let first = max(0, Int(floor(beat(g.minX) / m.grid)))
        let last = Int(ceil(min(m.beatsTotal, beat(g.maxX)) / m.grid))
        if last >= first {
            for i in first...last {
                let b = Double(i) * m.grid
                let px = x(b)
                let isBar = abs(b.truncatingRemainder(dividingBy: 4)) < 1e-9
                let isBeat = abs(b - b.rounded()) < 1e-9
                if !isBar && !isBeat && pxPerBeat * CGFloat(m.grid) < 6 { continue }
                NSColor(white: 1, alpha: isBar ? 0.22 : (isBeat ? 0.09 : 0.04)).setFill()
                NSRect(x: px, y: g.minY, width: 1, height: g.height).fill()
            }
        }
        // past the end of the section
        let endX = x(m.beatsTotal)
        if endX < g.maxX {
            NSColor(white: 0, alpha: 0.35).setFill()
            NSRect(x: endX, y: g.minY, width: g.maxX - endX, height: g.height).fill()
        }
        // notes
        for n in m.notes {
            let r = noteRect(n)
            if !r.intersects(g) { continue }
            let sel = m.selection.contains(n.id)
            let base = sel ? NSColor.white : color
            base.withAlphaComponent(0.45 + 0.55 * n.vel).setFill()
            let p = NSBezierPath(roundedRect: r.insetBy(dx: 0.5, dy: 1), xRadius: 2.5, yRadius: 2.5)
            p.fill()
            (sel ? color : NSColor(white: 0, alpha: 0.5)).setStroke()
            p.lineWidth = sel ? 1.5 : 1
            p.stroke()
            if r.width > 30 && rowH >= 12 {
                let label = m.kind == "notes" ? m.name(m.rowPitch[n.row]) : m.rows[n.row]
                (label as NSString).draw(at: NSPoint(x: r.minX + 3, y: r.minY + (rowH - 13) / 2),
                                         withAttributes: [.font: NSFont.monospacedSystemFont(ofSize: 9.5, weight: .medium),
                                                          .foregroundColor: NSColor(white: 0, alpha: 0.75)])
            }
        }
        if case let .marquee(a, b) = drag {
            let r = NSRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(a.x - b.x), height: abs(a.y - b.y))
            NSColor(white: 1, alpha: 0.08).setFill(); r.fill()
            NSColor(white: 1, alpha: 0.5).setStroke(); NSBezierPath(rect: r).stroke()
        }
        NSGraphicsContext.restoreGraphicsState()
        drawKeys(m)
        drawRuler(m)
        drawVelocity(m)
    }

    func noteRect(_ n: EditNote) -> NSRect {
        NSRect(x: x(n.beat), y: y(n.row), width: max(3, CGFloat(n.len) * pxPerBeat), height: rowH)
    }

    private func drawKeys(_ m: NoteEditorModel) {
        let area = NSRect(x: 0, y: rulerH, width: keysW, height: bounds.height - rulerH - velH)
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: area).addClip()
        NSColor(calibratedRed: 0.12, green: 0.125, blue: 0.14, alpha: 1).setFill()
        area.fill()
        for r in 0..<m.rows.count {
            let ry = y(r)
            if ry > area.maxY || ry + rowH < area.minY { continue }
            let rect = NSRect(x: 0, y: ry, width: keysW - 1, height: rowH)
            if m.kind == "notes" {
                let pc = ((m.rowPitch[r] % 12) + 12) % 12
                let black = [1, 3, 6, 8, 10].contains(pc)
                (black ? NSColor(white: 0.1, alpha: 1) : NSColor(white: 0.82, alpha: 1)).setFill()
                rect.insetBy(dx: 0, dy: 0.5).fill()
                if pc == 0 || rowH >= 13 {
                    (m.name(m.rowPitch[r]) as NSString).draw(
                        at: NSPoint(x: 6, y: ry + (rowH - 12) / 2),
                        withAttributes: [.font: NSFont.monospacedSystemFont(ofSize: 9, weight: pc == 0 ? .bold : .regular),
                                         .foregroundColor: black ? NSColor(white: 0.7, alpha: 1) : NSColor(white: 0.15, alpha: 1)])
                }
            } else {
                let label = drumNames[m.rows[r]] ?? m.rows[r]
                (label as NSString).draw(at: NSPoint(x: 6, y: ry + (rowH - 12) / 2),
                                         withAttributes: [.font: NSFont.systemFont(ofSize: 10),
                                                          .foregroundColor: NSColor(white: 0.8, alpha: 1)])
                NSColor(white: 1, alpha: 0.06).setFill()
                NSRect(x: 0, y: ry + rowH - 1, width: keysW, height: 1).fill()
            }
        }
        NSGraphicsContext.restoreGraphicsState()
    }

    private func drawRuler(_ m: NoteEditorModel) {
        let r = NSRect(x: 0, y: 0, width: bounds.width, height: rulerH)
        NSColor(calibratedRed: 0.11, green: 0.115, blue: 0.13, alpha: 1).setFill()
        r.fill()
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: NSRect(x: keysW, y: 0, width: bounds.width - keysW, height: rulerH)).addClip()
        let bars = Int(ceil(m.bars))
        for b in 0...bars {
            let px = x(Double(b) * 4)
            NSColor(white: 1, alpha: 0.3).setFill()
            NSRect(x: px, y: rulerH - 8, width: 1, height: 8).fill()
            if b < bars {
                ("\(Int(m.startBar) + b + 1)" as NSString).draw(
                    at: NSPoint(x: px + 4, y: 4),
                    withAttributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .medium),
                                     .foregroundColor: NSColor(white: 0.75, alpha: 1)])
            }
            if pxPerBeat > 14 && b < bars {
                for q in 1..<4 {
                    NSColor(white: 1, alpha: 0.15).setFill()
                    NSRect(x: x(Double(b) * 4 + Double(q)), y: rulerH - 4, width: 1, height: 4).fill()
                }
            }
        }
        NSGraphicsContext.restoreGraphicsState()
        NSColor(white: 0, alpha: 0.6).setFill()
        NSRect(x: 0, y: rulerH - 1, width: bounds.width, height: 1).fill()
    }

    private func drawVelocity(_ m: NoteEditorModel) {
        let v = velRect
        NSColor(calibratedRed: 0.12, green: 0.125, blue: 0.14, alpha: 1).setFill()
        NSRect(x: 0, y: v.minY, width: bounds.width, height: velH).fill()
        NSColor(white: 0, alpha: 0.6).setFill()
        NSRect(x: 0, y: v.minY, width: bounds.width, height: 1).fill()
        ("velocity" as NSString).draw(at: NSPoint(x: 6, y: v.minY + 6),
                                      withAttributes: [.font: NSFont.systemFont(ofSize: 10), .foregroundColor: NSColor(white: 0.6, alpha: 1)])
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: v).addClip()
        for n in m.notes {
            let px = x(n.beat)
            if px < v.minX - 4 || px > v.maxX { continue }
            let h = CGFloat(n.vel) * (velH - 10)
            let sel = m.selection.contains(n.id)
            (sel ? NSColor.white : color).withAlphaComponent(sel ? 1 : 0.8).setFill()
            NSRect(x: px, y: v.maxY - 3 - h, width: 3, height: h).fill()
            NSBezierPath(ovalIn: NSRect(x: px - 1.5, y: v.maxY - 3 - h - 3, width: 6, height: 6)).fill()
        }
        NSGraphicsContext.restoreGraphicsState()
    }

    // MARK: mouse

    private func hit(_ p: NSPoint) -> EditNote? {
        model?.notes.last { noteRect($0).contains(p) }
    }

    override func mouseDown(with e: NSEvent) {
        window?.makeFirstResponder(self)
        guard let m = model else { return }
        let p = convert(e.locationInWindow, from: nil)
        if m.readOnlyReason != nil { NSSound.beep(); return }
        if velRect.contains(p) {
            m.record()
            drag = .velocity
            velocityDrag(p)
            return
        }
        guard gridRect.contains(p) else { return }
        if let n = hit(p) {
            if e.clickCount == 2 {  // double-click deletes
                m.record()
                m.notes.removeAll { $0.id == n.id }
                m.selection.remove(n.id)
                needsDisplay = true
                return
            }
            if e.modifierFlags.contains(.shift) {
                if m.selection.contains(n.id) { m.selection.remove(n.id) } else { m.selection.insert(n.id) }
            } else if !m.selection.contains(n.id) {
                m.selection = [n.id]
            }
            m.record()
            if e.modifierFlags.contains(.option) {  // option-drag copies
                let copies = m.notes.filter { m.selection.contains($0.id) }.map { EditNote(beat: $0.beat, len: $0.len, row: $0.row, vel: $0.vel, flags: $0.flags) }
                m.notes.append(contentsOf: copies)
                m.selection = Set(copies.map { $0.id })
            }
            let r = noteRect(n)
            if p.x > r.maxX - min(7, r.width / 3) {
                drag = .resize(start: p, orig: Dictionary(uniqueKeysWithValues: m.notes.filter { m.selection.contains($0.id) }.map { ($0.id, $0.len) }))
            } else {
                drag = .move(start: p, orig: Dictionary(uniqueKeysWithValues: m.notes.filter { m.selection.contains($0.id) }.map { ($0.id, ($0.beat, $0.row)) }))
            }
        } else if e.modifierFlags.contains(.command) || e.modifierFlags.contains(.shift) {
            drag = .marquee(start: p, now: p)
        } else {
            m.selection = []
            let b = m.snap(beat(p.x)), r = row(p.y)
            guard b >= 0, b < m.beatsTotal, r >= 0, r < m.rows.count else { return }
            m.record()
            let len = min(m.lastLength, m.beatsTotal - b)
            let n = EditNote(beat: b, len: max(m.grid, len), row: r, vel: 0.75, flags: [])
            m.notes.append(n)
            m.selection = [n.id]
            drag = .create(id: n.id, beat: b)
        }
        needsDisplay = true
    }

    override func mouseDragged(with e: NSEvent) {
        guard let m = model else { return }
        let p = convert(e.locationInWindow, from: nil)
        switch drag {
        case let .move(start, orig):
            let db = (Double(p.x - start.x) / Double(pxPerBeat) / m.grid).rounded() * m.grid
            let dr = Int(((p.y - start.y) / rowH).rounded())
            for i in m.notes.indices {
                guard let (b0, r0) = orig[m.notes[i].id] else { continue }
                let nb = max(0, min(m.beatsTotal - m.grid, b0 + db))
                if nb != m.notes[i].beat { m.notes[i].beat = nb; m.notes[i].beatText = nil }
                m.notes[i].row = max(0, min(m.rows.count - 1, r0 + dr))
            }
        case let .resize(start, orig):
            let dl = (Double(p.x - start.x) / Double(pxPerBeat) / m.grid).rounded() * m.grid
            for i in m.notes.indices {
                guard let l0 = orig[m.notes[i].id] else { continue }
                let nl = max(m.grid, l0 + dl)
                if nl != m.notes[i].len { m.notes[i].len = nl; m.notes[i].lenText = nil; m.lastLength = nl }
            }
        case let .create(id, b0):
            if let i = m.notes.firstIndex(where: { $0.id == id }) {
                let end = max(b0 + m.grid, (beat(p.x) / m.grid).rounded(.up) * m.grid)
                m.notes[i].len = min(end, m.beatsTotal) - b0
                m.lastLength = m.notes[i].len
            }
        case let .marquee(a, _):
            drag = .marquee(start: a, now: p)
            let r = NSRect(x: min(a.x, p.x), y: min(a.y, p.y), width: abs(a.x - p.x), height: abs(a.y - p.y))
            m.selection = Set(m.notes.filter { noteRect($0).intersects(r) }.map { $0.id })
        case .velocity:
            velocityDrag(p)
        case .none:
            break
        }
        autoscroll(with: e)
        needsDisplay = true
    }

    override func mouseUp(with e: NSEvent) {
        drag = .none
        needsDisplay = true
    }

    /// In the velocity lane: set the selected notes (or the ones under the pointer) to the pointer's height.
    private func velocityDrag(_ p: NSPoint) {
        guard let m = model else { return }
        let v = Double((velRect.maxY - 3 - p.y) / (velH - 10))
        let near = Set(m.notes.filter { abs(x($0.beat) - p.x) < 5 }.map { $0.id })
        let ids = near.isEmpty ? [] : (near.isDisjoint(with: m.selection) ? near : m.selection)
        m.setVelocity(v, for: ids)
        needsDisplay = true
    }

    override func scrollWheel(with e: NSEvent) {
        if e.modifierFlags.contains(.command) || e.modifierFlags.contains(.option) {
            let p = convert(e.locationInWindow, from: nil)
            if e.modifierFlags.contains(.option) {
                let anchor = (p.y - rulerH + scroll.y) / rowH
                rowH = min(32, max(8, rowH * (1 + e.scrollingDeltaY * 0.01)))
                scroll.y = anchor * rowH - (p.y - rulerH)
            } else {
                let anchor = beat(p.x)
                pxPerBeat = min(400, max(6, pxPerBeat * (1 + e.scrollingDeltaY * 0.01)))
                scroll.x = CGFloat(anchor) * pxPerBeat - (p.x - keysW)
            }
        } else {
            scroll.x -= e.scrollingDeltaX
            scroll.y -= e.scrollingDeltaY
        }
        clampScroll()
        needsDisplay = true
    }

    override func magnify(with e: NSEvent) {
        let p = convert(e.locationInWindow, from: nil)
        let anchor = beat(p.x)
        pxPerBeat = min(400, max(6, pxPerBeat * (1 + e.magnification)))
        scroll.x = CGFloat(anchor) * pxPerBeat - (p.x - keysW)
        clampScroll()
        needsDisplay = true
    }

    // MARK: keys

    override func keyDown(with e: NSEvent) {
        guard let m = model else { return }
        let cmd = e.modifierFlags.contains(.command), shift = e.modifierFlags.contains(.shift)
        switch (e.keyCode, e.charactersIgnoringModifiers ?? "") {
        case (51, _), (117, _): m.deleteSelected()
        case (123, _): m.nudge(beats: -(shift ? 4 : m.grid), rows: 0)
        case (124, _): m.nudge(beats: shift ? 4 : m.grid, rows: 0)
        case (126, _): m.nudge(beats: 0, rows: shift ? -12 : -1)
        case (125, _): m.nudge(beats: 0, rows: shift ? 12 : 1)
        case (_, "a") where cmd: m.selectAll()
        case (_, "z") where cmd && shift: m.redo()
        case (_, "z") where cmd: m.undo()
        case (53, _): m.selection = []
        default: super.keyDown(with: e)
        }
        needsDisplay = true
    }

    override func layout() {
        super.layout()
        clampScroll()
    }
}

struct PianoRoll: NSViewRepresentable {
    @ObservedObject var model: NoteEditorModel
    let color: NSColor

    func makeNSView(context: Context) -> PianoRollView {
        let v = PianoRollView()
        v.model = model
        v.color = color
        return v
    }

    func updateNSView(_ v: PianoRollView, context: Context) {
        v.model = model
        v.needsDisplay = true
    }
}

// MARK: window

struct NoteEditorView: View {
    @ObservedObject var model: NoteEditorModel
    let color: NSColor
    let roll: PianoRollHolder

    private let grids: [(String, Double)] = [("1/4", 1), ("1/8", 0.5), ("1/8T", 1.0 / 3), ("1/16", 0.25),
                                             ("1/16T", 1.0 / 6), ("1/32", 0.125)]

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Circle().fill(Color(nsColor: color)).frame(width: 10, height: 10)
                Text("\(model.track)").font(.system(size: 13, weight: .semibold))
                Text("in \(model.section)").foregroundColor(.secondary)
                Divider().frame(height: 16)
                Picker("Grid", selection: $model.grid) {
                    ForEach(grids, id: \.1) { Text($0.0).tag($0.1) }
                }
                .frame(width: 130)
                if !model.selection.isEmpty {
                    Text("velocity").foregroundColor(.secondary)
                    Slider(value: Binding(
                        get: {
                            let vs = model.notes.filter { model.selection.contains($0.id) }.map { $0.vel }
                            return vs.isEmpty ? 0.75 : vs.reduce(0, +) / Double(vs.count)
                        },
                        set: { model.setVelocity($0, for: model.selection) }), in: 0.01...1,
                        onEditingChanged: { if $0 { model.record() } })
                    .frame(width: 120)
                }
                Spacer()
                Button("Revert") { model.revert() }.disabled(!model.changed)
                Button("Apply & Play") { model.apply(thenPlay: true) }
                    .disabled(model.readOnlyReason != nil)
                    .keyboardShortcut(.return, modifiers: [.command, .shift])
                Button("Apply") { model.apply() }
                    .disabled(!model.changed || model.readOnlyReason != nil)
                    .keyboardShortcut(.return, modifiers: .command)
                    .buttonStyle(.borderedProminent)
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(Color(red: 0.11, green: 0.115, blue: 0.13))
            roll.view
            HStack {
                Text(model.status).lineLimit(1)
                Spacer()
                Text(model.readOnlyReason == nil
                     ? "click: add · drag: move · edge: resize · ⌥drag: copy · ⇧/⌘drag: select · double-click / ⌫: delete · arrows: nudge · ⌘scroll: zoom"
                     : "read only")
                    .lineLimit(1)
            }
            .font(.system(size: 11))
            .foregroundColor(.secondary)
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(Color(red: 0.11, green: 0.115, blue: 0.13))
        }
        .onChange(of: model.notes) { _ in roll.redraw() }
        .onChange(of: model.selection) { _ in roll.redraw() }
        .onChange(of: model.grid) { _ in roll.redraw() }
    }
}

/// Keeps the roll's NSView so the toolbar can ask it to redraw or re-center.
@MainActor
final class PianoRollHolder {
    let model: NoteEditorModel
    let color: NSColor
    private(set) lazy var view = PianoRoll(model: model, color: color)
    weak var nsView: PianoRollView?
    init(model: NoteEditorModel, color: NSColor) { self.model = model; self.color = color }
    func redraw() {
        nsView?.needsDisplay = true
    }
}

/// Opens (or brings forward) one editor window per band.
@MainActor
final class NoteEditorWindows: NSObject, NSWindowDelegate {
    static let shared = NoteEditorWindows()
    private var windows: [String: (NSWindow, NoteEditorModel)] = [:]

    func open(studio: StudioModel, track: String, section: String, startBar: Double, colorIndex: Int) {
        let key = "\(track)@\(section)@\(startBar)"
        if let (w, _) = windows[key] {
            w.makeKeyAndOrderFront(nil)
            return
        }
        let model = NoteEditorModel(studio: studio, section: section, track: track, startBar: startBar)
        let color = NSColor(Palette.color(colorIndex))
        let holder = PianoRollHolder(model: model, color: color)
        let host = NSHostingView(rootView: NoteEditorView(model: model, color: color, roll: holder))
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 560),
                         styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "\(track) — \(section)"
        w.contentView = host
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        w.setFrameAutosaveName("NoteEditor")
        windows[key] = (w, model)
        w.makeKeyAndOrderFront(nil)
        model.load()
        // Find the roll view once it's in the window, then fit it to the notes when they arrive.
        DispatchQueue.main.async {
            holder.nsView = Self.findRoll(in: host)
            holder.nsView.map { w.makeFirstResponder($0) }
        }
        var centered = false
        cancellables[key] = model.$notes.dropFirst().sink { [weak holder] _ in
            guard !centered else { return }
            centered = true
            DispatchQueue.main.async { holder?.nsView?.centerOnNotes() }
        }
    }

    private var cancellables: [String: AnyCancellable] = [:]

    func entry(track: String, section: String, startBar: Double) -> (NSWindow, NoteEditorModel)? {
        windows["\(track)@\(section)@\(startBar)"]
    }

    static func findRoll(in v: NSView) -> PianoRollView? {
        if let r = v as? PianoRollView { return r }
        for s in v.subviews { if let r = findRoll(in: s) { return r } }
        return nil
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard let (key, entry) = windows.first(where: { $0.value.0 === sender }) else { return true }
        let model = entry.1
        if model.changed && model.readOnlyReason == nil {
            let a = NSAlert()
            a.messageText = "Apply your note changes to \(model.track) in \(model.section)?"
            a.informativeText = "If you don't apply them, they're lost."
            a.addButton(withTitle: "Apply")
            a.addButton(withTitle: "Discard")
            a.addButton(withTitle: "Cancel")
            switch a.runModal() {
            case .alertFirstButtonReturn: model.apply()
            case .alertSecondButtonReturn: break
            default: return false
            }
        }
        windows[key] = nil
        cancellables[key] = nil
        return true
    }
}

// MARK: self-test

/// PULSAR_EDITOR_SELFTEST="track,section" with PULSAR_EDITOR_SELFTEST_OUT=dir: opens that band's editor, takes a
/// snapshot, adds a note and drags the first one with real mouse events, applies, then writes snapshots and the
/// resulting source into dir. Used to check the editor without a person at the screen.
@MainActor
enum NoteEditorSelfTest {
    private static var started = false

    static func runIfRequested(studio: StudioModel) {
        let env = ProcessInfo.processInfo.environment
        guard !started, let spec = env["PULSAR_EDITOR_SELFTEST"], let out = env["PULSAR_EDITOR_SELFTEST_OUT"] else { return }
        started = true
        let parts = spec.split(separator: ",").map(String.init)
        guard parts.count == 2 else { return }
        let dir = URL(fileURLWithPath: out)
        func log(_ s: String) {
            let f = dir.appendingPathComponent("log.txt")
            let line = s + "\n"
            if let h = try? FileHandle(forWritingTo: f) { h.seekToEndOfFile(); h.write(Data(line.utf8)); try? h.close() }
            else { try? line.write(to: f, atomically: true, encoding: .utf8) }
        }
        func waitFor(_ cond: @escaping () -> Bool, _ then: @escaping () -> Void, tries: Int = 100) {
            if cond() || tries == 0 { then(); return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { waitFor(cond, then, tries: tries - 1) }
        }
        waitFor({ studio.score?.sections != nil }) {
            guard let sec = studio.score?.sections?.first(where: { $0.name == parts[1] }) else { log("no section"); return }
            NoteEditorWindows.shared.open(studio: studio, track: parts[0], section: parts[1], startBar: sec.bar, colorIndex: 2)
            guard let (w, m) = NoteEditorWindows.shared.entry(track: parts[0], section: parts[1], startBar: sec.bar) else { return }
            waitFor({ !m.notes.isEmpty }) {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    snapshot(w, dir.appendingPathComponent("1-opened.png"))
                    log("loaded \(m.notes.count) notes, rows \(m.rows.count), kind \(m.kind), readOnly \(m.readOnlyReason ?? "no")")
                    guard let roll = NoteEditorWindows.findRoll(in: w.contentView!) else { log("no roll"); return }
                    let before = m.notes.count
                    let first = m.notes.min { $0.beat < $1.beat }!
                    // 1. click an empty spot one row above the first note, two beats later: adds a note
                    let emptyPt = NSPoint(x: roll.x(first.beat + 2) + 2, y: roll.y(max(0, first.row - 3)) + roll.rowH / 2)
                    send(roll, w, .leftMouseDown, emptyPt); send(roll, w, .leftMouseUp, emptyPt)
                    log("after click: \(m.notes.count) notes (was \(before))")
                    // 2. drag the first note up two rows and right one beat
                    let r = roll.noteRect(first)
                    let a = NSPoint(x: r.minX + 4, y: r.midY)
                    let b = NSPoint(x: a.x + roll.pxPerBeat, y: a.y - 2 * roll.rowH)
                    send(roll, w, .leftMouseDown, a); send(roll, w, .leftMouseDragged, b); send(roll, w, .leftMouseUp, b)
                    if let moved = m.notes.first(where: { $0.id == first.id }) {
                        let label = { (r: Int) in m.kind == "notes" ? m.name(m.rowPitch[r]) : m.rows[r] }
                        log("dragged: beat \(first.beat) -> \(moved.beat), row \(first.row) -> \(moved.row) (\(label(first.row)) -> \(label(moved.row)))")
                    }
                    log("changed \(m.changed)")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                        snapshot(w, dir.appendingPathComponent("2-edited.png"))
                        let src0 = studio.source
                        m.apply()
                        waitFor({ studio.source != src0 }) {
                            try? studio.source.write(to: dir.appendingPathComponent("applied.pulse"), atomically: true, encoding: .utf8)
                            log("applied: \(m.status)")
                            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                                if let main = NSApp.windows.first(where: { !($0.contentView is NSHostingView<NoteEditorView>) && $0.isVisible && $0 !== w }) {
                                    snapshot(main, dir.appendingPathComponent("3-main.png"))
                                }
                                log("done")
                            }
                        }
                    }
                }
            }
        }
    }

    private static func send(_ v: NSView, _ w: NSWindow, _ type: NSEvent.EventType, _ p: NSPoint) {
        let inWindow = v.convert(p, to: nil)
        guard let e = NSEvent.mouseEvent(with: type, location: inWindow, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                         windowNumber: w.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) else { return }
        switch type {
        case .leftMouseDown: v.mouseDown(with: e)
        case .leftMouseDragged: v.mouseDragged(with: e)
        default: v.mouseUp(with: e)
        }
    }

    static func snapshot(_ w: NSWindow, _ url: URL) {
        guard let v = w.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) else { return }
        v.cacheDisplay(in: v.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }
}
