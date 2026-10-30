import AppKit
import SwiftUI

/// One horizontal strip in the arrangement: a track (notes / drum rows / hits) or an automation curve.
struct Lane: Identifiable {
    enum Kind { case notes, drums, hits, automation }
    let id: String
    let kind: Kind
    let track: TrackData
    let colorIndex: Int
    var y: CGFloat = 0
    var height: CGFloat
    var rows: [String] = []  // drum voices or slice labels, top to bottom
    var pitchLo: Double = 48
    var pitchHi: Double = 72
    var auto: AutoData?
}

enum Palette {
    static let colors: [Color] = [
        Color(red: 1.00, green: 0.55, blue: 0.26), Color(red: 0.98, green: 0.82, blue: 0.30),
        Color(red: 0.56, green: 0.86, blue: 0.40), Color(red: 0.33, green: 0.80, blue: 0.84),
        Color(red: 0.40, green: 0.62, blue: 1.00), Color(red: 0.70, green: 0.52, blue: 1.00),
        Color(red: 1.00, green: 0.45, blue: 0.70), Color(red: 0.95, green: 0.38, blue: 0.36),
    ]
    static func color(_ i: Int) -> Color { colors[i % colors.count] }
    static let bg = Color(red: 0.145, green: 0.15, blue: 0.165)
    static let laneA = Color(red: 0.165, green: 0.17, blue: 0.19)
    static let laneB = Color(red: 0.185, green: 0.19, blue: 0.21)
    static let header = Color(red: 0.12, green: 0.125, blue: 0.14)
    static let grid = Color.white.opacity(0.06)
    static let gridStrong = Color.white.opacity(0.13)
}

let drumOrder = ["bd", "sd", "cp", "rim", "lt", "mt", "ht", "hh", "oh", "sh", "cb", "pc", "rd", "cr"]
let drumNames = ["bd": "kick", "sd": "snare", "cp": "clap", "rim": "rim", "lt": "low tom", "mt": "mid tom",
                 "ht": "hi tom", "hh": "closed hat", "oh": "open hat", "sh": "shaker", "cb": "cowbell",
                 "pc": "perc", "rd": "ride", "cr": "crash"]

let trackGap: CGFloat = 8  // space between channels
let rowH: CGFloat = 14  // one drum voice / slice row at 1x vertical zoom

func buildLanes(_ score: ScoreData, scale: CGFloat = 1) -> [Lane] {
    var lanes: [Lane] = []
    var y: CGFloat = 0
    let autos = score.automation ?? []
    for (i, t) in (score.tracks ?? []).enumerated() {
        if i > 0 { y += trackGap }
        var lane: Lane
        let pitches = t.notes.compactMap { $0.pitch }
        if t.lane == "drums" {
            let voices = Set(t.notes.compactMap { $0.drum })
            let rows = drumOrder.filter { voices.contains($0) } + voices.subtracting(drumOrder).sorted()
            lane = Lane(id: t.name, kind: .drums, track: t, colorIndex: i,
                        height: max(30, CGFloat(rows.count) * rowH * scale + 8), rows: rows)
        } else if t.lane == "sampler" && pitches.isEmpty {
            let slices = Set(t.notes.compactMap { $0.slice })
            let rows = slices.isEmpty ? ["hit"] : slices.sorted().map { "slice \($0)" }
            lane = Lane(id: t.name, kind: .hits, track: t, colorIndex: i,
                        height: max(30, CGFloat(rows.count) * rowH * scale + 8), rows: rows)
        } else {
            let lo = (pitches.min() ?? 60) - 1, hi = (pitches.max() ?? 60) + 1
            let span = hi - lo
            lane = Lane(id: t.name, kind: .notes, track: t, colorIndex: i,
                        height: min(150 * scale, max(46, CGFloat(span) * 4 * scale + 10)), pitchLo: lo, pitchHi: hi)
        }
        lane.y = y
        y += lane.height
        lanes.append(lane)
        for a in autos where a.track == t.name {
            y += 1
            var al = Lane(id: t.name + "/" + a.param, kind: .automation, track: t, colorIndex: i, height: 30 * max(1, scale * 0.8))
            al.auto = a
            al.y = y
            y += al.height
            lanes.append(al)
        }
    }
    return lanes
}

/// Scroll and hover state for the arrangement (an object rather than @State, which
/// needs the SwiftUI macro plugin that only ships with full Xcode).
final class ArrangementState: ObservableObject {
    @Published var offset = CGPoint.zero  // x in points of timeline, y in points of lanes
    @Published var hoverInfo = ""
    /// When the user last scrolled or zoomed by hand; auto-follow waits 10 s after that.
    var lastManualScroll = Date.distantPast
}

struct ArrangementView: View {
    @ObservedObject var model: StudioModel
    @StateObject private var state = ArrangementState()
    private var offset: CGPoint {
        get { state.offset }
        nonmutating set { state.offset = newValue }
    }
    private var hoverInfo: String {
        get { state.hoverInfo }
        nonmutating set { state.hoverInfo = newValue }
    }
    static let headerW: CGFloat = 200
    static let rulerH: CGFloat = 40

    var body: some View {
        GeometryReader { geo in
            let lanes = model.score.map { buildLanes($0, scale: model.laneScale) } ?? []
            let contentH = (lanes.last.map { $0.y + $0.height } ?? 0)
            ZStack(alignment: .topLeading) {
                TimelineView(.animation(minimumInterval: 1.0 / 60, paused: !model.isPlaying)) { timeline in
                    // The canvas must depend on the timeline's date: otherwise SwiftUI sees
                    // identical inputs on each tick and only repaints when something else
                    // (like a mouse-move hover update) invalidates the view.
                    let frame = timeline.date.timeIntervalSinceReferenceDate
                    Canvas { ctx, size in
                        draw(ctx: ctx, size: size, lanes: lanes, frame: frame)
                    }
                }
                InputCatcher(
                    onScroll: { dx, dy, zoom, vzoom, loc in
                        if dx != 0 || zoom { state.lastManualScroll = Date() }
                        if vzoom {
                            stretch(by: 1 + dy * 0.01, at: loc.y)
                        } else if zoom {
                            zoomAround(x: loc.x, factor: 1 + dy * 0.01, width: geo.size.width)
                        } else {
                            offset.x = max(0, offset.x - dx)
                            offset.y = min(max(0, offset.y - dy), max(0, contentH - geo.size.height + Self.rulerH + 40))
                        }
                    },
                    onMagnify: { f, vertical, loc in
                        if !vertical { state.lastManualScroll = Date() }
                        if vertical { stretch(by: 1 + f, at: loc.y) } else { zoomAround(x: loc.x, factor: 1 + f, width: geo.size.width) }
                    },
                    onClick: { loc in click(at: loc, lanes: lanes) },
                    onDoubleClick: { loc in openNoteEditor(at: loc, lanes: lanes) },
                    onHover: { loc in hoverInfo = hover(at: loc, lanes: lanes) })
                if !hoverInfo.isEmpty {
                    Text(hoverInfo)
                        .font(.system(size: 11, design: .monospaced))
                        .padding(.horizontal, 6).padding(.vertical, 3)
                        .background(.black.opacity(0.75)).cornerRadius(4)
                        .foregroundColor(.white)
                        .padding(8)
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomTrailing)
                        .allowsHitTesting(false)
                }
            }
            .background(Palette.bg)
            .onReceive(Timer.publish(every: 0.1, on: .main, in: .common).autoconnect()) { _ in
                guard model.isPlaying, model.followPlayhead else { return }
                // Scrolling by hand takes over; following resumes after 10 s without manual scrolling.
                guard Date().timeIntervalSince(state.lastManualScroll) > 10 else { return }
                let x = CGFloat(model.playheadBar()) * model.pixelsPerBar
                let visible = geo.size.width - Self.headerW
                if x < offset.x || x > offset.x + visible * 0.85 {
                    offset.x = max(0, x - visible * 0.15)
                }
            }
        }
    }

    // MARK: interaction

    private func zoomAround(x: CGFloat, factor: CGFloat, width: CGFloat) {
        let tx = x - Self.headerW
        let barAt = (offset.x + tx) / model.pixelsPerBar
        model.pixelsPerBar = min(400, max(4, model.pixelsPerBar * factor))
        offset.x = max(0, barAt * model.pixelsPerBar - tx)
    }

    /// Vertical zoom around a point, keeping the lane under the pointer in place.
    private func stretch(by factor: CGFloat, at y: CGFloat) {
        let old = model.laneScale
        let new = min(4, max(0.6, old * factor))
        guard new != old else { return }
        let anchor = y - Self.rulerH + offset.y
        model.laneScale = new
        offset.y = max(0, anchor * new / old - (y - Self.rulerH))
    }

    private func barAt(_ x: CGFloat) -> Double { Double((x - Self.headerW + offset.x) / model.pixelsPerBar) }

    private func laneAt(_ y: CGFloat, _ lanes: [Lane]) -> Lane? {
        let ly = y - Self.rulerH + offset.y
        return lanes.first { ly >= $0.y && ly < $0.y + $0.height }
    }

    private func click(at loc: CGPoint, lanes: [Lane]) {
        if loc.y < Self.rulerH, loc.x > Self.headerW {
            model.seek(toBar: max(0, (barAt(loc.x) * 4).rounded(.down) / 4))
            return
        }
        if let lane = laneAt(loc.y, lanes) {
            model.selectedTrack = lane.track.name
            if loc.x < Self.headerW, let line = lane.track.line { model.jumpToLine = line }
        }
    }

    /// Double-clicking a band (a track inside one section) opens its notes in a note editor window.
    private func openNoteEditor(at loc: CGPoint, lanes: [Lane]) {
        guard loc.x > Self.headerW, loc.y > Self.rulerH, let lane = laneAt(loc.y, lanes), lane.kind != .automation,
              let sections = model.score?.sections else { return }
        let bar = barAt(loc.x)
        guard let sec = sections.last(where: { $0.bar <= bar + 1e-9 }), bar < sec.bar + sec.bars else { return }
        NoteEditorWindows.shared.open(studio: model, track: lane.track.name, section: sec.name, startBar: sec.bar,
                                      colorIndex: lane.colorIndex)
    }

    private func hover(at loc: CGPoint, lanes: [Lane]) -> String {
        guard loc.x > Self.headerW, loc.y > Self.rulerH, let lane = laneAt(loc.y, lanes) else { return "" }
        let bar = barAt(loc.x)
        let beat = Int((bar - floor(bar)) * 4) + 1
        var s = "\(lane.track.name)  bar \(Int(bar) + 1).\(beat)"
        if lane.kind == .notes {
            let ly = loc.y - Self.rulerH + offset.y - lane.y
            let p = lane.pitchHi - Double(ly - 5) / Double(lane.height - 10) * (lane.pitchHi - lane.pitchLo)
            if let n = lane.track.notes.first(where: { abs(($0.pitch ?? -99) - p) < 1.0 && $0.bar <= bar && bar < $0.bar + $0.dur_bars }) {
                s += "  \(noteName(n.pitch!))  vel \(String(format: "%.2f", n.vel))"
            }
        } else if lane.kind == .drums {
            let ly = loc.y - Self.rulerH + offset.y - lane.y - 4
            let row = Int(ly / (rowH * model.laneScale))
            if row >= 0 && row < lane.rows.count { s += "  \(drumNames[lane.rows[row]] ?? lane.rows[row])" }
        } else if lane.kind == .automation, let a = lane.auto {
            if let p = a.points.last(where: { $0[0] <= bar }) { s += "  \(a.param) = \(String(format: "%.3g", p[1]))" }
        }
        return s
    }

    // MARK: drawing

    private func draw(ctx: GraphicsContext, size: CGSize, lanes: [Lane], frame: TimeInterval) {
        _ = frame  // drives redraws while playing; the playhead itself is read from the player
        let ppb = model.pixelsPerBar
        let hw = Self.headerW, rh = Self.rulerH
        let score = model.score
        let totalBars = score?.total_bars ?? 16
        let x0 = hw - offset.x
        let visibleBars = (offset.x / ppb, (offset.x + size.width - hw) / ppb)
        let playhead = model.playheadBar()

        // lanes
        var c = ctx
        c.clip(to: Path(CGRect(x: 0, y: rh, width: size.width, height: size.height - rh)))
        for (i, lane) in lanes.enumerated() {
            let y = rh + lane.y - offset.y
            if y > size.height || y + lane.height < rh { continue }
            let rect = CGRect(x: hw, y: y, width: size.width - hw, height: lane.height)
            c.fill(Path(rect), with: .color(lane.kind == .automation ? Palette.laneA.opacity(0.7) :
                                                (i % 2 == 0 ? Palette.laneA : Palette.laneB)))
            if model.selectedTrack == lane.track.name && lane.kind != .automation {
                c.fill(Path(rect), with: .color(Color.white.opacity(0.035)))
            }
        }
        // grid
        var gc = c
        gc.clip(to: Path(CGRect(x: hw, y: rh, width: size.width - hw, height: size.height - rh)))
        let barStep = ppb < 8 ? 8 : (ppb < 16 ? 4 : (ppb < 40 ? 1 : 1))
        var b = Int(floor(visibleBars.0))
        while Double(b) <= visibleBars.1 + 1 {
            let x = x0 + CGFloat(b) * ppb
            if b % barStep == 0 {
                gc.stroke(Path { p in p.move(to: CGPoint(x: x, y: rh)); p.addLine(to: CGPoint(x: x, y: size.height)) },
                          with: .color(b % 4 == 0 ? Palette.gridStrong : Palette.grid), lineWidth: 1)
            }
            if ppb >= 60 {
                for q in 1..<4 {
                    let xb = x + CGFloat(q) * ppb / 4
                    gc.stroke(Path { p in p.move(to: CGPoint(x: xb, y: rh)); p.addLine(to: CGPoint(x: xb, y: size.height)) },
                              with: .color(Palette.grid.opacity(0.5)), lineWidth: 0.5)
                }
            }
            b += 1
        }
        if let secs = score?.sections {
            for s in secs {
                let x = x0 + CGFloat(s.bar) * ppb
                gc.stroke(Path { p in p.move(to: CGPoint(x: x, y: rh)); p.addLine(to: CGPoint(x: x, y: size.height)) },
                          with: .color(Color.white.opacity(0.22)), lineWidth: 1)
            }
        }
        // content
        for lane in lanes {
            let y = rh + lane.y - offset.y
            if y > size.height || y + lane.height < rh { continue }
            drawLane(gc, lane: lane, y: y, x0: x0, ppb: ppb, visible: visibleBars, playhead: playhead)
        }
        // headers
        for lane in lanes {
            let y = rh + lane.y - offset.y
            if y > size.height || y + lane.height < rh { continue }
            drawHeader(c, lane: lane, y: y)
        }
        // ruler
        drawRuler(ctx, size: size, x0: x0, ppb: ppb, visible: visibleBars, totalBars: totalBars)
        // end-of-song shade
        let endX = x0 + CGFloat(totalBars) * ppb
        if endX < size.width {
            ctx.fill(Path(CGRect(x: max(hw, endX), y: rh, width: size.width - max(hw, endX), height: size.height - rh)),
                     with: .color(Color.black.opacity(0.25)))
        }
        // playhead
        let px = x0 + CGFloat(playhead) * ppb
        if px >= hw {
            ctx.stroke(Path { p in p.move(to: CGPoint(x: px, y: 0)); p.addLine(to: CGPoint(x: px, y: size.height)) },
                       with: .color(Color(red: 1, green: 0.85, blue: 0.3)), lineWidth: 1.5)
            ctx.fill(Path { p in
                p.move(to: CGPoint(x: px - 6, y: rh - 10)); p.addLine(to: CGPoint(x: px + 6, y: rh - 10))
                p.addLine(to: CGPoint(x: px, y: rh - 2)); p.closeSubpath()
            }, with: .color(Color(red: 1, green: 0.85, blue: 0.3)))
        }
        // header column border
        ctx.fill(Path(CGRect(x: hw - 1, y: 0, width: 1, height: size.height)), with: .color(.black.opacity(0.6)))
    }

    private func drawLane(_ ctx: GraphicsContext, lane: Lane, y: CGFloat, x0: CGFloat, ppb: CGFloat,
                          visible: (CGFloat, CGFloat), playhead: Double) {
        let color = Palette.color(lane.colorIndex)
        let lo = Double(visible.0) - 8, hi = Double(visible.1) + 1
        switch lane.kind {
        case .notes:
            let span = max(1, lane.pitchHi - lane.pitchLo)
            let usable = lane.height - 10
            let nh = max(2, min(8 * model.laneScale, usable / CGFloat(span)))
            for n in lane.track.notes where n.bar < hi && n.bar + n.dur_bars > lo {
                guard let p = n.pitch else { continue }
                let ny = y + 5 + CGFloat((lane.pitchHi - p) / span) * usable - nh / 2
                let r = CGRect(x: x0 + CGFloat(n.bar) * ppb, y: ny, width: max(1.5, CGFloat(n.dur_bars) * ppb - 0.5), height: nh)
                let active = model.isPlaying && playhead >= n.bar && playhead < n.bar + n.dur_bars
                ctx.fill(Path(roundedRect: r, cornerRadius: min(2, nh / 2)),
                         with: .color(active ? .white : color.opacity(0.45 + 0.55 * n.vel)))
                if n.slide && ppb > 20 {
                    ctx.fill(Path(ellipseIn: CGRect(x: r.minX - 1.5, y: r.midY - 1.5, width: 3, height: 3)), with: .color(.white))
                }
            }
        case .drums, .hits:
            let rh = rowH * model.laneScale
            for ri in lane.rows.indices {
                let ry = y + 4 + CGFloat(ri) * rh
                ctx.fill(Path(CGRect(x: x0 + CGFloat(max(0, lo)) * ppb, y: ry + rh / 2, width: CGFloat(hi - max(0, lo)) * ppb, height: 1)),
                         with: .color(Color.white.opacity(0.04)))
            }
            for n in lane.track.notes where n.bar < hi && n.bar > lo {
                let key = lane.kind == .drums ? (n.drum ?? "") : (n.slice.map { "slice \($0)" } ?? "hit")
                guard let ri = lane.rows.firstIndex(of: key) else { continue }
                let ry = y + 4 + CGFloat(ri) * rh
                let w = max(2, min(CGFloat(n.dur_bars) * ppb * 0.9, ppb / 8))
                let active = model.isPlaying && playhead >= n.bar && playhead < n.bar + 0.06
                let r = CGRect(x: x0 + CGFloat(n.bar) * ppb, y: ry + 1, width: w, height: rh - 3)
                ctx.fill(Path(roundedRect: r, cornerRadius: 1.5),
                         with: .color(active ? .white : color.opacity(0.35 + 0.65 * n.vel)))
            }
        case .automation:
            guard let a = lane.auto, !a.points.isEmpty else { return }
            let vals = a.points.map { $0[1] }
            let vmin = vals.min()!, vmax = vals.max()!
            let rng = max(vmax - vmin, 1e-9)
            func py(_ v: Double) -> CGFloat { y + lane.height - 5 - CGFloat((v - vmin) / rng) * (lane.height - 10) }
            var path = Path(), fill = Path()
            var started = false
            for pt in a.points where pt[0] >= lo - 1 && pt[0] <= hi + 1 {
                let p = CGPoint(x: x0 + CGFloat(pt[0]) * ppb, y: vmax == vmin ? y + lane.height / 2 : py(pt[1]))
                if !started { path.move(to: p); fill.move(to: CGPoint(x: p.x, y: y + lane.height)); started = true }
                path.addLine(to: p)
                fill.addLine(to: p)
            }
            if started {
                fill.addLine(to: CGPoint(x: path.currentPoint?.x ?? 0, y: y + lane.height))
                fill.closeSubpath()
                ctx.fill(fill, with: .color(color.opacity(0.12)))
                ctx.stroke(path, with: .color(color.opacity(0.9)), lineWidth: 1.3)
            }
        }
    }

    private func drawHeader(_ ctx: GraphicsContext, lane: Lane, y: CGFloat) {
        let hw = Self.headerW
        let color = Palette.color(lane.colorIndex)
        let rect = CGRect(x: 0, y: y, width: hw - 1, height: lane.height)
        ctx.fill(Path(rect), with: .color(Palette.header))
        ctx.fill(Path(CGRect(x: 0, y: y + lane.height - 1, width: hw, height: 1)), with: .color(.black.opacity(0.5)))
        let t = lane.track
        if lane.kind == .automation {
            ctx.draw(Text("↳ " + (lane.auto?.param ?? "")).font(.system(size: 10.5)).foregroundColor(color.opacity(0.9)),
                     at: CGPoint(x: 16, y: y + lane.height / 2), anchor: .leading)
            return
        }
        ctx.fill(Path(CGRect(x: 0, y: y, width: 4, height: lane.height)), with: .color(color))
        let selected = model.selectedTrack == t.name
        ctx.draw(Text(t.name).font(.system(size: 12.5, weight: .semibold)).foregroundColor(selected ? .white : .white.opacity(0.9)),
                 at: CGPoint(x: 12, y: y + 11), anchor: .leading)
        let icon = t.lane == "drums" ? "drums" : (t.lane == "sampler" ? "sampler" : t.engine)
        var sub = "\(t.preset) · \(icon)"
        if t.gain != 0 { sub += String(format: " · %+.1f dB", t.gain) }
        ctx.draw(Text(sub).font(.system(size: 10)).foregroundColor(.white.opacity(0.5)),
                 at: CGPoint(x: 12, y: y + 25), anchor: .leading)
        var chips = t.effects
        if t.duck > 0 { chips.append("duck") }
        for (k, v) in t.sends.sorted(by: { $0.key < $1.key }) where v > 0 { chips.append("→\(k)") }
        if lane.height >= 46 && !chips.isEmpty {
            var cx: CGFloat = 12
            for chip in chips {
                let w = CGFloat(chip.count) * 5.6 + 8
                if cx + w > hw - 6 { break }
                let r = CGRect(x: cx, y: y + 33, width: w, height: 12)
                ctx.fill(Path(roundedRect: r, cornerRadius: 3), with: .color(color.opacity(chip.hasPrefix("→") ? 0.12 : 0.22)))
                ctx.draw(Text(chip).font(.system(size: 8.5, weight: .medium)).foregroundColor(.white.opacity(0.85)),
                         at: CGPoint(x: r.midX, y: r.midY))
                cx += w + 3
            }
        }
        if lane.kind == .drums || lane.kind == .hits {
            for (ri, row) in lane.rows.enumerated() where lane.height > 30 {
                let ry = y + 4 + (CGFloat(ri) + 0.5) * rowH * model.laneScale
                ctx.draw(Text(row).font(.system(size: 9, design: .monospaced)).foregroundColor(.white.opacity(0.4)),
                         at: CGPoint(x: hw - 8, y: ry), anchor: .trailing)
            }
        } else if lane.kind == .notes {
            ctx.draw(Text(noteName(lane.pitchHi - 1)).font(.system(size: 8.5, design: .monospaced)).foregroundColor(.white.opacity(0.35)),
                     at: CGPoint(x: hw - 8, y: y + 7), anchor: .trailing)
            ctx.draw(Text(noteName(lane.pitchLo + 1)).font(.system(size: 8.5, design: .monospaced)).foregroundColor(.white.opacity(0.35)),
                     at: CGPoint(x: hw - 8, y: y + lane.height - 7), anchor: .trailing)
        }
    }

    private func drawRuler(_ ctx: GraphicsContext, size: CGSize, x0: CGFloat, ppb: CGFloat, visible: (CGFloat, CGFloat),
                           totalBars: Double) {
        let hw = Self.headerW, rh = Self.rulerH
        ctx.fill(Path(CGRect(x: 0, y: 0, width: size.width, height: rh)), with: .color(Palette.header))
        var c = ctx
        c.clip(to: Path(CGRect(x: hw, y: 0, width: size.width - hw, height: rh)))
        if let secs = model.score?.sections {
            for (i, s) in secs.enumerated() {
                let r = CGRect(x: x0 + CGFloat(s.bar) * ppb + 1, y: 2, width: CGFloat(s.bars) * ppb - 2, height: 16)
                if r.maxX < hw || r.minX > size.width { continue }
                let col = Palette.color(i + 3)
                c.fill(Path(roundedRect: r, cornerRadius: 3), with: .color(col.opacity(0.28)))
                // Sticky label: pinned to the left edge only while its own section is on screen,
                // and pushed out by the section's end instead of piling up with earlier labels.
                let labelW = CGFloat(s.name.count) * 6.6 + 8
                let x = min(max(r.minX + 6, hw + 6), r.maxX - labelW)
                if x + 4 < r.minX || r.width < 14 { continue }
                var lc = c
                lc.clip(to: Path(r))
                lc.draw(Text(s.name).font(.system(size: 10.5, weight: .semibold)).foregroundColor(.white.opacity(0.92)),
                        at: CGPoint(x: x, y: r.midY), anchor: .leading)
            }
        }
        let step = ppb < 6 ? 16 : (ppb < 12 ? 8 : (ppb < 24 ? 4 : (ppb < 50 ? 2 : 1)))
        var b = Int(floor(visible.0 / CGFloat(step))) * step
        while CGFloat(b) <= visible.1 + 1 {
            let x = x0 + CGFloat(b) * ppb
            c.stroke(Path { p in p.move(to: CGPoint(x: x, y: rh - 12)); p.addLine(to: CGPoint(x: x, y: rh)) },
                     with: .color(.white.opacity(0.35)), lineWidth: 1)
            c.draw(Text("\(b + 1)").font(.system(size: 9.5, design: .monospaced)).foregroundColor(.white.opacity(0.6)),
                   at: CGPoint(x: x + 3, y: rh - 9), anchor: .leading)
            b += step
        }
        ctx.fill(Path(CGRect(x: 0, y: rh - 1, width: size.width, height: 1)), with: .color(.black.opacity(0.6)))
        if let s = model.score {
            ctx.draw(Text("\(Int(s.tempo ?? 120)) BPM · \(s.key ?? "")").font(.system(size: 11, weight: .medium))
                        .foregroundColor(.white.opacity(0.7)), at: CGPoint(x: 12, y: rh / 2), anchor: .leading)
        }
    }
}

func noteName(_ midi: Double) -> String {
    let names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    let n = Int(midi.rounded())
    return names[((n % 12) + 12) % 12] + "\(n / 12 - 1)"
}

/// Captures trackpad/mouse scrolling, pinch zoom, clicks and hover over the canvas.
struct InputCatcher: NSViewRepresentable {
    var onScroll: (CGFloat, CGFloat, Bool, Bool, CGPoint) -> Void  // dx, dy, zoom, vertical zoom, location
    var onMagnify: (CGFloat, Bool, CGPoint) -> Void  // factor, vertical, location
    var onClick: (CGPoint) -> Void
    var onDoubleClick: (CGPoint) -> Void
    var onHover: (CGPoint) -> Void

    func makeNSView(context: Context) -> CatcherView {
        let v = CatcherView()
        v.handlers = self
        return v
    }

    func updateNSView(_ v: CatcherView, context: Context) { v.handlers = self }

    final class CatcherView: NSView {
        var handlers: InputCatcher?
        override var isFlipped: Bool { true }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

        override func updateTrackingAreas() {
            trackingAreas.forEach(removeTrackingArea)
            addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseMoved, .activeInKeyWindow, .inVisibleRect, .mouseEnteredAndExited],
                                           owner: self))
        }

        private func loc(_ e: NSEvent) -> CGPoint { convert(e.locationInWindow, from: nil) }

        override func scrollWheel(with e: NSEvent) {
            var dx = e.scrollingDeltaX, dy = e.scrollingDeltaY
            if !e.hasPreciseScrollingDeltas { dx *= 8; dy *= 8 }
            let vzoom = e.modifierFlags.contains(.option)
            let zoom = !vzoom && (e.modifierFlags.contains(.command) || e.modifierFlags.contains(.control))
            if e.modifierFlags.contains(.shift) && dx == 0 { dx = dy; dy = 0 }
            handlers?.onScroll(dx, dy, zoom, vzoom, loc(e))
        }

        override func magnify(with e: NSEvent) {
            handlers?.onMagnify(e.magnification, e.modifierFlags.contains(.option), loc(e))
        }
        override func mouseDown(with e: NSEvent) {
            if e.clickCount == 2 { handlers?.onDoubleClick(loc(e)) } else { handlers?.onClick(loc(e)) }
        }
        override func mouseDragged(with e: NSEvent) {
            let p = loc(e)
            if p.y < ArrangementView.rulerH { handlers?.onClick(p) }
        }
        override func mouseMoved(with e: NSEvent) { handlers?.onHover(loc(e)) }
        override func mouseExited(with e: NSEvent) { handlers?.onHover(CGPoint(x: -1, y: -1)) }
    }
}
