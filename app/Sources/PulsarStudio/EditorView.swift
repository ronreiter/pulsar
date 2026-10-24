import AppKit
import SwiftUI

/// Syntax-highlighted Pulsar editor (NSTextView) with line numbers and an error marker.
struct EditorView: NSViewRepresentable {
    @Binding var text: String
    var errorLine: Int?
    @Binding var jumpToLine: Int?

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        // TextKit 1 from the start: the line-number ruler and the highlighter use the
        // layout manager, and touching it on a TextKit 2 view breaks drawing.
        let scroll = NSScrollView()
        let tv = NSTextView(usingTextLayoutManager: false)
        tv.frame = NSRect(x: 0, y: 0, width: 600, height: 400)
        scroll.documentView = tv
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.allowsUndo = true
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false
        tv.isAutomaticSpellingCorrectionEnabled = false
        tv.isContinuousSpellCheckingEnabled = false
        tv.font = Theme.font
        tv.textColor = Theme.text
        tv.backgroundColor = Theme.editorBG
        tv.insertionPointColor = .white
        tv.textContainerInset = NSSize(width: 6, height: 8)
        tv.usesFindBar = true  // the inline find bar (⌘F), like TextEdit and Xcode
        tv.isIncrementalSearchingEnabled = true
        tv.string = text
        // Lay text out across the full width and keep it there as the split view resizes.
        tv.minSize = NSSize(width: 0, height: 0)
        tv.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.containerSize = NSSize(width: scroll.contentSize.width, height: CGFloat.greatestFiniteMagnitude)
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = true
        scroll.backgroundColor = Theme.editorBG
        let ruler = LineNumberRuler(textView: tv)
        scroll.verticalRulerView = ruler
        scroll.hasVerticalRuler = true
        scroll.rulersVisible = true
        context.coordinator.textView = tv
        context.coordinator.ruler = ruler
        NotificationCenter.default.addObserver(context.coordinator, selector: #selector(Coordinator.find(_:)),
                                               name: .pulsarFind, object: nil)
        Highlighter.apply(to: tv, errorLine: errorLine)
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = context.coordinator.textView else { return }
        context.coordinator.parent = self
        if tv.string != text {
            let sel = tv.selectedRanges
            tv.string = text
            tv.selectedRanges = sel.filter { $0.rangeValue.upperBound <= (text as NSString).length }
            context.coordinator.needsHighlight = true
        }
        if context.coordinator.lastErrorLine != errorLine || context.coordinator.needsHighlight {
            context.coordinator.lastErrorLine = errorLine
            context.coordinator.needsHighlight = false
            Highlighter.apply(to: tv, errorLine: errorLine)
            context.coordinator.ruler?.errorLine = errorLine
            context.coordinator.ruler?.needsDisplay = true
        }
        if let line = jumpToLine {
            DispatchQueue.main.async { self.jumpToLine = nil }
            let ns = tv.string as NSString
            var idx = 0, current = 1
            while current < line && idx < ns.length {
                let r = ns.lineRange(for: NSRange(location: idx, length: 0))
                idx = NSMaxRange(r)
                current += 1
            }
            let r = ns.lineRange(for: NSRange(location: min(idx, ns.length), length: 0))
            tv.window?.makeFirstResponder(tv)
            tv.setSelectedRange(NSRange(location: r.location, length: 0))
            tv.scrollRangeToVisible(r)
            tv.showFindIndicator(for: r)
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: EditorView
        weak var textView: NSTextView?
        weak var ruler: LineNumberRuler?
        var lastErrorLine: Int?
        var needsHighlight = false

        init(_ parent: EditorView) { self.parent = parent }

        /// Menu commands post this with an NSTextFinder.Action; route it to the editor even if
        /// the arrangement currently has focus.
        @objc func find(_ note: Notification) {
            guard let tv = textView, let action = note.object as? NSTextFinder.Action else { return }
            tv.window?.makeFirstResponder(tv)
            let item = NSMenuItem()
            item.tag = action.rawValue
            tv.performTextFinderAction(item)
        }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            parent.text = tv.string
            Highlighter.apply(to: tv, errorLine: parent.errorLine)
            ruler?.needsDisplay = true
        }

        func textView(_ textView: NSTextView, doCommandBy sel: Selector) -> Bool {
            // Keep indentation on newline; Tab inserts two spaces.
            if sel == #selector(NSResponder.insertNewline(_:)) {
                let ns = textView.string as NSString
                let r = ns.lineRange(for: NSRange(location: textView.selectedRange().location, length: 0))
                let line = ns.substring(with: r)
                var indent = String(line.prefix { $0 == " " })
                if line.trimmingCharacters(in: .whitespacesAndNewlines).hasSuffix("{") { indent += "  " }
                textView.insertText("\n" + indent, replacementRange: textView.selectedRange())
                return true
            }
            if sel == #selector(NSResponder.insertTab(_:)) {
                textView.insertText("  ", replacementRange: textView.selectedRange())
                return true
            }
            return false
        }
    }
}

enum Theme {
    static let font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .regular)
    static let editorBG = NSColor(calibratedRed: 0.11, green: 0.115, blue: 0.13, alpha: 1)
    static let text = NSColor(calibratedWhite: 0.86, alpha: 1)
    static let keyword = NSColor(calibratedRed: 0.98, green: 0.47, blue: 0.62, alpha: 1)
    static let builtin = NSColor(calibratedRed: 0.42, green: 0.78, blue: 1.0, alpha: 1)
    static let string = NSColor(calibratedRed: 0.62, green: 0.86, blue: 0.47, alpha: 1)
    static let number = NSColor(calibratedRed: 1.0, green: 0.72, blue: 0.38, alpha: 1)
    static let comment = NSColor(calibratedWhite: 0.45, alpha: 1)
    static let op = NSColor(calibratedRed: 0.78, green: 0.62, blue: 1.0, alpha: 1)
    static let errorBG = NSColor(calibratedRed: 0.55, green: 0.12, blue: 0.14, alpha: 0.45)
}

enum Highlighter {
    static let keywords = ["tempo", "key", "swing", "seed", "loudness", "ceiling", "track", "bus", "master", "section",
                           "extends", "play", "from", "to", "at", "mute", "automate", "arrange", "let", "fn", "return",
                           "if", "elif", "else", "for", "in", "repeat", "true", "false", "none", "and", "or", "not"]
    static let builtins = ["synth", "drums", "analog", "fm", "pluck", "sampler", "reverb", "delay", "chorus", "drive",
                           "dist", "crush", "lofi", "filter", "lp", "hp", "eq", "comp", "width", "phaser", "tremolo",
                           "gain", "pan", "tone", "seq", "cat", "stack", "euclid", "ramp", "lfo", "print", "range",
                           "len", "rand", "choose", "note", "chord", "silence"]
    private static let rules: [(NSRegularExpression, NSColor)] = {
        func re(_ p: String) -> NSRegularExpression { try! NSRegularExpression(pattern: p, options: [.anchorsMatchLines]) }
        return [
            (re("\\b(" + builtins.joined(separator: "|") + ")(?=\\s*\\()"), Theme.builtin),
            (re("\\b(" + keywords.joined(separator: "|") + ")\\b"), Theme.keyword),
            (re("(?<![A-Za-z_])-?\\d+(\\.\\d+)?"), Theme.number),
            (re(">>|->|=>|\\.\\."), Theme.op),
            (re("\"(\\\\.|[^\"\\\\\\n])*\"|'(\\\\.|[^'\\\\\\n])*'|`[^`]*`"), Theme.string),
            (re("(//|#)[^\\n]*"), Theme.comment),
        ]
    }()

    static func apply(to tv: NSTextView, errorLine: Int?) {
        guard let storage = tv.textStorage else { return }
        let ns = storage.string as NSString
        let full = NSRange(location: 0, length: ns.length)
        storage.beginEditing()
        storage.setAttributes([.font: Theme.font, .foregroundColor: Theme.text], range: full)
        for (regex, color) in rules {
            regex.enumerateMatches(in: storage.string, range: full) { m, _, _ in
                if let r = m?.range { storage.addAttribute(.foregroundColor, value: color, range: r) }
            }
        }
        if let line = errorLine, line > 0 {
            var idx = 0, current = 1
            while current < line && idx < ns.length {
                idx = NSMaxRange(ns.lineRange(for: NSRange(location: idx, length: 0)))
                current += 1
            }
            if idx <= ns.length {
                let r = ns.lineRange(for: NSRange(location: min(idx, ns.length), length: 0))
                storage.addAttribute(.backgroundColor, value: Theme.errorBG, range: r)
            }
        }
        storage.endEditing()
        tv.typingAttributes = [.font: Theme.font, .foregroundColor: Theme.text]
    }
}

/// Line numbers in the editor gutter.
final class LineNumberRuler: NSRulerView {
    weak var textView: NSTextView?
    var errorLine: Int?

    init(textView: NSTextView) {
        self.textView = textView
        super.init(scrollView: textView.enclosingScrollView, orientation: .verticalRuler)
        ruleThickness = 40
        clientView = textView
        NotificationCenter.default.addObserver(self, selector: #selector(redraw), name: NSView.boundsDidChangeNotification,
                                               object: textView.enclosingScrollView?.contentView)
    }

    required init(coder: NSCoder) { fatalError() }

    @objc private func redraw() { needsDisplay = true }

    override var requiredThickness: CGFloat { ruleThickness }

    override func drawHashMarksAndLabels(in rect: NSRect) {
        NSColor(calibratedRed: 0.09, green: 0.095, blue: 0.11, alpha: 1).setFill()
        NSRect(x: 0, y: rect.minY, width: ruleThickness, height: rect.height).fill()
        guard let tv = textView, let lm = tv.layoutManager, let tc = tv.textContainer else { return }
        let ns = tv.string as NSString
        let visible = tv.visibleRect
        let glyphs = lm.glyphRange(forBoundingRect: visible, in: tc)
        let chars = lm.characterRange(forGlyphRange: glyphs, actualGlyphRange: nil)
        var line = 1
        var i = 0
        while i < chars.location {
            if ns.character(at: i) == 10 { line += 1 }
            i += 1
        }
        let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .regular),
                                                    .foregroundColor: NSColor(calibratedWhite: 0.42, alpha: 1)]
        let errAttrs: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .bold),
                                                       .foregroundColor: NSColor.systemRed]
        var idx = chars.location
        let yOffset = tv.textContainerInset.height - visible.minY
        while idx <= NSMaxRange(chars) && idx <= ns.length {
            let lr = ns.lineRange(for: NSRange(location: min(idx, ns.length), length: 0))
            let gr = lm.glyphRange(forCharacterRange: NSRange(location: lr.location, length: 0), actualCharacterRange: nil)
            let frag = lm.lineFragmentRect(forGlyphAt: min(gr.location, max(lm.numberOfGlyphs - 1, 0)), effectiveRange: nil)
            let label = "\(line)" as NSString
            let a = line == errorLine ? errAttrs : attrs
            let size = label.size(withAttributes: a)
            label.draw(at: NSPoint(x: ruleThickness - size.width - 6, y: frag.minY + yOffset + 1), withAttributes: a)
            if NSMaxRange(lr) <= idx { break }
            idx = NSMaxRange(lr)
            line += 1
            if idx >= ns.length { break }
        }
    }
}


extension Notification.Name {
    static let pulsarFind = Notification.Name("PulsarFind")
}

/// Ask the editor to run a find action (show the find bar, find next, ...).
func pulsarFind(_ action: NSTextFinder.Action) {
    NotificationCenter.default.post(name: .pulsarFind, object: action)
}
