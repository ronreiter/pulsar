"""Turn an Ableton Live set into a Pulsar song: `pulsar import-ableton set.als -o song.pulse`.

The song names the set (`ableton_set(...)`), declares one `ableton_track(...)` per MIDI track, and
writes every note into clips, one section per locator. Rendering it plays those notes through the
set's instruments, device chains and mix (pulsar.ableton.engine), so the notes can be edited in
Pulsar -- or Pulsar Studio's note editor -- and re-rendered.
"""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

from ..clip import _fmt, clip_source, note_name
from . import als

RESERVED = {"tempo", "key", "swing", "seed", "loudness", "ceiling", "track", "bus", "master", "section", "play",
            "mute", "automate", "arrange", "let", "fn", "if", "elif", "else", "for", "in", "repeat", "return",
            "from", "to", "at", "extends", "true", "false", "none", "and", "or", "not"}


def ident(name: str, taken: set) -> str:
    from ..interp import BUILTINS
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "track"
    if base[0].isdigit():
        base = "t_" + base
    if base in BUILTINS or base in RESERVED:
        base += "_part"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    taken.add(out)
    return out


def sections(live: dict) -> list[tuple[str, float, float]]:
    """(name, start beat, end beat) from the locators; the whole song if there are none."""
    end = live["length_beats"]
    locs = [(b, n) for b, n in live["locators"] if b < end] or [(0.0, "song")]
    if locs[0][0] > 0:
        locs.insert(0, (0.0, "start"))
    out, taken = [], set()
    for (b0, nm), (b1, _) in zip(locs, locs[1:] + [(end, None)]):
        out.append((ident(nm, taken), b0, b1))
    return out


def write_pulse(set_path: str | Path, out: str | Path, calibration: str | Path | None = None,
                compact: bool = True) -> Path:
    """compact: each part written once as the shortest loop that repeats, shared between sections and
    tracks, with simple grids as plain patterns; otherwise every note of every section."""
    if compact:
        return write_compact(set_path, out, calibration)
    return write_full(set_path, out, calibration)


def write_full(set_path: str | Path, out: str | Path, calibration: str | Path | None = None) -> Path:
    set_path, out = Path(set_path).resolve(), Path(out)
    live = als.read(set_path)
    tempo = live["tempo"]
    secs = sections(live)
    taken: set = set()
    lines = [f"// Imported from {set_path.name} with pulsar import-ableton.",
             "// The notes are here (edit them freely); instruments, devices and the mix come from the set.",
             f"tempo {tempo:g}", ""]
    cal = f', calibration: "{Path(calibration).resolve()}"' if calibration else ""
    lines += [f'ableton_set("{set_path}"{cal})', ""]
    tracks = []
    for t in live["tracks"]:
        if t["kind"] != "midi":
            continue
        notes = als.arrangement_notes(t["midi_clips"], tempo)
        if not notes:
            continue
        name = ident(t["name"], taken)
        tracks.append((name, t["name"], notes))
        lines.append(f'track {name} = ableton_track("{t["name"]}")')
    lines.append("")
    spb = 60.0 / tempo
    for sname, b0, b1 in secs:
        bars = Fraction(b1 - b0).limit_denominator(64) / 4
        lines.append(f"section {sname} {_fmt(bars)} {{")
        for name, _, notes in tracks:
            mine = []
            for (t0, dur, key, vel) in notes:
                beat = t0 / spb - b0
                if 0 <= beat < b1 - b0 - 1e-9:
                    mine.append({"beat": _fmt(Fraction(beat).limit_denominator(960)),
                                 "len": _fmt(Fraction(dur / spb).limit_denominator(960)),
                                 "note": note_name(key, False), "vel": vel})
            if mine:
                lines.append(f"  play {name} " + clip_source(mine, int(-(-(b1 - b0) // 4)), "  "))
        lines.append("}")
        lines.append("")
    lines.append("arrange " + ", ".join(s for s, _, _ in secs))
    out.write_text("\n".join(lines) + "\n")
    return out


# -- compact songs ---------------------------------------------------------------------------------

def _bars(notes, b0: float, nbars: int, spb: float):
    """Per bar: a tuple of (beat in bar, length, key, velocity), quantized finely enough to be exact."""
    bars = [[] for _ in range(nbars)]
    for (t0, dur, key, vel) in notes:
        beat = t0 / spb - b0
        i = int(beat // 4)
        if 0 <= i < nbars:
            bars[i].append((round(beat - 4 * i, 4), round(dur / spb, 4), int(key), round(vel, 2)))
    return [tuple(sorted(b)) for b in bars]


def _period(bars):
    """The shortest loop (in bars) that the part repeats, over its active span."""
    act = [i for i, b in enumerate(bars) if b]
    if not act:
        return None
    a, z = act[0], act[-1] + 1
    span = bars[a:z]
    for per in (1, 2, 4, 8, 16, 32):
        if per > len(span):
            break
        if all(span[i] == span[i % per] for i in range(len(span))):
            return a, z, per
    return a, z, len(span)


def _mini(loop) -> str | None:
    """A loop as mini-notation when it's a plain grid: same length and velocity for every note."""
    notes = [n for bar in loop for n in bar]
    if not notes or len({n[1] for n in notes}) != 1 or len({n[3] for n in notes}) != 1:
        return None
    for steps in (4, 8, 16):
        size = 4 / steps
        if all(abs(n[0] / size - round(n[0] / size)) < 1e-6 and round(n[0] / size) < steps for n in notes):
            break
    else:
        return None
    bars = []
    for bar in loop:
        cells = [[] for _ in range(steps)]
        for beat, _, key, _ in bar:
            cells[min(steps - 1, int(round(beat / size)))].append(note_name(key, False))
        toks = ["~" if not c else c[0] if len(c) == 1 else "[" + ",".join(c) + "]" for c in cells]
        bars.append(_squash(toks))
    body = bars[0] if len(bars) == 1 else "<" + " ".join(f"[{b}]" for b in bars) + ">"
    length, vel = notes[0][1], notes[0][3]
    out = f'"{body}"'
    legato = length / size
    if abs(legato - 1) > 1e-3:
        out += f".legato({_fmt(Fraction(legato).limit_denominator(1000))})"
    if abs(vel - 0.8) > 1e-3:
        out += f".vel({vel:g})"
    return out


def _squash(toks: list[str]) -> str:
    """'c4 ~ c4 ~ c4 ~ c4 ~' -> '[c4 ~]*4' (the shortest repeating unit)."""
    n = len(toks)
    for unit in (1, 2, 4, 8):
        if unit < n and n % unit == 0 and toks == toks[:unit] * (n // unit):
            u = toks[:unit]
            return (u[0] if unit == 1 else "[" + " ".join(u) + "]") + f"*{n // unit}"
    return " ".join(toks)


def _clip(loop) -> str:
    rows = []
    for bar in loop:
        rows.append('"' + ", ".join(f"{_fmt(Fraction(b).limit_denominator(960))} {note_name(k, False)} "
                                    f"{_fmt(Fraction(l).limit_denominator(960))} {v:g}" for b, l, k, v in bar) + '"')
    if len(rows) == 1:
        return f"clip([{rows[0]}])"
    return "clip([\n" + "\n".join(f"  {r}," for r in rows) + "\n])"


def write_compact(set_path: str | Path, out: str | Path, calibration: str | Path | None = None) -> Path:
    set_path, out = Path(set_path).resolve(), Path(out)
    live = als.read(set_path)
    tempo, spb = live["tempo"], 60.0 / live["tempo"]
    secs = sections(live)
    taken: set = set()
    tracks = []
    for t in live["tracks"]:
        if t["kind"] == "midi":
            notes = als.arrangement_notes(t["midi_clips"], tempo)
            if notes:
                tracks.append((ident(t["name"], taken), t["name"], notes))
    loops: dict = {}      # loop content -> variable name
    lets: list = []
    plays: dict = {s: [] for s, _, _ in secs}
    for name, _, notes in tracks:
        for sname, b0, b1 in secs:
            nbars = int(-(-(b1 - b0) // 4))
            bars = _bars(notes, b0, nbars, spb)
            found = _period(bars)
            if found is None:
                continue
            a, z, per = found
            loop = tuple(bars[a:a + per])
            if loop not in loops:
                var = f"{name}_{sum(1 for v in loops.values() if v.startswith(name + '_')) + 1}"
                var = ident(var, taken)
                loops[loop] = var
                lets.append(f"let {var} = {_mini(loop) or _clip(loop)}")
            rng = (f" from {a}" if a else "") + (f" to {z}" if z < (b1 - b0) / 4 else "")
            plays[sname].append(f"play {name} {loops[loop]}{rng}")
    cal = f', calibration: "{Path(calibration).resolve()}"' if calibration else ""
    lines = [f"// {set_path.stem}: imported from Ableton Live. Notes and arrangement here; sounds and mix from the set.",
             f"tempo {tempo:g}", f'ableton_set("{set_path}"{cal})', ""]
    lines += [f'track {name} = ableton_track("{orig}")' for name, orig, _ in tracks] + [""]
    lines += lets + [""]
    for sname, b0, b1 in secs:
        bars = Fraction(b1 - b0).limit_denominator(64) / 4
        body = plays[sname]
        if len(body) <= 2:
            lines.append(f"section {sname} {_fmt(bars)} {{ " + "; ".join(body) + " }")
        else:
            lines += [f"section {sname} {_fmt(bars)} {{"] + [f"  {b}" for b in body] + ["}"]
    lines += ["", "arrange " + ", ".join(s for s, _, _ in secs)]
    out.write_text("\n".join(lines) + "\n")
    return out
