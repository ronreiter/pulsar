"""Clips: a track's notes in one section written out as concrete notes, so they can be edited by hand.

    play melody clip([
      "0 a5 1.5 .78, 1.5 f#5 1.5 .62, 3 e5 1 .78",   // bar 1: beat, note, length (beats), velocity
      "0 d5 2 .62",                                  // bar 2
    ])

`read_band` turns whatever a section plays on a track (patterns, functions, chords, arps) into
notes; `write_band` puts edited notes back into the source as one `play track clip([...])`,
replacing that track's plays in the section.
"""
from __future__ import annotations

import math
import re
from fractions import Fraction as F

from . import theory
from .errors import PulsarError
from .pattern import Ctx, Ev, Pattern, resolve

BEATS = 4  # beats per bar: Pulsar bars are 4/4
FLAGS = ("slide", "accent")


def _num(tok: str, what: str, where: str) -> F:
    try:
        v = F(tok)
    except (ValueError, ZeroDivisionError):
        raise PulsarError(f"clip {where}: {what} must be a number of beats like 1.5 or 1/3, got {tok!r}") from None
    return v


def parse_bar(text: str, bar: int) -> list[tuple]:
    """[(beat, token, length, vel, flags)] for one bar string."""
    out = []
    for part in [x.strip() for x in text.split(",")]:
        if not part:
            continue
        toks = part.split()
        where = f"bar {bar + 1} ({part!r})"
        if len(toks) < 3:
            raise PulsarError(f"clip {where}: write each note as `beat note length [velocity]`, e.g. `0 a4 1 .8`")
        beat, tok, length = _num(toks[0], "the start", where), toks[1], _num(toks[2], "the length", where)
        vel, flags = 0.8, set()
        for extra in toks[3:]:
            if extra in FLAGS:
                flags.add(extra)
            else:
                try:
                    vel = float(extra)
                except ValueError:
                    raise PulsarError(f"clip {where}: {extra!r} isn't a velocity or one of {', '.join(FLAGS)}") from None
        if beat < 0 or length <= 0:
            raise PulsarError(f"clip {where}: notes need a start of 0 or more and a positive length")
        out.append((beat, tok, length, vel, frozenset(flags)))
    return out


def clip_pattern(bars: list[str]) -> Pattern:
    parsed = [parse_bar(t, i) for i, t in enumerate(bars)]
    n = max(1, len(bars))
    longest = max((F(b + ln, BEATS) for bar in parsed for b, _, ln, _, _ in bar), default=F(1))

    def q(b, e, c: Ctx):
        evs = []
        for cy in range(math.floor(b - longest), math.ceil(e)):
            for beat, tok, length, vel, flags in parsed[cy % n] if parsed else []:
                v = resolve(tok, c)
                if v is None:
                    continue
                v = dict(v, vel=vel)
                for fl in flags:
                    v[fl] = True
                t0 = cy + beat / BEATS
                evs.append(Ev(t0, t0 + length / BEATS, v))
        return evs

    return Pattern(q, label="clip")


# -- reading a band ----------------------------------------------------------------

def _instance(song, section: str, at: float | None):
    """(index in the arrangement, Section) of the instance of `section` starting at bar `at`
    (or its first instance)."""
    arrangement = song.arrangement or list(song.sections.values())
    pos, first = F(0), None
    for idx, sec in enumerate(arrangement):
        if sec.name == section:
            if at is None or abs(float(pos) - at) < 1e-6:
                return idx, sec
            first = first or (idx, sec)
        pos += sec.bars
    if first:
        return first
    raise PulsarError(f"section {section!r} isn't in the arrangement")


def editable(sec, track: str) -> str | None:
    """Why the band can't be written back, or None."""
    own = [p for p in sec.plays if p.track == track and not p.inherited]
    if any(not p.direct for p in own):
        return f"{track}'s notes in {sec.name} are made by a loop or an if; edit that code instead"
    if any(p.span is None for p in own) or sec.body is None:
        return "this band's source position is unknown"
    return None


def _flat_key(song, sec) -> bool:
    key_pc, scale = sec.key if sec.key else (song.key_pc, song.scale)
    major = key_pc if list(scale)[:3] != [0, 2, 3] else (key_pc + 3) % 12
    return major in (5, 10, 3, 8, 1, 6)


def note_name(midi: float, flats: bool) -> str:
    n = int(round(midi))
    names = (["c", "db", "d", "eb", "e", "f", "gb", "g", "ab", "a", "bb", "b"] if flats else
             ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"])
    return f"{names[n % 12]}{n // 12 - 1}"


def read_band(song, section: str, track: str, at: float | None = None) -> dict:
    """The notes a track plays in one section, as written (before swing, humanize and the track's legato)."""
    from .song import play_ctx
    if track not in song.tracks:
        raise PulsarError(f"unknown track {track!r}")
    tr = song.tracks[track]
    idx, sec = _instance(song, section, at)
    notes = []
    flats = _flat_key(song, sec)
    for pi, play in enumerate(sec.plays):
        if play.track != track:
            continue
        end = sec.bars if play.end is None else min(play.end, sec.bars)
        if end <= play.start:
            continue
        ctx = play_ctx(song, sec, idx, pi, tr)
        for ev in play.pattern.onsets(0, end - play.start, ctx):
            b = play.start + ev.b
            length = (ev.e - ev.b) * F(ev.v.get("legato", 1)).limit_denominator(1000)
            vel = round(float(ev.v.get("vel", 0.8)), 3)
            flags = [f for f in FLAGS if ev.v.get(f)]
            base = {"beat": _fmt(b * BEATS), "len": _fmt(length * BEATS), "vel": vel, "flags": flags}
            if "drum" in ev.v:
                notes.append(dict(base, note=ev.v["drum"], pitch=None))
            elif "slice" in ev.v:
                notes.append(dict(base, note=str(ev.v["slice"]), pitch=None))
            elif ev.v.get("notes"):
                for m in ev.v["notes"]:
                    notes.append(dict(base, note=note_name(m, flats), pitch=float(m)))
            else:
                notes.append(dict(base, note="x", pitch=None))
    kind = {"drums": "drums"}.get(tr.inst.kind, "notes" if tr.inst.kind in ("synth", "piano", "voice") else "hits")
    if kind == "hits" and any(n["pitch"] is not None for n in notes):
        kind = "notes"
    return {"ok": True, "section": sec.name, "track": track, "bars": float(sec.bars), "beats_per_bar": BEATS,
            "kind": kind, "editable": editable(sec, track) is None, "reason": editable(sec, track),
            "inherited": any(p.inherited for p in sec.plays if p.track == track),
            "flats": flats, "notes": sorted(notes, key=lambda n: (float(F(n["beat"])), n["pitch"] or 0))}


def _fmt(x) -> str:
    """A beat position as a short decimal, or a fraction when it doesn't end (triplets)."""
    x = F(x).limit_denominator(1 << 12)
    if x.denominator & (x.denominator - 1) == 0 and x.denominator <= 64 or x.denominator in (5, 10, 20, 25, 50, 100):
        s = f"{float(x):.6f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    return f"{x.numerator}/{x.denominator}"


# -- writing a band ------------------------------------------------------------------

def clip_source(notes: list[dict], bars: int, indent: str) -> str:
    rows = [[] for _ in range(max(1, bars))]
    for n in sorted(notes, key=lambda n: (F(str(n["beat"])), -(n.get("pitch") or 0))):
        beat = F(str(n["beat"]))
        bar = int(beat // BEATS)
        if bar >= len(rows) or beat < 0:
            continue  # outside the section
        tok = str(n["note"])
        if not re.fullmatch(r"[A-Za-z0-9#_]+", tok):
            raise PulsarError(f"can't write {tok!r} as a note in a clip")
        vel = float(n.get("vel", 0.8))
        item = f"{_fmt(beat - bar * BEATS)} {tok} {_fmt(F(str(n['len'])))} {_vel(vel)}"
        for fl in n.get("flags") or []:
            if fl in FLAGS:
                item += f" {fl}"
        rows[bar].append(item)
    lines = [f'{indent}  "{", ".join(r)}",' for r in rows]
    # Mark every 4th bar so long clips stay navigable.
    lines = [ln + (f"  // bar {i + 1}" if i % 4 == 0 and len(rows) > 4 else "") for i, ln in enumerate(lines)]
    return "clip([\n" + "\n".join(lines) + f"\n{indent}])"


def _vel(v: float) -> str:
    s = f"{max(0.0, min(1.0, v)):.2f}".rstrip("0").rstrip(".")
    return s[1:] if s.startswith("0.") else (s or "0")


def _line_bounds(src: str, a: int, b: int) -> tuple[int, int]:
    """Widen [a, b) to whole lines when the statement is alone on its line(s)."""
    ls = src.rfind("\n", 0, a) + 1
    le = src.find("\n", b)
    le = len(src) if le < 0 else le
    before, after = src[ls:a], src[b:le]
    if before.strip() == "" and (after.strip() == "" or after.strip().startswith(("//", "#"))):
        return ls, min(len(src), le + 1)
    return a, b


def write_band(source: str, song, section: str, track: str, notes: list[dict]) -> str:
    """The source with the track's plays in `section` replaced by one clip of `notes`."""
    sec = song.sections.get(section)
    if sec is None:
        raise PulsarError(f"unknown section {section!r}")
    why = editable(sec, track)
    if why:
        raise PulsarError(why)
    own = sorted((p for p in sec.plays if p.track == track and not p.inherited), key=lambda p: p.span[0])
    bars = math.ceil(sec.bars)
    if own:
        a0 = own[0].span[0]
        ls = source.rfind("\n", 0, a0) + 1
        indent = re.match(r"[ \t]*", source[ls:a0]).group(0) if source[ls:a0].strip() == "" else "  "
    else:
        # Nothing of its own yet (all inherited, or new): add a play at the end of the block.
        close = sec.body[1] - 1
        ls = source.rfind("\n", 0, close) + 1
        inner = [ln for ln in source[sec.body[0]:close].splitlines()[1:] if ln.strip()]
        indent = re.match(r"[ \t]*", inner[0]).group(0) if inner else "  "
    inherited = any(p.inherited for p in sec.plays if p.track == track)
    if notes:
        # Leading empty bars become `from N` (as a part that enters late is usually written).
        lead = min(int(F(str(n["beat"])) // BEATS) for n in notes)
        lead = max(0, min(lead, bars - 1))
        shifted = [dict(n, beat=str(F(str(n["beat"])) - lead * BEATS)) for n in notes] if lead else notes
        stmt = f"play {track} {clip_source(shifted, bars - lead, indent)}" + (f" from {lead}" if lead else "")
    else:  # cleared: an empty clip still overrides what the section inherits
        stmt = f'play {track} clip([""])' if inherited else ""
    out = source
    for i, p in enumerate(reversed(own)):
        a, b = p.span
        first = i == len(own) - 1
        if first and stmt:
            out = out[:a] + stmt + out[b:]
        else:
            a, b = _line_bounds(out, a, b)
            out = out[:a] + out[b:]
    if not own and stmt:
        close = sec.body[1] - 1
        ls = out.rfind("\n", 0, close) + 1
        if out[ls:close].strip() == "":
            out = out[:ls] + indent + stmt + "\n" + out[ls:]
        else:  # a one-line section: `section a 4 { ... }`
            out = out[:close].rstrip() + "\n" + indent + stmt + "\n" + out[close:]
    return out
