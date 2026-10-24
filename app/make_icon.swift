// Draws the Pulsar Studio app icon (1024x1024 PNG). Run: swift app/make_icon.swift out.png
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon_1024.png"
let cs = CGColorSpace(name: CGColorSpace.sRGB)!
let ctx = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8, bytesPerRow: 0,
                    space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!

func color(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}
let amber = color(0xffc94d), pink = color(0xff6fae), violet = color(0xa98bff)
let accent = CGGradient(colorsSpace: cs, colors: [amber, pink, violet] as CFArray, locations: [0, 0.55, 1])!

// macOS-style rounded square with a margin (Big Sur icon grid: 824/1024 body)
let inset: CGFloat = 100
let body = CGRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
let radius: CGFloat = 185
let tile = CGPath(roundedRect: body, cornerWidth: radius, cornerHeight: radius, transform: nil)

// drop shadow
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 40, color: color(0x000000, 0.45))
ctx.addPath(tile); ctx.setFillColor(color(0x15171c)); ctx.fillPath()
ctx.restoreGState()

// background gradient
ctx.saveGState()
ctx.addPath(tile); ctx.clip()
let bg = CGGradient(colorsSpace: cs, colors: [color(0x23262e), color(0x101115)] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 0, y: body.maxY), end: CGPoint(x: 0, y: body.minY), options: [])
// soft colour glow behind the mark
let glow = CGGradient(colorsSpace: cs, colors: [color(0xff6fae, 0.30), color(0xa98bff, 0.10), color(0x000000, 0)] as CFArray,
                      locations: [0, 0.5, 1])!
ctx.drawRadialGradient(glow, startCenter: CGPoint(x: size / 2, y: size / 2 + 20), startRadius: 0,
                       endCenter: CGPoint(x: size / 2, y: size / 2 + 20), endRadius: 420, options: [])
// faint arrangement lanes, a nod to the DAW view
for (i, y) in stride(from: body.minY + 120, to: body.maxY - 100, by: 92).enumerated() {
    let w = [0.42, 0.62, 0.30, 0.55, 0.47, 0.36, 0.58][i % 7] * body.width
    let x = body.minX + 90 + CGFloat((i * 53) % 120)
    let bar = CGPath(roundedRect: CGRect(x: x, y: y, width: w, height: 18), cornerWidth: 9, cornerHeight: 9, transform: nil)
    ctx.addPath(bar); ctx.setFillColor(color(0xffffff, 0.035)); ctx.fillPath()
}
ctx.restoreGState()

// gradient ring
let c = CGPoint(x: size / 2, y: size / 2)
let ringR: CGFloat = 290
ctx.saveGState()
ctx.setLineWidth(34)
ctx.addEllipse(in: CGRect(x: c.x - ringR, y: c.y - ringR, width: 2 * ringR, height: 2 * ringR))
ctx.replacePathWithStrokedPath(); ctx.clip()
ctx.drawLinearGradient(accent, start: CGPoint(x: c.x - ringR, y: c.y + ringR), end: CGPoint(x: c.x + ringR, y: c.y - ringR), options: [])
ctx.restoreGState()

// pulse waveform through the ring
let pts: [(CGFloat, CGFloat)] = [(-330, 0), (-150, 0), (-95, 150), (-20, -230), (60, 175), (120, -60), (165, 0), (330, 0)]
let wave = CGMutablePath()
for (i, p) in pts.enumerated() {
    let q = CGPoint(x: c.x + p.0, y: c.y + p.1)
    if i == 0 { wave.move(to: q) } else { wave.addLine(to: q) }
}
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 28, color: color(0xff6fae, 0.8))
ctx.addPath(wave); ctx.setLineWidth(46); ctx.setLineCap(.round); ctx.setLineJoin(.round)
ctx.replacePathWithStrokedPath(); ctx.clip()
ctx.drawLinearGradient(accent, start: CGPoint(x: c.x - 330, y: c.y), end: CGPoint(x: c.x + 330, y: c.y), options: [])
ctx.restoreGState()

// subtle top highlight on the tile edge
ctx.saveGState()
ctx.addPath(tile); ctx.setLineWidth(3); ctx.setStrokeColor(color(0xffffff, 0.08)); ctx.strokePath()
ctx.restoreGState()

let img = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: img)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
