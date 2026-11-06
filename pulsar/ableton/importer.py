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


def write_pulse(set_path: str | Path, out: str | Path, calibration: str | Path | None = None) -> Path:
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
