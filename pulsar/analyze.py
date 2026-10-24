"""Turn audio into numbers an author can act on without ears.

`analyze` measures a rendered mix (loudness, peaks, spectrum, stereo, the
energy arc across sections, per-track balance and masking) and turns the
measurements into plain-language suggestions.
"""
from __future__ import annotations

import math

import numpy as np
import pyloudnorm as pyln
from scipy.signal import welch

from . import theory

BANDS = [("sub", 20, 60), ("bass", 60, 250), ("low_mid", 250, 800), ("mid", 800, 2500),
         ("presence", 2500, 6000), ("high", 6000, 12000), ("air", 12000, 20000)]

# Rough share of energy (dB relative to the loudest band) in modern
# electronic masters; used only for gentle "this looks unusual" hints.
TYPICAL = {"sub": -2, "bass": 0, "low_mid": -6, "mid": -10, "presence": -14, "high": -18, "air": -26}


def _db(x):
    return round(20 * math.log10(max(float(x), 1e-12)), 1)


def band_energies(x: np.ndarray, sr: int) -> dict:
    mono = x.mean(axis=0) if x.ndim == 2 else x
    if len(mono) < 4096:
        return {b: -120.0 for b, _, _ in BANDS}
    f, p = welch(mono, sr, nperseg=8192)
    out = {}
    for name, lo, hi in BANDS:
        m = (f >= lo) & (f < hi)
        out[name] = round(10 * math.log10(max(float(p[m].sum()), 1e-20)), 1)
    return out


def describe_score(score) -> str:
    song = score.song
    lines = [f"tempo {song.tempo:g} BPM, key {song.key_name}, {score.total_bars:g} bars = {score.seconds:.1f}s"]
    lines.append("arrangement:")
    for name, start, bars in score.sections:
        playing = sorted({p.track for p in song.sections[name].plays})
        lines.append(f"  bar {start:>5g}  {name:<12} {bars:g} bars  [{', '.join(playing) or 'silence'}]")
    lines.append("tracks:")
    for name, tr in song.tracks.items():
        notes = score.notes.get(name, [])
        desc = f"{tr.inst.preset} ({tr.inst.engine})"
        if notes and tr.inst.kind in ("sampler", "voice"):
            pitched = [n.pitch for n in notes if n.pitch is not None]
            rng = (f" range {theory.midi_name(int(min(pitched)))}-{theory.midi_name(int(max(pitched)))}"
                   if pitched else " (as recorded)")
        elif notes and tr.inst.kind != "drums":
            lo = min(n.pitch for n in notes)
            hi = max(n.pitch for n in notes)
            rng = f" range {theory.midi_name(int(lo))}-{theory.midi_name(int(hi))}"
        elif notes:
            kinds = sorted({n.drum for n in notes})
            rng = f" voices {' '.join(kinds)}"
        else:
            rng = ""
        fx = " >> ".join(e.name for e in tr.inst.inserts)
        lines.append(f"  {name:<10} {desc:<22} {len(notes):>5} notes{rng}{('  fx: ' + fx) if fx else ''}")
    for w in score_warnings(score):
        lines.append(f"warning: {w}")
    return "\n".join(lines)


def score_warnings(score) -> list[str]:
    out = []
    song = score.song
    for name, tr in song.tracks.items():
        notes = score.notes.get(name, [])
        if not notes:
            out.append(f"track {name!r} never plays")
            continue
        if tr.inst.kind in ("drums", "sampler", "voice"):
            continue
        lo = min(n.pitch for n in notes)
        hi = max(n.pitch for n in notes)
        if lo < 21:
            out.append(f"track {name!r} goes down to {theory.midi_name(int(lo))}, below the audible bass range; "
                       f"raise its octave")
        if hi > 100:
            out.append(f"track {name!r} reaches {theory.midi_name(int(hi))}, which is shrill; lower its octave")
        if tr.inst.preset in ("sub", "bass", "acid", "reese", "moog_bass", "wobble", "fm_bass") and hi > 64:
            out.append(f"bass track {name!r} plays up to {theory.midi_name(int(hi))}; basslines usually stay below c4")
        if tr.inst.preset in ("pad", "strings", "supersaw", "chord", "dark_pad") and lo < 40:
            out.append(f"chord track {name!r} goes down to {theory.midi_name(int(lo))}; low chords get muddy, "
                       f"keep pads above e2 and let the bass take the low end")
    for name, start, bars in score.sections:
        if not song.sections[name].plays:
            out.append(f"section {name!r} is silent")
    return out


def analyze(x: np.ndarray, sr: int, score=None, res=None) -> dict:
    x = x.astype(np.float64)
    rep: dict = {}
    meter = pyln.Meter(sr)
    try:
        lufs = meter.integrated_loudness(x.T)
    except ValueError:
        lufs = -70.0
    rep["duration_s"] = round(x.shape[1] / sr, 2)
    rep["lufs"] = round(float(lufs), 1) if np.isfinite(lufs) else -70.0
    from .dsp.master import true_peak_env
    rep["true_peak_db"] = _db(true_peak_env(x.astype(np.float32)).max())
    rms = math.sqrt(float((x ** 2).mean()) + 1e-20)
    rep["crest_db"] = round(_db(np.abs(x).max()) - _db(rms), 1)
    # short-term loudness (3 s windows, 1 s hop) for loudness range and the energy arc
    st = short_term(x, sr)
    if len(st):
        valid = st[st > -70]
        if len(valid) > 2:
            rep["loudness_range_lu"] = round(float(np.percentile(valid, 95) - np.percentile(valid, 10)), 1)
    bands = band_energies(x, sr)
    top = max(bands.values())
    rep["spectrum_db"] = {k: round(v - top, 1) for k, v in bands.items()}
    l, r = x[0], x[1]
    denom = math.sqrt(float((l * l).sum() * (r * r).sum())) + 1e-20
    rep["stereo_correlation"] = round(float((l * r).sum()) / denom, 2)
    m, s = (l + r) / 2, (l - r) / 2
    rep["stereo_width"] = round(math.sqrt(float((s * s).mean())) / (math.sqrt(float((m * m).mean())) + 1e-12), 2)
    low_s = band_energies(np.stack([s, s]), sr)
    low_m = band_energies(np.stack([m, m]), sr)
    rep["low_end_side_db"] = round(max(low_s["sub"], low_s["bass"]) - max(low_m["sub"], low_m["bass"]), 1)
    if score is not None:
        rep["sections"] = section_arc(x, sr, score)
    if res is not None:
        rep["master"] = res.info.get("master", {})
        rep["tracks"] = res.info.get("tracks", {})
        rep["render_seconds"] = res.info.get("render_seconds")
    if res is not None:
        rep["track_bands"] = {k: v["bands"] for k, v in rep["tracks"].items() if "bands" in v}
    rep["suggestions"] = suggestions(rep, score)
    return rep


def short_term(x, sr, win=3.0, hop=1.0):
    meter = pyln.Meter(sr, block_size=0.4)
    n = int(win * sr)
    h = int(hop * sr)
    out = []
    for s in range(0, max(1, x.shape[1] - n + 1), h):
        seg = x[:, s:s + n]
        if seg.shape[1] < int(0.4 * sr):
            break
        try:
            v = meter.integrated_loudness(seg.T)
        except ValueError:
            v = -70
        out.append(v if np.isfinite(v) else -70)
    return np.array(out)


def section_arc(x, sr, score):
    bar = score.song.bar_seconds
    meter = pyln.Meter(sr, block_size=0.4)
    out = []
    for name, start, bars in score.sections:
        a, b = int(start * bar * sr), int((start + bars) * bar * sr)
        seg = x[:, a:b]
        try:
            v = meter.integrated_loudness(seg.T) if seg.shape[1] > sr else -70
        except ValueError:
            v = -70
        bands = band_energies(seg, sr)
        out.append({"section": name, "bar": start, "bars": bars,
                    "lufs": round(float(v), 1) if np.isfinite(v) else -70.0,
                    "low_db": round(max(bands["sub"], bands["bass"]), 1),
                    "high_db": round(max(bands["high"], bands["air"]), 1)})
    return out


def suggestions(rep, score) -> list[str]:
    out = []
    master = rep.get("master", {})
    red = master.get("max_reduction_db", 0)
    if red > 8:
        out.append(f"the limiter is pulling peaks down by {red} dB: something is much peakier than the rest "
                   f"(usually the kick or a resonant synth). Lower its gain or add comp() to it")
    sp = rep.get("spectrum_db", {})
    if sp:
        if sp.get("sub", 0) < -12 and sp.get("bass", 0) < -8:
            out.append("the low end is thin: add or raise a bass/sub track, or a longer kick (bd_decay)")
        if max(sp.get("sub", -99), sp.get("bass", -99)) == 0 and sp.get("mid", 0) < -20:
            out.append("the mix is all low end: the mids are 20 dB+ below the bass; raise leads/chords or lower the bass")
        if sp.get("low_mid", -99) > -2:
            out.append("the low-mids (250-800 Hz) are crowded, which sounds muddy: move pads/chords up an octave, "
                       "add hp(freq: 200) to pads, or lower their gain")
        if sp.get("presence", -99) > -6 or sp.get("high", -99) > -9:
            out.append("the top end is harsh: lower hats/leads or add lp() / eq(high: -3)")
        if sp.get("high", 0) < -30 and sp.get("air", 0) < -40:
            out.append("the mix is dull: there's almost nothing above 6 kHz; add hats/shakers or open filters")
    if rep.get("stereo_correlation", 1) < 0.2:
        out.append("the stereo image is very wide or phasey (correlation < 0.2) and may collapse in mono; "
                   "reduce width/spread on pads")
    if rep.get("low_end_side_db", -99) > -10:
        out.append("there's stereo content in the bass; keep the bass and kick mono (width: 0 on the bass track)")
    secs = rep.get("sections") or []
    if len(secs) >= 3:
        louds = [s["lufs"] for s in secs if s["lufs"] > -60]
        if louds and max(louds) - min(louds) < 2:
            out.append("every section is within 2 LU of the others: the arrangement has no dynamics. Thin out "
                       "intros/breakdowns (mute drums or bass) so drops hit harder")
    tracks = rep.get("tracks", {})
    tb = rep.get("track_bands", {})
    if tb:
        loud = {k: v for k, v in tb.items() if tracks.get(k, {}).get("rms_db", -99) > -40}
        names = list(loud)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = loud[names[i]], loud[names[j]]
                ba = max(a, key=a.get)
                bb = max(b, key=b.get)
                if ba == bb and ba in ("low_mid", "mid") and abs(a[ba] - b[bb]) < 6:
                    out.append(f"{names[i]} and {names[j]} both peak in the {ba} band and may mask each other; "
                               f"separate them by octave, eq, or pan")
    if score is not None:
        out += score_warnings(score)
    return out


def format_report(rep: dict) -> str:
    lines = []
    lines.append(f"loudness {rep['lufs']} LUFS   true peak {rep['true_peak_db']} dBTP   crest {rep['crest_db']} dB"
                 + (f"   range {rep['loudness_range_lu']} LU" if "loudness_range_lu" in rep else ""))
    sp = rep["spectrum_db"]
    lines.append("spectrum (dB vs loudest band): " + "  ".join(f"{k} {v:+.0f}" for k, v in sp.items()))
    hz = lambda f: f"{f / 1000:g}k" if f >= 1000 else str(f)
    lines.append("  bands (Hz): " + "  ".join(f"{n} {hz(lo)}-{hz(hi)}" for n, lo, hi in BANDS))
    lines.append(f"stereo: correlation {rep['stereo_correlation']}, width {rep['stereo_width']}, "
                 f"low-end side {rep['low_end_side_db']} dB")
    if rep.get("master"):
        m = rep["master"]
        lines.append(f"master: gain {m.get('gain_db')} dB, limiter max reduction {m.get('max_reduction_db')} dB")
    if rep.get("sections"):
        lines.append("energy arc:")
        for s in rep["sections"]:
            bar = "#" * max(0, int((s["lufs"] + 30) / 1.5))
            lines.append(f"  {s['section']:<12} bar {s['bar']:>4g}  {s['lufs']:>6} LUFS  {bar}")
    if rep.get("tracks"):
        lines.append("tracks (post-fader):")
        for k, v in rep["tracks"].items():
            if v.get("notes"):
                b = v.get("bands", {})
                main = max(b, key=b.get) if b else "?"
                lines.append(f"  {k:<10} peak {v['peak_db']:>6} dB  rms {v['rms_db']:>6} dB  {v['notes']:>4} notes"
                             f"  mostly {main}")
    for s in rep.get("suggestions", []):
        lines.append(f"- {s}")
    return "\n".join(lines)


# -- reference comparison -------------------------------------------------------

KEY_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def estimate_tempo(x, sr, lo=80, hi=160) -> float:
    """Tempo from the autocorrelation of a spectral-flux onset envelope."""
    from scipy.signal import stft
    mono = x.mean(axis=0)[: sr * 120]
    hop = 512
    _, _, Z = stft(mono, sr, nperseg=2048, noverlap=2048 - hop)
    mag = np.log1p(np.abs(Z))
    flux = np.maximum(np.diff(mag, axis=1), 0).sum(axis=0)
    flux = flux - flux.mean()
    ac = np.correlate(flux, flux, "full")[len(flux) - 1:]
    fps = sr / hop
    lags = np.arange(len(ac))
    bpm = 60 * fps / np.maximum(lags, 1)
    m = (bpm >= lo) & (bpm <= hi)
    best = lags[m][np.argmax(ac[m])]
    # refine with a parabolic fit around the peak
    if 1 <= best < len(ac) - 1:
        a, b, c = ac[best - 1], ac[best], ac[best + 1]
        best = best + 0.5 * (a - c) / (a - 2 * b + c + 1e-12)
    return round(float(60 * fps / best), 1)


def estimate_key(x, sr) -> str:
    """Krumhansl-Schmuckler key estimate from a long-term chroma vector."""
    mono = x.mean(axis=0)[: sr * 120]
    n = 1 << 15
    chroma = np.zeros(12)
    win = np.hanning(n)
    f = np.fft.rfftfreq(n, 1 / sr)
    band = (f > 55) & (f < 2000)
    pcs = (np.round(12 * np.log2(f[band] / 440.0)) + 9) % 12
    for s in range(0, len(mono) - n, n // 2):
        X = np.abs(np.fft.rfft(mono[s:s + n] * win))[band]
        np.add.at(chroma, pcs.astype(int), X)
    best, name = -2, ""
    for k in range(12):
        for prof, q in ((_MAJOR, "major"), (_MINOR, "minor")):
            r = np.corrcoef(chroma, np.roll(prof, k))[0, 1]
            if r > best:
                best, name = r, f"{KEY_NAMES[k]} {q}"
    return name


def profile(x: np.ndarray, sr: int) -> dict:
    """The sonic fingerprint of a mix (no melody or harmony beyond the key)."""
    rep = analyze(x, sr)
    mono = x.mean(axis=0)
    f, p = welch(mono, sr, nperseg=8192)
    centroid = float((f * p).sum() / (p.sum() + 1e-20))
    low = band_energies(x, sr)
    from scipy.signal import butter, sosfilt
    sos = butter(4, 150, "low", fs=sr, output="sos")
    lowsig = sosfilt(sos, mono)
    low_crest = _db(np.abs(lowsig).max()) - _db(np.sqrt((lowsig ** 2).mean()))
    return {
        "tempo_bpm": estimate_tempo(x, sr),
        "key": estimate_key(x, sr),
        "lufs": rep["lufs"],
        "true_peak_db": rep["true_peak_db"],
        "crest_db": rep["crest_db"],
        "loudness_range_lu": rep.get("loudness_range_lu"),
        "spectrum_db": rep["spectrum_db"],
        "centroid_hz": round(centroid),
        "stereo_width": rep["stereo_width"],
        "stereo_correlation": rep["stereo_correlation"],
        "low_end_punch_db": round(low_crest, 1),
    }


def compare(mine: dict, ref: dict) -> list[str]:
    out = []
    d = lambda k: (mine.get(k) or 0) - (ref.get(k) or 0)
    out.append(f"{'':22}{'yours':>12}{'reference':>12}{'diff':>9}")
    for k in ("tempo_bpm", "lufs", "true_peak_db", "crest_db", "loudness_range_lu", "centroid_hz",
              "stereo_width", "stereo_correlation", "low_end_punch_db"):
        a, b = mine.get(k), ref.get(k)
        if a is None or b is None:
            continue
        out.append(f"{k:22}{a:>12}{b:>12}{a - b:>+9.1f}")
    out.append(f"{'key':22}{mine['key']:>12}{ref['key']:>12}")
    out.append("spectrum (each band vs the loudest band):")
    for band in mine["spectrum_db"]:
        a, b = mine["spectrum_db"][band], ref["spectrum_db"][band]
        out.append(f"  {band:20}{a:>12}{b:>12}{a - b:>+9.1f}")
    tips = []
    for band, (a, b) in ((k, (mine["spectrum_db"][k], ref["spectrum_db"][k])) for k in mine["spectrum_db"]):
        if a - b > 3:
            tips.append(f"{band} is {a - b:.0f} dB hotter than the reference: lower the tracks living there or cut it with eq")
        elif b - a > 3:
            tips.append(f"{band} is {b - a:.0f} dB weaker than the reference: raise or brighten the tracks living there")
    if abs(d("stereo_width")) > 0.08:
        tips.append("wider than the reference: narrow pads/fx (width:)" if d("stereo_width") > 0 else
                    "narrower than the reference: add width to pads/fx, chorus, or reverb sends")
    if d("crest_db") > 2:
        tips.append("more dynamic (peakier) than the reference: more glue (automate master.glue) or louder target")
    elif d("crest_db") < -2:
        tips.append("more squashed than the reference: lower `loudness` or reduce glue")
    if abs(d("lufs")) > 1:
        tips.append(f"set `loudness {ref['lufs']:.0f}` to match the reference level")
    if d("low_end_punch_db") < -2:
        tips.append("the low end punches less than the reference: shorter bass notes, more duck, punchier kick (bd_punch)")
    return out + [f"- {t}" for t in tips]
