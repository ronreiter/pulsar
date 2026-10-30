"""The song model the interpreter builds, and its compilation to timed events."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from fractions import Fraction

from . import theory
from .errors import PulsarError
from .pattern import Ctx, Pattern, rand, to_frac


@dataclass
class Effect:
    name: str
    params: dict
    loc: tuple | None = None

    def __repr__(self):
        return f"<effect {self.name}>"


@dataclass
class Instrument:
    kind: str  # "synth" | "drums"
    preset: str
    engine: str  # analog | fm | pluck | drums
    shape: dict  # compile-time: wave1, wave2, unison, filter
    params: dict
    mono: bool = False
    octave: int = 4
    mixer: dict = field(default_factory=dict)
    inserts: list = field(default_factory=list)
    kit: dict = field(default_factory=dict)  # drums: {voice: {param: value}}
    levels: dict = field(default_factory=dict)  # drums: {voice: dB}
    base_gain: float = 0.0  # the preset's balanced fader (dB); the track's `gain` is relative to it
    sample: dict = field(default_factory=dict)  # sampler: path, root, region and playback settings
    loc: tuple | None = None

    def chained(self, eff: Effect) -> "Instrument":
        return replace(self, inserts=self.inserts + [eff])

    def __repr__(self):
        return f"<instrument {self.preset}>"


@dataclass
class Track:
    name: str
    inst: Instrument
    mixer: dict
    loc: tuple | None = None

    def __repr__(self):
        return f"<track {self.name}>"


@dataclass
class Ramp:
    start: float
    end: float
    curve: str = "lin"

    def at(self, x: float) -> float:
        x = min(max(x, 0.0), 1.0)
        if self.curve == "exp" and self.start > 0 and self.end > 0:
            return self.start * (self.end / self.start) ** x
        if self.curve == "smooth":
            x = x * x * (3 - 2 * x)
        elif self.curve == "in":
            x = x * x
        elif self.curve == "out":
            x = 1 - (1 - x) ** 2
        return self.start + (self.end - self.start) * x

    def __repr__(self):
        return f"<ramp {self.start} -> {self.end}>"


@dataclass
class Lfo:
    period: float = 1.0  # bars per cycle
    lo: float = 0.0
    hi: float = 1.0
    shape: str = "sine"
    phase: float = 0.0

    def at(self, bars: float, seed: int = 0) -> float:
        p = (bars / self.period + self.phase) % 1.0
        if self.shape == "sine":
            u = 0.5 - 0.5 * math.cos(2 * math.pi * p)
        elif self.shape == "tri":
            u = 1 - abs(2 * p - 1)
        elif self.shape == "saw":
            u = p
        elif self.shape == "ramp_down":
            u = 1 - p
        elif self.shape == "square":
            u = 1.0 if p < 0.5 else 0.0
        elif self.shape == "random":
            u = rand(seed, int(bars / self.period + self.phase))
        else:
            raise PulsarError(f"unknown lfo shape {self.shape!r}; use sine, tri, saw, ramp_down, square, random")
        return self.lo + (self.hi - self.lo) * u

    def __repr__(self):
        return f"<lfo {self.period} bars>"


@dataclass
class Play:
    track: str
    pattern: Pattern
    start: Fraction
    end: Fraction | None
    loc: tuple | None = None
    inherited: bool = False


@dataclass
class Automation:
    track: str  # track name or "master"
    path: list
    value: object  # number | Ramp | Lfo | Pattern
    start: Fraction
    end: Fraction | None
    loc: tuple | None = None


@dataclass
class Section:
    name: str
    bars: Fraction
    plays: list = field(default_factory=list)
    autos: list = field(default_factory=list)
    key: tuple | None = None
    swing: float | None = None
    loc: tuple | None = None

    def __repr__(self):
        return f"<section {self.name} ({self.bars} bars)>"


@dataclass
class Song:
    tempo: float = 120.0
    key_pc: int = 9
    scale: tuple = tuple(theory.SCALES["minor"])
    key_name: str = "A minor"
    swing: float = 0.0
    seed: int = 1
    loudness: float = -10.0
    ceiling: float = -1.0
    tracks: dict = field(default_factory=dict)
    buses: dict = field(default_factory=dict)
    master: list = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    arrangement: list = field(default_factory=list)

    @property
    def bar_seconds(self) -> float:
        return 240.0 / self.tempo


# -- compiled score ---------------------------------------------------------

@dataclass
class Note:
    time: float  # seconds
    dur: float  # seconds (gate length)
    pitch: float | None  # MIDI, or None for drums
    drum: str | None
    vel: float
    ctrl: dict
    bar: float  # absolute bar position, for automation lookups


@dataclass
class Span:
    """One automation segment in absolute bars."""
    start: float
    end: float
    value: object
    local0: float  # bar where the segment's own time starts (for LFOs / patterns)
    seed: int


@dataclass
class Score:
    song: Song
    notes: dict  # track -> list[Note]
    autos: dict  # (track, tuple(path)) -> list[Span]
    sections: list  # [(name, start_bar, bars)]
    total_bars: float

    @property
    def seconds(self) -> float:
        return self.total_bars * self.song.bar_seconds


def select_sections(song: Song, only) -> None:
    """Keep only the named sections of the arrangement (each once, in song order)."""
    if not only:
        return
    want = [x.strip() for x in (only.split(",") if isinstance(only, str) else only) if x.strip()]
    from .errors import suggest
    for w in want:
        if w not in song.sections:
            raise PulsarError(f"no section named {w!r}", hint=suggest(w, song.sections))
    order = song.arrangement or list(song.sections.values())
    seen: set = set()
    song.arrangement = [s for s in order if s.name in want and not (s.name in seen or seen.add(s.name))]


def build_score(source: str, filename: str = "<song>", only=None):
    """Source text to (song, score, print output)."""
    from .interp import run
    song, output = run(source, filename)
    select_sections(song, only)
    return song, compile_song(song), output


def compile_song(song: Song) -> Score:
    arrangement = song.arrangement or list(song.sections.values())
    if not arrangement:
        raise PulsarError("the song has no sections; add `section name bars { play ... }`")
    bar = song.bar_seconds
    notes: dict = {name: [] for name in song.tracks}
    autos: dict = {}
    markers = []
    pos = Fraction(0)
    for idx, sec in enumerate(arrangement):
        markers.append((sec.name, float(pos), float(sec.bars)))
        key_pc, scale = sec.key if sec.key else (song.key_pc, song.scale)
        swing = song.swing if sec.swing is None else sec.swing
        for pi, play in enumerate(sec.plays):
            track = song.tracks[play.track]
            inst = track.inst
            start = play.start
            end = sec.bars if play.end is None else min(play.end, sec.bars)
            if end <= start:
                continue
            kind = {"drums": "drums", "sampler": "slices" if inst.sample.get("slices") else "sample",
                    "voice": "sample", "ambience": "sample"}.get(inst.kind, "melodic")
            ctx = Ctx(kind=kind, key_pc=key_pc, scale=tuple(scale),
                      octave=int(track.mixer.get("octave", inst.octave)),
                      seed=song.seed * 7919 + idx * 104729 + pi * 31)
            pat = play.pattern
            tsw = track.mixer.get("swing", swing)
            if tsw:
                pat = pat.swing(tsw, Fraction(1, 8))
            hum = track.mixer.get("humanize", 0)
            if hum:
                pat = pat.humanize(to_frac(hum / 1000.0 / bar), 0.06)
            legato = float(track.mixer.get("legato", 1.0))
            try:
                evs = pat.onsets(0, end - start, ctx)
            except RecursionError:
                raise PulsarError("pattern is nested too deeply", play.loc) from None
            except PulsarError as e:
                if e.loc is None:
                    e.loc = play.loc
                raise
            for ev in evs:
                b = float(pos + start + ev.b)
                d = float(ev.e - ev.b) * bar * float(ev.v.get("legato", legato))
                vel = float(ev.v.get("vel", 0.8))
                ctrl = {k: v for k, v in ev.v.items() if k in ("slide", "accent", "slice")}
                if inst.kind == "drums":
                    notes[play.track].append(Note(b * bar, d, None, ev.v["drum"], vel, ctrl, b))
                elif inst.kind in ("sampler", "voice", "ambience"):
                    for n in ev.v.get("notes") or [None]:
                        notes[play.track].append(Note(b * bar, d, None if n is None else float(n), None, vel, ctrl, b))
                else:
                    if "notes" not in ev.v:
                        raise PulsarError(f"track {play.track!r} plays melodic notes but got {ev.v}", play.loc)
                    for n in ev.v["notes"]:
                        notes[play.track].append(Note(b * bar, d, float(n), None, vel, ctrl, b))
        for ai, a in enumerate(sec.autos):
            start = float(a.start)
            end = float(sec.bars if a.end is None else min(a.end, sec.bars))
            autos.setdefault((a.track, tuple(a.path)), []).append(
                Span(float(pos) + start, float(pos) + end, a.value, float(pos) + start, song.seed + idx * 13 + ai))
        pos += sec.bars
    for lst in notes.values():
        lst.sort(key=lambda n: (n.time, n.pitch or 0))
    return Score(song, notes, autos, markers, float(pos))


def span_value(spans: list, bars: float, default: float, ctxkey=None) -> float:
    """Automation value at an absolute bar position. Outside every span the
    last value set persists (like a DAW automation lane); before the first
    span the parameter keeps its static value."""
    current = default
    for s in spans:
        if bars < s.start:
            break
        val = _eval(s, min(bars, s.end))
        if val == val:  # NaN means "no value here" (a rest in a value pattern)
            current = val
    return current


def _eval(s: Span, bars: float) -> float:
    v = s.value
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Ramp):
        length = max(s.end - s.start, 1e-9)
        return v.at((bars - s.start) / length)
    if isinstance(v, Lfo):
        return v.at(bars - s.local0, s.seed)
    if isinstance(v, Pattern):
        local = to_frac(bars - s.local0)
        if bars >= s.end:
            local = to_frac(max(s.end - s.local0 - 1e-6, 0))
        got = v.value_at(local, Ctx(kind="num", seed=s.seed))
        if got is None:
            return float("nan")
        return float(got["num"])
    raise PulsarError(f"can't automate with {v!r}")
