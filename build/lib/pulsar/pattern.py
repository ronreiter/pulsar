"""Patterns: functions from a time span to events, in the spirit of TidalCycles.

Time is measured in cycles (one cycle = one bar) using exact Fractions. A query
returns every event whose span overlaps the queried span; callers that only
care about note-ons filter by onset. Atom values are resolved lazily against a
Ctx (key, octave, what kind of track is playing) so the same pattern string
means the right thing on a bass, a pad, or a drum kit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Callable

from . import theory
from .errors import PulsarError

F = Fraction
EPS = F(1, 1 << 20)


@dataclass(frozen=True)
class Ctx:
    kind: str = "melodic"  # melodic | drums | num | bool
    key_pc: int = 9
    scale: tuple = tuple(theory.SCALES["minor"])
    octave: int = 4
    seed: int = 0


@dataclass
class Ev:
    b: Fraction
    e: Fraction
    v: dict = field(default_factory=dict)

    def shifted(self, t):
        return Ev(self.b + t, self.e + t, self.v)

    def scaled(self, f):
        return Ev(self.b * f, self.e * f, self.v)


def to_frac(x) -> Fraction:
    if isinstance(x, Fraction):
        return x
    if isinstance(x, int):
        return F(x)
    return F(x).limit_denominator(1 << 16)


def cycles(b: Fraction, e: Fraction):
    return range(math.floor(b), math.ceil(e))


def rand(*keys) -> float:
    """Deterministic hash of numbers to [0, 1)."""
    h = 0x9E3779B97F4A7C15
    for k in keys:
        if isinstance(k, Fraction):
            k = k.numerator * 1000003 + k.denominator
        x = (int(k) ^ h) & 0xFFFFFFFFFFFFFFFF
        x = (x + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        h = x ^ (x >> 31)
    return (h >> 11) / float(1 << 53)


class Pattern:
    """A pattern. `q(b, e, ctx)` returns events overlapping [b, e)."""

    def __init__(self, q: Callable, length: Fraction | None = None, label: str = ""):
        self.q = q
        self.label = label

    def query(self, b, e, ctx: Ctx) -> list[Ev]:
        b, e = to_frac(b), to_frac(e)
        if e <= b:
            return []
        return [ev for ev in self.q(b, e, ctx) if ev.e > b and ev.b < e]

    def onsets(self, b, e, ctx: Ctx) -> list[Ev]:
        b, e = to_frac(b), to_frac(e)
        evs = [ev for ev in self.query(b, e, ctx) if b <= ev.b < e]
        evs.sort(key=lambda ev: ev.b)
        return evs

    def value_at(self, t, ctx: Ctx) -> dict | None:
        t = to_frac(t)
        hits = [ev for ev in self.query(t, t + EPS, ctx) if ev.b <= t < ev.e]
        return hits[-1].v if hits else None

    # -- time ---------------------------------------------------------------

    def fast(self, f):
        f = to_frac(f)
        if f <= 0:
            return silence()
        p = self
        return Pattern(lambda b, e, c: [ev.scaled(1 / f) for ev in p.q(b * f, e * f, c)])

    def slow(self, f):
        f = to_frac(f)
        if f <= 0:
            return silence()
        return self.fast(1 / f)

    def late(self, t):
        t = to_frac(t)
        p = self
        return Pattern(lambda b, e, c: [ev.shifted(t) for ev in p.q(b - t, e - t, c)])

    def early(self, t):
        return self.late(-to_frac(t))

    def rev(self):
        p = self

        def q(b, e, c):
            out = []
            for cy in cycles(b, e):
                for ev in p.q(F(cy), F(cy + 1), c):
                    if cy <= ev.b < cy + 1:
                        out.append(Ev(2 * cy + 1 - ev.e, 2 * cy + 1 - ev.b, ev.v))
            return out

        return Pattern(q)

    def ply(self, n):
        n = int(n)
        p = self

        def q(b, e, c):
            out = []
            for ev in p.q(b, e, c):
                d = (ev.e - ev.b) / n
                out += [Ev(ev.b + i * d, ev.b + (i + 1) * d, ev.v) for i in range(n)]
            return out

        return Pattern(q)

    def palindrome(self):
        return slowcat([self, self.rev()])

    def iter(self, n):
        n = int(n)
        return slowcat([self.early(F(i, n)) for i in range(n)])

    def swing(self, amount=0.1, grid=F(1, 8)):
        """Delay events that land on the second half of each `grid` by amount*grid/2."""
        grid = to_frac(grid)
        shift = to_frac(amount) * grid / 2
        p = self

        def q(b, e, c):
            out = []
            for ev in p.q(b - shift, e, c):
                pos = (ev.b / grid) - math.floor(ev.b / grid)
                if pos == F(1, 2):
                    ev = Ev(ev.b + shift, ev.e + shift, ev.v)
                out.append(ev)
            return out

        return Pattern(q)

    def humanize(self, time=0.004, vel=0.08):
        """Seeded random timing (in cycles) and velocity jitter."""
        tj, vj = to_frac(time), float(vel)
        p = self

        def q(b, e, c):
            out = []
            for ev in p.q(b - tj, e + tj, c):
                r1, r2 = rand(c.seed, ev.b, 11), rand(c.seed, ev.b, 23)
                d = tj * to_frac(r1 * 2 - 1)
                v = dict(ev.v)
                v["vel"] = max(0.0, min(1.0, v.get("vel", 0.8) * (1 + vj * (r2 * 2 - 1))))
                out.append(Ev(ev.b + d, ev.e + d, v))
            return out

        return Pattern(q)

    # -- structure ----------------------------------------------------------

    def degrade(self, prob=0.5, salt=0):
        prob = float(prob)
        p = self
        return Pattern(lambda b, e, c: [ev for ev in p.q(b, e, c) if rand(c.seed, salt, ev.b) >= prob])

    def every(self, n, fn, offset=0):
        n, offset = int(n), int(offset)
        p, alt = self, fn(self)

        def q(b, e, c):
            out = []
            for cy in cycles(b, e):
                src = alt if (cy - offset) % n == 0 else p
                lo, hi = max(b, F(cy)), min(e, F(cy + 1))
                out += [ev for ev in src.q(lo, hi, c) if cy <= ev.b < cy + 1 or ev.b < lo]
            return dedupe(out)

        return Pattern(q)

    def lastof(self, n, fn):
        return self.every(n, fn, offset=int(n) - 1)

    def sometimes(self, fn, prob=0.5, salt=7):
        p, alt = self, fn(self)
        prob = float(prob)

        def q(b, e, c):
            out = []
            for cy in cycles(b, e):
                src = alt if rand(c.seed, salt, cy) < prob else p
                lo, hi = max(b, F(cy)), min(e, F(cy + 1))
                out += [ev for ev in src.q(lo, hi, c) if cy <= ev.b < cy + 1 or ev.b < lo]
            return dedupe(out)

        return Pattern(q)

    def off(self, t, fn):
        return stack([self, fn(self.late(t))])

    def echo(self, n=3, time=F(1, 8), feedback=0.6):
        layers = []
        for i in range(int(n)):
            g = float(feedback) ** i
            layers.append(self.late(to_frac(time) * i).fmap(lambda v, g=g: {**v, "vel": v.get("vel", 0.8) * g}))
        return stack(layers)

    def struct(self, boolpat: "Pattern"):
        p = self

        def q(b, e, c):
            out = []
            for s in boolpat.q(b, e, replace(c, kind="bool")):
                if not s.v.get("on"):
                    continue
                for ev in p.query(s.b, s.b + EPS, c):
                    if ev.b <= s.b < ev.e:
                        out.append(Ev(s.b, s.e, ev.v))
            return out

        return Pattern(q)

    def mask(self, boolpat: "Pattern"):
        p = self

        def q(b, e, c):
            bc = replace(c, kind="bool")
            out = []
            for ev in p.q(b, e, c):
                m = boolpat.value_at(ev.b, bc)
                if m and m.get("on"):
                    out.append(ev)
            return out

        return Pattern(q)

    # -- values -------------------------------------------------------------

    def fmap(self, fn):
        p = self
        return Pattern(lambda b, e, c: [Ev(ev.b, ev.e, fn(ev.v)) for ev in p.q(b, e, c)])

    def with_ctx(self, fn):
        p = self
        return Pattern(lambda b, e, c: p.q(b, e, fn(c)))

    def ctrl(self, name, other: "Pattern", kind="num"):
        """Sample `other` at each event onset and store it under `name`."""
        p = self

        def q(b, e, c):
            oc = replace(c, kind=kind)
            out = []
            for ev in p.q(b, e, c):
                ov = other.value_at(ev.b, oc)
                if ov is None:
                    out.append(ev)
                    continue
                val = ov.get("num") if kind == "num" else bool(ov.get("on"))
                out.append(Ev(ev.b, ev.e, {**ev.v, name: val}))
            return out

        return Pattern(q)

    def transpose(self, semis):
        s = int(round(float(semis)))
        return self.fmap(lambda v: {**v, "notes": [n + s for n in v["notes"]]} if "notes" in v else v)

    def legato(self, x):
        x = to_frac(x)
        p = self
        return Pattern(lambda b, e, c: [Ev(ev.b, ev.b + (ev.e - ev.b) * x, ev.v) for ev in p.q(b, e, c)])

    def arp(self, mode="up", rate=None, octaves=1):
        p = self

        def order(notes, b, seed):
            notes = sorted(notes)
            notes = [n + 12 * o for o in range(int(octaves)) for n in notes]
            if mode == "up":
                return notes
            if mode == "down":
                return notes[::-1]
            if mode == "updown":
                return notes + notes[-2:0:-1]
            if mode == "downup":
                r = notes[::-1]
                return r + r[-2:0:-1]
            if mode == "converge":
                out, lo, hi = [], 0, len(notes) - 1
                while lo <= hi:
                    out.append(notes[lo])
                    if lo != hi:
                        out.append(notes[hi])
                    lo, hi = lo + 1, hi - 1
                return out
            if mode == "random":
                return sorted(notes, key=lambda n: rand(seed, b, n))
            raise PulsarError(f"unknown arp mode {mode!r}; use up, down, updown, downup, converge, random")

        def q(b, e, c):
            out = []
            for ev in p.q(b, e, c):
                notes = ev.v.get("notes")
                if not notes:
                    out.append(ev)
                    continue
                seq = order(notes, ev.b, c.seed)
                if rate is None:
                    step = (ev.e - ev.b) / len(seq)
                else:
                    step = to_frac(rate)
                t, i = ev.b, 0
                while t < ev.e - EPS:
                    end = min(t + step, ev.e)
                    out.append(Ev(t, end, {**ev.v, "notes": [seq[i % len(seq)]]}))
                    t, i = t + step, i + 1
            return out

        return Pattern(q)

    def chop_chords(self):
        """Split chords into separate simultaneous events (one per note)."""
        p = self
        return Pattern(lambda b, e, c: [Ev(ev.b, ev.e, {**ev.v, "notes": [n]})
                                        for ev in p.q(b, e, c) for n in ev.v.get("notes", [])])

    def __repr__(self):
        return f"<pattern {self.label}>" if self.label else "<pattern>"


def dedupe(evs):
    seen, out = set(), []
    for ev in evs:
        k = (ev.b, ev.e, id(ev.v))
        if k not in seen:
            seen.add(k)
            out.append(ev)
    return out


def silence() -> Pattern:
    return Pattern(lambda b, e, c: [])


def pure(value: dict) -> Pattern:
    return Pattern(lambda b, e, c: [Ev(F(cy), F(cy + 1), value) for cy in cycles(b, e)])


def atom(tok: str, loc=None) -> Pattern:
    def q(b, e, c):
        v = resolve(tok, c, loc)
        if v is None:
            return []
        return [Ev(F(cy), F(cy + 1), v) for cy in cycles(b, e)]

    return Pattern(q, label=tok)


def stack(pats) -> Pattern:
    pats = list(pats)
    return Pattern(lambda b, e, c: [ev for p in pats for ev in p.q(b, e, c)])


def compress(cb, ce, p: Pattern) -> Pattern:
    """Squeeze each cycle of `p` into [cb, ce) of the same cycle."""
    cb, ce = to_frac(cb), to_frac(ce)
    w = ce - cb
    if w <= 0:
        return silence()

    def q(b, e, c):
        out = []
        for cy in cycles(b, e):
            lo, hi = max(b, cy + cb), min(e, cy + ce)
            if hi <= lo:
                continue
            ilo, ihi = cy + (lo - cy - cb) / w, cy + (hi - cy - cb) / w
            for ev in p.q(ilo, ihi, c):
                if cy <= ev.b < cy + 1:
                    out.append(Ev(cy + cb + (ev.b - cy) * w, cy + cb + (ev.e - cy) * w, ev.v))
        return out

    return Pattern(q)


def timecat(pairs) -> Pattern:
    pairs = [(to_frac(w), p) for w, p in pairs]
    total = sum(w for w, _ in pairs)
    if not pairs or total == 0:
        return silence()
    if len(pairs) == 1:
        return pairs[0][1]
    out, s = [], F(0)
    for w, p in pairs:
        out.append(compress(s / total, (s + w) / total, p))
        s += w
    return stack(out)


def fastcat(pats) -> Pattern:
    return timecat([(1, p) for p in pats])


def slowcat(pats) -> Pattern:
    """One pattern per cycle, each keeping its own cycle count."""
    pats = list(pats)
    n = len(pats)
    if n == 0:
        return silence()
    if n == 1:
        return pats[0]

    def q(b, e, c):
        out = []
        for cy in cycles(b, e):
            p = pats[cy % n]
            off = cy - cy // n
            lo, hi = max(b, F(cy)), min(e, F(cy + 1))
            for ev in p.q(lo - off, hi - off, c):
                if cy - off <= ev.b < cy + 1 - off:
                    out.append(ev.shifted(off))
        return out

    return Pattern(q)


def cat(pats) -> Pattern:
    """Sequence patterns of different lengths (in cycles) end to end, looping."""
    return slowcat(pats)


def randcat(pats, salt=0) -> Pattern:
    pats = list(pats)

    def q(b, e, c):
        out = []
        for cy in cycles(b, e):
            p = pats[int(rand(c.seed, salt, cy) * len(pats))]
            lo, hi = max(b, F(cy)), min(e, F(cy + 1))
            out += [ev for ev in p.q(lo, hi, c) if cy <= ev.b < cy + 1]
        return out

    return Pattern(q)


def bjorklund(k: int, n: int, rot: int = 0) -> list[bool]:
    if n <= 0:
        return []
    k = max(0, min(k, n))
    # Bresenham form gives the same rhythms as Bjorklund up to rotation; anchor
    # the first hit on step 0 like Tidal does.
    bits = [((i * k) // n) != (((i - 1) * k) // n) if i else k > 0 for i in range(n)]
    rot %= n
    return bits[rot:] + bits[:rot]


def euclid(k, n, rot, p: Pattern) -> Pattern:
    bits = bjorklund(int(k), int(n), int(rot))
    return fastcat([p if bit else silence() for bit in bits])


# -- atom resolution ----------------------------------------------------------

DRUM_ALIASES = {
    "kick": "bd", "snare": "sd", "hat": "hh", "hihat": "hh", "openhat": "oh",
    "clap": "cp", "rimshot": "rim", "rs": "rim", "crash": "cr", "ride": "rd",
    "cowbell": "cb", "shaker": "sh", "tom": "mt", "perc": "pc", "cl": "cp",
}


def resolve(tok: str, c: Ctx, loc=None) -> dict | None:
    if tok == "~":
        return None
    kind = c.kind
    if kind == "bool":
        return {"on": tok.lower() not in ("0", "f", "false", "-", "no")}
    if kind == "num":
        try:
            return {"num": float(tok)}
        except ValueError:
            if tok.lower() in ("x", "t", "true"):
                return {"num": 1.0}
            raise PulsarError(f"expected a number in this pattern, got {tok!r}", loc)
    if kind in ("sample", "slices"):
        # Sampler steps: integers pick slices when the sampler is sliced;
        # otherwise anything melodic (notes, degrees, chords, numerals) re-pitches
        # the sample, and any other word plays it as recorded.
        if kind == "slices":
            try:
                return {"slice": int(tok)}
            except ValueError:
                pass
        try:
            return resolve(tok, replace(c, kind="melodic"), loc)
        except PulsarError:
            return {"hit": True}
    if kind == "drums":
        if not tok.replace("_", "").isalnum() or tok[0].isdigit():
            raise PulsarError(f"expected a drum name like bd, sd, hh, got {tok!r}", loc)
        return {"drum": DRUM_ALIASES.get(tok, tok)}
    # melodic. A capitalised root followed by a chord quality is a chord, so
    # "E6", "A9", "G7" are chords; write notes in lowercase ("e6", "a9").
    if tok[0].isupper():
        ch = theory.chord_symbol(tok, c.octave)
        if ch is not None:
            return {"notes": ch}
    n = theory.note_number(tok)
    if n is not None:
        return {"notes": [n]}
    if len(tok) <= 3 and tok[0].islower():
        pc = theory.pitch_class(tok)
        if pc is not None:
            return {"notes": [12 * (c.octave + 1) + pc]}
    ch = theory.roman_chord(tok, c.key_pc, list(c.scale), c.octave)
    if ch is not None:
        return {"notes": ch}
    ch = theory.chord_symbol(tok, c.octave)
    if ch is not None:
        return {"notes": ch}
    try:
        d = int(tok)
        return {"notes": [theory.degree_to_midi(d, c.key_pc, list(c.scale), c.octave)]}
    except ValueError:
        pass
    raise PulsarError(
        f"can't read {tok!r} as a note: use a note name (a3, eb2), a scale degree (0 2 4), "
        f"a chord (Am7, Cmaj9) or a roman numeral (i iv VI v)", loc)


# -- mini-notation ------------------------------------------------------------

class _Mini:
    """Recursive-descent parser for mini-notation strings like "bd [~ sd]*2 <hh oh>"."""

    PUNCT = set("[]<>(),|*/!@?~_")

    def __init__(self, text: str, loc=None):
        self.text = text
        self.loc = loc
        self.toks = self._lex(text)
        self.i = 0

    def err(self, msg, off):
        loc = None
        if self.loc:
            loc = (self.loc[0], self.loc[1] + 1 + off)
        raise PulsarError(f"in pattern \"{self.text}\": {msg}", loc)

    def _lex(self, s):
        out, i = [], 0
        while i < len(s):
            ch = s[i]
            if ch.isspace() or (ch == "." and (i + 1 == len(s) or s[i + 1].isspace())):
                i += 1
                continue
            if ch in self.PUNCT:
                out.append((ch, i))
                i += 1
                continue
            j = i
            while j < len(s) and not s[j].isspace() and s[j] not in self.PUNCT:
                j += 1
            if j == i:
                self.err(f"unexpected character {ch!r}", i)
            out.append((s[i:j], i))
            i = j
        return out

    def peek(self):
        return self.toks[self.i][0] if self.i < len(self.toks) else None

    def off(self):
        return self.toks[self.i][1] if self.i < len(self.toks) else len(self.text)

    def take(self, expect=None):
        if self.i >= len(self.toks):
            self.err(f"expected {expect!r} but the pattern ended" if expect else "unexpected end", len(self.text))
        t = self.toks[self.i]
        if expect and t[0] != expect:
            self.err(f"expected {expect!r}, got {t[0]!r}", t[1])
        self.i += 1
        return t

    def parse(self) -> Pattern:
        if not self.toks:
            return silence()
        p = self.stack(None)
        if self.peek() is not None:
            self.err(f"unexpected {self.peek()!r}", self.off())
        return p

    def stack(self, close):
        parts = [self.choice(close)]
        while self.peek() == ",":
            self.take()
            parts.append(self.choice(close))
        return parts[0] if len(parts) == 1 else stack(parts)

    def choice(self, close):
        opts = [self.seq(close)]
        start = self.off()
        while self.peek() == "|":
            self.take()
            opts.append(self.seq(close))
        return opts[0] if len(opts) == 1 else randcat(opts, salt=start)

    def seq_items(self, close):
        items = []  # [weight, pattern]
        while True:
            t = self.peek()
            if t is None or t in (",", "|", "]", ">", ")"):
                break
            if t == "_":
                o = self.off()
                self.take()
                if not items:
                    self.err("'_' must follow a step to extend it", o)
                items[-1][0] += 1
                continue
            if t == "!":
                o = self.off()
                self.take()
                if not items:
                    self.err("'!' must follow a step to repeat it", o)
                items.append(list(items[-1]))
                continue
            items.append(self.term())
        if close and self.peek() != close:
            if self.peek() is None:
                self.err(f"missing closing {close!r}", len(self.text))
        return items

    def seq(self, close):
        items = self.seq_items(close)
        if not items:
            return silence()
        return timecat([(w, p) for w, p in items])

    def number(self):
        tok, o = self.take()
        try:
            return to_frac(float(tok)) if "." in tok else F(int(tok))
        except ValueError:
            self.err(f"expected a number, got {tok!r}", o)

    def term(self):
        tok, o = self.take()
        if tok == "[":
            p = self.stack("]")
            self.take("]")
        elif tok == "<":
            seqs = [self.seq_items(">")]
            while self.peek() == ",":
                self.take()
                seqs.append(self.seq_items(">"))
            self.take(">")
            alts = [slowcat([p for w, p in items for _ in range(1)]) for items in seqs]
            p = alts[0] if len(alts) == 1 else stack(alts)
        elif tok == "~":
            p = silence()
        elif tok in self.PUNCT:
            self.err(f"unexpected {tok!r}", o)
        else:
            loc = (self.loc[0], self.loc[1] + 1 + o) if self.loc else None
            p = atom(tok, loc)
        weight = F(1)
        reps = 1
        while True:
            t = self.peek()
            if t == "*":
                self.take()
                p = p.fast(self.number())
            elif t == "/":
                self.take()
                p = p.slow(self.number())
            elif t == "?":
                self.take()
                prob = 0.5
                nt = self.peek()
                if nt and nt[0].isdigit():
                    prob = float(self.number())
                p = p.degrade(prob, salt=o + 1)
            elif t == "@":
                self.take()
                weight = self.number()
            elif t == "!":
                # "a!3" replicates; a bare "!" after whitespace is handled in seq_items
                nxt = self.toks[self.i + 1][0] if self.i + 1 < len(self.toks) else None
                if nxt and nxt.isdigit() and self.toks[self.i + 1][1] == self.off() + 1:
                    self.take()
                    reps = int(self.number())
                else:
                    break
            elif t == "(":
                self.take()
                k = self.number()
                self.take(",")
                n = self.number()
                rot = 0
                if self.peek() == ",":
                    self.take()
                    rot = self.number()
                self.take(")")
                p = euclid(k, n, rot, p)
            else:
                break
        if reps > 1:
            # Replication expands into several equal steps; represent as one
            # weighted step holding a fastcat so weights stay consistent.
            return [weight * reps, fastcat([p] * reps)]
        return [weight, p]


_MINI_CACHE: dict = {}


def mini(text: str, loc=None) -> Pattern:
    key = (text, loc)
    if key not in _MINI_CACHE:
        p = _Mini(text, loc).parse()
        p.label = text
        _MINI_CACHE[key] = p
    return _MINI_CACHE[key]
