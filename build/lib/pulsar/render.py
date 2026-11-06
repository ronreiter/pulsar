"""Render a compiled Score to audio.

Voices are rendered note by note at INTERNAL_SR (2x the output rate, which
keeps oscillators, filters and saturation clean), summed per track,
downsampled, then run through insert effects, the mixer (fader, pan,
sidechain duck, sends), the send buses and the master chain.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import zlib
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from . import theory
from .dsp import effects as FX
from .dsp import faust as FZ
from .dsp.engine import Engine
from .dsp.master import downsample, master as master_bus
from .errors import PulsarError, suggest
from .song import Instrument, Note, Score, Track, span_value

OUT_SR = 48000
OS = 2
INTERNAL_SR = OUT_SR * OS
REF_MOMENTARY_DB = -18.0  # every synth preset is calibrated to this
DRUM_PEAK_DB = -3.0  # every drum voice peaks here before its kit level
CONTROL_STEP = 1 / 256  # automation grid, in bars
CALIB_VERSION = "2"  # bump when the calibration method changes

_CALIB_PATH = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar")) / "calibration.json"


@dataclass
class RenderResult:
    mix: np.ndarray
    sr: int
    stems: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)


# Extra players in an ensemble: (detune cents, delay seconds, pan). Player 1 is the recording as is.
ENSEMBLE = [(0, 0.0, 0.0), (7, 0.011, -0.45), (-6, 0.019, 0.45), (3, 0.006, -0.2), (-9, 0.025, 0.2), (5, 0.015, 0.0)]
# Low thump under the big drums: (start Hz, end Hz, decay seconds)
THUMP = {"taiko": (115, 50, 0.32), "taikos": (125, 56, 0.26), "taikoh": (120, 55, 0.28), "bd": (95, 40, 0.45),
         "bdm": (100, 45, 0.25), "tenor": (140, 70, 0.22), "timp": (90, 46, 0.5), "timph": (110, 58, 0.45),
         "gong": (70, 36, 0.9)}
_THUMPS: dict = {}


def thump_wave(spec) -> np.ndarray:
    if spec not in _THUMPS:
        f0, f1, dec = spec
        t = np.arange(int((dec * 5) * OUT_SR)) / OUT_SR
        f = f1 + (f0 - f1) * np.exp(-t / 0.04)
        ph = 2 * np.pi * np.cumsum(f) / OUT_SR
        env = np.exp(-t / dec) * (1 - np.exp(-t / 0.0015))
        body = np.sin(ph) * env
        # ...and a short bright crack on top, so the hit cuts through a full orchestra
        rng = np.random.default_rng(int(f0 * 1000 + f1))
        noise = np.diff(rng.standard_normal(len(t) + 1))  # tilted towards the highs
        crack = noise * np.exp(-t / 0.007) * 0.12
        _THUMPS[spec] = (body + crack).astype(np.float32)
    return _THUMPS[spec]


class Renderer:
    def __init__(self, score: Score, log=None, rebuild=None):
        self.score = score
        self.rebuild = rebuild  # (source, filename, only): lets worker processes rebuild the score
        self.song = score.song
        self.bar = self.song.bar_seconds
        self.voice = Engine(INTERNAL_SR)
        self.fx = Engine(OUT_SR)
        self.log = log or (lambda *a: None)
        self._calib = _load_calib()

    # -- helpers ---------------------------------------------------------------

    def secs(self, value) -> float:
        """A note value (fraction of a bar) in seconds."""
        return float(value) * self.bar

    def length_samples(self, sr) -> int:
        return int((self.score.seconds + 6.0) * sr)

    def control_curve(self, spans, default, n_samples, sr) -> np.ndarray:
        """Automation as a per-sample array, evaluated on a 1/256-bar grid and interpolated."""
        total_bars = n_samples / sr / self.bar
        grid = np.arange(0, total_bars + CONTROL_STEP, CONTROL_STEP)
        vals = np.array([span_value(spans, float(b), default) for b in grid], dtype=np.float64)
        t = np.arange(n_samples) / sr / self.bar
        return np.interp(t, grid, vals).astype(np.float32)

    def autos_for(self, track: str, path) -> list:
        return self.score.autos.get((track, tuple(path)), [])

    # -- calibration -------------------------------------------------------------

    def calibrate(self, code: str, params: dict, ref_midi: int) -> float:
        key = hashlib.sha1((CALIB_VERSION + code + json.dumps(sorted((k, round(float(v), 4)) for k, v in params.items()
                                                       if k != "level"))).encode()).hexdigest()
        if key in self._calib:
            return self._calib[key]
        p = dict(params, level=1.0, freq=theory.midi_to_hz(ref_midi), vel=0.8, dur=1.2, seed=1)
        a = self.voice.render(code, p, 2.2, trim=False)
        win = int(0.2 * INTERNAL_SR)
        sq = (a.astype(np.float64) ** 2).mean(axis=0)
        c = np.concatenate([[0], np.cumsum(sq)])
        mom = (c[win:] - c[:-win]) / win if len(sq) > win else np.array([sq.mean()])
        rms = math.sqrt(max(float(mom.max()), 1e-12))
        gain = 10 ** (REF_MOMENTARY_DB / 20) / rms if rms > 1e-7 else 1.0
        self._calib[key] = gain
        _save_calib(self._calib)
        return gain

    # -- synth tracks ----------------------------------------------------------------

    def synth_code(self, inst: Instrument) -> str:
        if inst.engine == "analog":
            sh = inst.shape
            return FZ.analog_voice(sh.get("wave1", "saw"), sh.get("wave2", "saw"), int(sh.get("unison", 1)),
                                   sh.get("filter", "ladder"))
        if inst.engine == "fm":
            return FZ.fm_voice()
        if inst.engine == "pluck":
            return FZ.pluck_voice()
        raise PulsarError(f"unknown engine {inst.engine}")

    def synth_params(self, inst: Instrument) -> dict:
        p = {}
        for k, v in inst.params.items():
            if k in ("vib_sync", "flfo_sync", "trem_sync"):
                rate = 1.0 / max(self.secs(v), 1e-4)
                p[{"vib_sync": "vib_rate", "flfo_sync": "flfo_rate", "trem_sync": "trem_rate"}[k]] = rate
            else:
                p[k] = v
        return p

    def render_synth(self, tr: Track, notes: list[Note], n_out: int, part=(0, 1)) -> np.ndarray:
        inst = tr.inst
        code = self.synth_code(inst)
        base = self.synth_params(inst)
        level = float(base.get("level", 1.0)) * self.calibrate(code, base, 12 * (inst.octave + 1) + 9)
        n_int = n_out * OS
        buf = np.zeros((2, n_int), np.float32)
        release = float(base.get("release", 0.3))
        if inst.engine == "pluck":
            release = max(release, float(base.get("decay", 3.0)))
        tail = min(release * 1.2 + 0.05, 12.0)
        synth_autos = {k[1][1]: v for k, v in self.score.autos.items() if k[0] == tr.name and k[1][0] == "synth"}
        phrases = self.phrases(inst, notes)
        for pi, phrase in enumerate(phrases):
            if pi % part[1] != part[0]:
                continue
            first = phrase[0]
            end = max(n.time + n.dur for n in phrase)
            dur = max(end - first.time, 0.005)
            p = dict(base)
            p.update(level=level, vel=first.vel, dur=dur, seed=(pi * 7.31) % 997,
                     accent=1.0 if first.ctrl.get("accent") else base.get("accent", 0.0))
            p["freq"] = theory.midi_to_hz(first.pitch)
            if getattr(first, "from_pitch", None) is not None:
                p["from_freq"] = theory.midi_to_hz(first.from_pitch)
            seconds = dur + tail
            auto = {}
            for name, spans in synth_autos.items():
                b0, b1 = first.bar, first.bar + seconds / self.bar
                v0 = span_value(spans, b0, float(base.get(name, FZ.ANALOG_PARAMS.get(name, (0,))[0])))
                v1 = span_value(spans, b1, v0)
                vm = span_value(spans, (b0 + b1) / 2, v0)
                if max(abs(v1 - v0), abs(vm - v0)) < 1e-4:
                    p[name] = v0
                else:
                    n = int(seconds * INTERNAL_SR)
                    grid = np.linspace(b0, b1, max(2, int((b1 - b0) / CONTROL_STEP) + 2))
                    vals = [span_value(spans, float(g), v0) for g in grid]
                    auto[name] = np.interp(np.linspace(b0, b1, n), grid, vals).astype(np.float32)
                    p[name] = v0
            if len(phrase) > 1:
                auto["freq"] = self.glide_curve(phrase, seconds, float(base.get("glide", 0.06)) or 0.06)
            a = self.voice.render(code, p, seconds, automation=auto or None)
            s = int(round(first.time * INTERNAL_SR))
            e = min(n_int, s + a.shape[1])
            if e > s:
                buf[:, s:e] += a[:, :e - s]
        return downsample(buf, OS)

    def phrases(self, inst: Instrument, notes: list[Note]) -> list[list[Note]]:
        """Group notes into render units. Poly: one note each. Mono: notes
        are cut at the next note, and a note marked .slide() glides from the
        previous one without retriggering (one render for the whole phrase)."""
        if not inst.mono:
            return [[n] for n in notes]
        out: list[list[Note]] = []
        notes = sorted(notes, key=lambda n: n.time)
        glide = float(inst.params.get("glide", 0.0))
        for i, n in enumerate(notes):
            nxt = notes[i + 1] if i + 1 < len(notes) else None
            if nxt is not None and not nxt.ctrl.get("slide"):
                n.dur = max(0.003, min(n.dur, nxt.time - n.time - 0.0005))
            if out and n.ctrl.get("slide"):
                prev = out[-1][-1]
                prev.dur = n.time - prev.time
                out[-1].append(n)
                continue
            if out and glide > 0:
                prev = out[-1][-1]
                if n.time - (prev.time + prev.dur) < 0.01:
                    n.from_pitch = prev.pitch
            out.append([n])
        return out

    def glide_curve(self, phrase, seconds, glide) -> np.ndarray:
        n = int(seconds * INTERNAL_SR)
        t0 = phrase[0].time
        f = np.full(n, theory.midi_to_hz(phrase[0].pitch), np.float64)
        for a, b in zip(phrase, phrase[1:]):
            s = int((b.time - t0) * INTERNAL_SR)
            if s >= n:
                break
            fa, fb = theory.midi_to_hz(a.pitch), theory.midi_to_hz(b.pitch)
            tt = np.arange(n - s) / INTERNAL_SR
            k = 1 - np.exp(-tt / max(glide / 4, 1e-4))
            f[s:] = fa * (fb / fa) ** k
        return f.astype(np.float32)

    # -- drums ---------------------------------------------------------------------

    def render_drums(self, tr: Track, notes: list[Note], n_out: int, part=(0, 1)) -> np.ndarray:
        inst = tr.inst
        n_int = n_out * OS
        buf = np.zeros((2, n_int), np.float32)
        norms = {}
        hats = sorted([n.time for n in notes if n.drum in ("hh", "oh")])
        for n in notes:
            if n.drum not in FZ.DRUM_PARAMS:
                raise PulsarError(f"track {tr.name!r}: unknown drum {n.drum!r}",
                                  hint=suggest(n.drum, FZ.DRUM_NAMES) or f"drums: {', '.join(FZ.DRUM_NAMES)}")
        for i, n in enumerate(notes):
            if i % part[1] != part[0]:
                continue
            name = n.drum
            code = FZ.drum_voice(name)
            params = dict(inst.kit.get(name, {}))
            if name not in norms:
                ref = self.voice.render(code, dict(params, vel=1.0, dur=10, seed=0, level=1.0), 4.0, cache=True)
                peak = float(np.abs(ref).max()) or 1.0
                norms[name] = 10 ** ((DRUM_PEAK_DB + inst.levels.get(name, 0)) / 20) / peak
            choke = 10.0
            if name == "oh":
                later = [t for t in hats if t > n.time + 1e-4]
                if later:
                    choke = later[0] - n.time
            decay = params.get("decay", FZ.DRUM_PARAMS[name].get("decay", (0.5,))[0])
            seconds = min(choke, float(decay) * 1.6 + 0.08) + 0.01
            vel = round(n.vel * 50) / 50
            p = dict(params, vel=vel, dur=round(choke, 3), seed=0, level=norms[name])
            a = self.voice.render(code, p, seconds, cache=True)
            s = int(round(n.time * INTERNAL_SR))
            e = min(n_int, s + a.shape[1])
            if e > s:
                buf[:, s:e] += a[:, :e - s]
        return downsample(buf, OS)

    # -- sampler ---------------------------------------------------------------------

    def load_sample(self, path: str) -> np.ndarray:
        """A sample file as float32 stereo at the internal rate (cached)."""
        cache = self.__dict__.setdefault("_samples", {})
        if path not in cache:
            import soundfile as sf
            from math import gcd
            from scipy.signal import resample_poly
            try:
                x, sr = sf.read(path, always_2d=True, dtype="float32")
            except RuntimeError as e:
                raise PulsarError(f"can't read sample {path}: {e}") from None
            x = x.T
            if x.shape[0] == 1:
                x = np.repeat(x, 2, axis=0)
            x = x[:2]
            if sr != INTERNAL_SR:
                g = gcd(INTERNAL_SR, sr)
                x = resample_poly(x, INTERNAL_SR // g, sr // g, axis=1).astype(np.float32)
            cache[path] = x
        return cache[path]

    def render_sampler(self, tr: Track, notes: list[Note], n_out: int, part=(0, 1)) -> np.ndarray:
        from fractions import Fraction as Fr
        from scipy.signal import resample_poly
        opt = tr.inst.sample
        if opt.get("ref"):
            from . import library
            src = self.load_sample(str(library.fetch(opt["ref"], self.log)[0]))
        else:
            src = self.load_sample(opt["path"])
        total = src.shape[1]
        a0, a1 = int(opt["start"] * total), int(opt["end"] * total)
        region = src[:, a0:a1]
        # Calibrate like the synths: loudest 200 ms of the region to the reference level.
        win = max(1, int(0.2 * INTERNAL_SR))
        sq = (region.astype(np.float64) ** 2).mean(axis=0)
        c = np.concatenate([[0], np.cumsum(sq)])
        mom = (c[win:] - c[:-win]) / win if len(sq) > win else np.array([sq.mean() if len(sq) else 0])
        rms = math.sqrt(max(float(mom.max()), 1e-12))
        level = 10 ** (REF_MOMENTARY_DB / 20) / rms if rms > 1e-7 else 1.0
        slices = opt["slices"]
        n_int = n_out * OS
        buf = np.zeros((2, n_int), np.float32)
        shifted = {}
        for i, nt in enumerate(notes):
            if i % part[1] != part[0]:
                continue
            seg = region
            if slices > 0:
                k = int(nt.ctrl.get("slice", 0)) % slices
                L = region.shape[1] // slices
                seg = region[:, k * L:(k + 1) * L]
            if opt["reverse"]:
                seg = seg[:, ::-1]
            if seg.shape[1] < 2:
                continue
            ratio = opt["speed"] * 2 ** (opt["transpose"] / 12)
            if nt.pitch is not None:
                ratio *= 2 ** ((nt.pitch - opt["root"]) / 12)
            if opt["fit"]:
                ratio = seg.shape[1] / max(nt.dur * INTERNAL_SR, 1)
            key = (id(seg) if slices == 0 else nt.ctrl.get("slice", 0), round(ratio, 6))
            out = shifted.get(key)
            if out is None:
                if abs(ratio - 1) < 1e-6:
                    out = seg
                else:
                    fr = Fr(1 / ratio).limit_denominator(480)
                    out = resample_poly(seg, fr.numerator, fr.denominator, axis=1).astype(np.float32)
                shifted[key] = out
            out = out.copy()
            att = int(opt["attack"] * INTERNAL_SR)
            if att > 1:
                out[:, :att] *= np.linspace(0, 1, min(att, out.shape[1]), dtype=np.float32)[: out.shape[1]]
            if opt["gate"]:
                stop = int(nt.dur * INTERNAL_SR)
                rel = max(2, int(opt["release"] * INTERNAL_SR))
                if stop < out.shape[1]:
                    end = min(out.shape[1], stop + rel)
                    out = out[:, :end]
                    out[:, stop:end] *= np.linspace(1, 0, end - stop, dtype=np.float32)
            tail = min(out.shape[1], int(0.002 * INTERNAL_SR))
            if tail > 1:
                out[:, -tail:] *= np.linspace(1, 0, tail, dtype=np.float32)
            s = int(round(nt.time * INTERNAL_SR))
            e = min(n_int, s + out.shape[1])
            if e > s:
                buf[:, s:e] += out[:, :e - s] * (level * nt.vel / 0.8)
        return downsample(buf, OS)

    # -- sampled piano -----------------------------------------------------------------

    def piano_set(self, tr: Track):
        """(regions, level) for a sampled instrument: its regions, and the gain that puts a
        mezzo-forte note in the middle of its range at the reference level (so dynamics between
        notes stay as recorded). An orchestral percussion kit gives {voice: (regions, level)}."""
        from . import orchestra, piano
        opt = tr.inst.sample
        key = (tr.inst.kind, opt["name"], opt.get("release"))
        cache = self.__dict__.setdefault("_pianos", {})
        if key in cache:
            return cache[key]
        if tr.inst.kind == "piano":
            regions = piano.parse_sfz(piano.install(opt["name"], self.log))
            cache[key] = (regions, self._sample_level(piano.find(regions, 60, 96)))
        elif opt.get("kit"):
            kit = orchestra.kit_regions(orchestra.install("percussion", self.log))
            # each voice calibrated on its loudest layer, so a fff hit sits at the reference level
            cache[key] = {v: (rs, self._sample_level(max(rs, key=lambda r: r.hivel), loud=True)) for v, rs in kit.items()}
        else:
            regions = orchestra.regions(opt["name"], orchestra.install(opt["name"], self.log), opt.get("release"))
            if not regions:
                raise PulsarError(f"no samples found for orchestra({opt['name']!r})")
            centers = sorted({r.center for r in regions})
            cache[key] = (regions, self._sample_level(piano.find(regions, centers[len(centers) // 2], 96)))
        return cache[key]

    def _sample_level(self, ref, loud=False) -> float:
        x = self.piano_wave(ref.path)
        win = int(0.2 * OUT_SR)
        sq = (x[:, : 4 * OUT_SR].astype(np.float64) ** 2).mean(axis=0)
        c = np.concatenate([[0], np.cumsum(sq)])
        mom = (c[win:] - c[:-win]) / win if len(sq) > win else np.array([sq.mean()])
        rms = math.sqrt(max(float(mom.max()), 1e-12))
        v = ref.hivel if loud else 96
        amp = 1 + ref.veltrack * ((v / 127) ** 2 - 1)
        return 10 ** (REF_MOMENTARY_DB / 20) / (rms * amp * 10 ** (ref.volume / 20))

    def piano_wave(self, path) -> np.ndarray:
        cache = self.__dict__.setdefault("_piano_waves", {})
        if path not in cache:
            import soundfile as sf

            from .orchestra import HOME
            x, sr = sf.read(str(path), always_2d=True, dtype="float32")
            x = x.T
            if x.shape[0] == 1:
                x = np.repeat(x, 2, axis=0)
            x = x[:2]
            if str(path).startswith(str(HOME)):
                # Some orchestral recordings start up to 100 ms before the sound does, which makes
                # their notes land late: start each one just before its onset (-50 dB from its peak).
                lvl = np.abs(x).max(axis=0)
                on = int(np.argmax(lvl > lvl.max() * 10 ** (-50 / 20)))
                start = max(0, on - int(0.004 * sr))
                if start > 0:
                    x = x[:, start:].copy()
                    f = min(int(0.002 * sr), x.shape[1])
                    x[:, :f] *= np.linspace(0, 1, f, dtype=np.float32)
            cache[path] = (x, sr)
        return cache[path][0]

    def piano_note(self, r, key: float, need: float) -> np.ndarray:
        """A region's recording re-pitched to `key` at the output rate, at least `need` seconds when it loops."""
        from fractions import Fraction as Fr
        from scipy.signal import resample_poly
        cache = self.__dict__.setdefault("_piano_notes", {})
        x = self.piano_wave(r.path)
        sr = self.__dict__["_piano_waves"][r.path][1]
        if r.loop == "auto" and x.shape[1] / sr < need and x.shape[1] > sr:
            # A sustain that runs out: loop its steady middle with a crossfade at the seam.
            lc = self.__dict__.setdefault("_auto_loops", {})
            if r.path not in lc:
                n = x.shape[1]
                # End the loop before the recording's own release: the last moment the level is
                # still within 4 dB of the sustain's median.
                w = int(0.1 * sr)
                env = np.sqrt((x[:, : n // w * w].mean(axis=0).reshape(-1, w).astype(np.float64) ** 2).mean(axis=1))
                db = 20 * np.log10(env + 1e-9)
                body_db = np.median(db[len(db) // 3: len(db) * 3 // 4])
                ok = np.nonzero(db >= body_db - 4)[0]
                b = int((ok[-1] + 1) * w) if len(ok) else n - int(0.15 * sr)
                a = min(int(n * 0.35), b // 2)
                xf = min(int(0.3 * sr), (b - a) // 3)
                t = np.linspace(0, np.pi / 2, xf, dtype=np.float32)
                body = x[:, a:b - xf].copy()
                body[:, :xf] = x[:, a:a + xf] * np.sin(t) + x[:, b - xf:b] * np.cos(t)
                lc[r.path] = (x[:, :b - xf], body)
            head, body = lc[r.path]
            reps = int((need * sr - head.shape[1]) / max(body.shape[1], 1)) + 1
            x = np.concatenate([head] + [body] * reps, axis=1)
        elif isinstance(r.loop, tuple) and x.shape[1] / sr < need:
            a, b = r.loop
            reps = int((need * sr - x.shape[1]) / max(b - a, 1)) + 1
            x = np.concatenate([x[:, :b]] + [x[:, a:b]] * reps, axis=1)
        semis = (key - r.center) * r.keytrack + r.tune / 100
        k = (r.path, round(semis, 4), x.shape[1])
        if k not in cache:
            ratio = Fr(OUT_SR / sr / 2 ** (semis / 12)).limit_denominator(2000)
            cache[k] = x if ratio == 1 else resample_poly(x, ratio.numerator, ratio.denominator, axis=1).astype(np.float32)
            if len(cache) > 400:
                cache.pop(next(iter(cache)))
        return cache[k]

    def render_piano(self, tr: Track, notes: list[Note], n_out: int, part=(0, 1)) -> np.ndarray:
        """Sampled instruments (pianos, the orchestra). Each note plays the recorded key nearest to it at
        its velocity layer, alternating takes where there are several; when the note ends the sound
        stops over the release time (a piano's dampers, a bow lifting), and a piano's key-up
        recordings (string resonance, hammer and damper noise) sound, quieter the longer it was held."""
        from . import piano
        from .pattern import rand as prand
        opt = tr.inst.sample
        bank = self.piano_set(tr)
        kit = opt.get("kit")
        out = np.zeros((2, n_out), np.float32)
        tseed = zlib.crc32(tr.name.encode()) % 1000003
        players = max(1, min(len(ENSEMBLE), int(opt.get("players", 1))))
        hum = float(opt.get("humanize", 0.0)) / 1000
        for i, nt in enumerate(notes):
            if i % part[1] != part[0]:
                continue
            voice = None
            if kit:
                from .orchestra import ALIASES
                voice = ALIASES.get(nt.drum, nt.drum)
                if voice not in bank:
                    from .errors import suggest
                    raise PulsarError(f"orchestra percussion has no voice {voice!r}",
                                      hint=suggest(voice, list(bank)) or "voices: " + ", ".join(bank))
                regions, level = bank[voice]
                key = 60
                level *= 10 ** (opt["levels"].get(voice, 0.0) / 20)
            else:
                if nt.pitch is None:
                    continue
                regions, level = bank
                key = nt.pitch + opt["transpose"]
            # A person plays it: a few ms early or late, a little louder or softer, every note different.
            jitter = (prand(self.song.seed, tseed, i, 1) - 0.5) * 2 * hum
            v = nt.vel * (1 + (prand(self.song.seed, tseed, i, 2) - 0.5) * 0.12) if hum else nt.vel
            vel = int(min(127, max(1, round(v * 127))))
            for k in range(players):
                cents, delay, pan = ENSEMBLE[k]
                r = piano.find(regions, int(round(key)), vel, rr=i + k)
                if r is None:
                    continue
                hold = nt.dur + opt.get("pedal", 0.0) + opt.get("hold", 0.0)
                if r.loop == "auto":
                    hold += 0.1  # sustained notes overlap the next a little, like legato bowing / breath
                rel = opt["release"] if opt.get("release") is not None else r.release
                if kit:
                    rel = 0.12
                x = self.piano_note(r, key + (cents / (300 if kit else 100)), hold + rel)
                amp = (1 + r.veltrack * opt["dynamics"] * ((vel / 127) ** 2 - 1)) * 10 ** (r.volume / 20)
                amp /= math.sqrt(players)
                stop = int(hold * OUT_SR)
                if kit and not voice.endswith("roll"):
                    y = x  # percussion rings out (rolls stop with their note, so a roll can cut dead)
                elif stop < x.shape[1]:
                    # Release: an exponential fade reaching -60 dB over the release time.
                    n_rel = max(16, int(rel * OUT_SR))
                    end = min(x.shape[1], stop + n_rel)
                    y = x[:, :end].copy()
                    y[:, stop:end] *= np.exp(np.linspace(0, -6.9, end - stop, dtype=np.float32))
                else:
                    y = x
                spread = (prand(self.song.seed, tseed, i, 3 + k) - 0.5) * hum if k else 0.0
                s0 = int(round((nt.time + jitter + delay + spread) * OUT_SR))
                if s0 < 0:
                    y, s0 = y[:, -s0:], 0
                e0 = min(n_out, s0 + y.shape[1])
                if e0 > s0:
                    gl, gr = math.cos((pan + 1) * math.pi / 4) * math.sqrt(2), math.sin((pan + 1) * math.pi / 4) * math.sqrt(2)
                    out[0, s0:e0] += y[0, :e0 - s0] * (amp * level * gl)
                    out[1, s0:e0] += y[1, :e0 - s0] * (amp * level * gr)
            if kit and opt.get("thump", 0) > 0 and voice in THUMP:
                # The trailer-drum low end: a pitched-down sine thump under the skin.
                z = thump_wave(THUMP[voice])
                s0 = int(round((nt.time + jitter) * OUT_SR))
                if s0 < 0:  # humanized to just before the song starts
                    z, s0 = z[-s0:], 0
                e0 = min(n_out, s0 + len(z))
                if e0 > s0:
                    g = opt["thump"] * 0.35 * (vel / 127) ** 1.5 * 10 ** (opt["levels"].get(voice, 0.0) / 20)
                    out[:, s0:e0] += z[:e0 - s0] * g
            stop = int((nt.dur + opt.get("pedal", 0.0)) * OUT_SR)
            s0 = int(round(nt.time * OUT_SR))
            if tr.inst.kind != "piano":
                continue
            # Key-up sounds.
            t_off = s0 + stop
            for rr in piano.releases(regions, int(round(key)), vel):
                mix = opt["hammer"] if rr.keytrack == 0 else opt["resonance"]
                if mix <= 0:
                    continue
                z = self.piano_note(rr, key, 0)
                g = mix * level * 10 ** ((rr.volume - rr.rt_decay * hold) / 20)
                g *= 1 + rr.veltrack * ((vel / 127) ** 2 - 1)
                e1 = min(n_out, t_off + z.shape[1])
                if e1 > t_off and g > 1e-5:
                    out[:, t_off:e1] += z[:, :e1 - t_off] * g
        return out

    # -- plugins (VST3 / AU) -----------------------------------------------------------

    def render_plugin(self, tr: Track, notes: list[Note], n_out: int, part=(0, 1)) -> np.ndarray:
        """The whole track as one performance: the notes go to the plugin as MIDI, its parameters
        follow the automation sample by sample, and the result is calibrated to the reference level."""
        from . import plugins
        out = np.zeros((2, n_out), np.float32)
        if part[0] != 0 or not notes:
            return out
        opt = tr.inst.sample
        path = plugins.ensure(opt["name"], self.log)
        import dawdreamer as daw
        eng = daw.RenderEngine(OUT_SR, 512)
        try:
            proc = eng.make_plugin_processor(f"plugin_{tr.name}", str(path))
        except Exception as e:  # noqa: BLE001 -- the host raises plain exceptions
            raise PulsarError(f"could not load {path.name}: {e}") from None
        if opt.get("state"):
            if not Path(opt["state"]).exists():
                raise PulsarError(f"plugin state file not found: {opt['state']}")
            proc.load_state(opt["state"])
        if opt.get("preset"):
            pre = plugins.find_preset(opt["name"], opt["preset"])
            if pre.suffix.lower() == ".vstpreset":
                ok = proc.load_vst3_preset(str(pre))
            elif pre.suffix.lower() == ".fxp":
                ok = proc.load_state(str(plugins.state_from_fxp(pre)))
            else:
                raise PulsarError(f"Pulsar can't load {pre.suffix} presets yet", hint="use .fxp or .vstpreset files")
            if ok is False:
                raise PulsarError(f"{path.stem} refused the preset {pre.name}")
        desc = proc.get_parameters_description()
        index = {}
        for d in desc:
            index.setdefault(plugins.norm(d["name"]), d["index"])

        def param(name):
            if plugins.norm(name) not in index:
                from .errors import suggest
                raise PulsarError(f"{path.stem} has no parameter {name!r}",
                                  hint=suggest(plugins.norm(name), list(index)) or
                                  f"pulsar plugins params {opt['name']} lists them")
            return index[plugins.norm(name)]
        for k, v in opt["params"].items():
            proc.set_parameter(param(k), float(v))
        # A short silent lead-in: a plugin can finish setting up during its first blocks.
        pre = 0.25
        for nt in notes:
            if nt.pitch is None:
                continue
            key = int(round(nt.pitch + opt.get("transpose", 0)))
            if 0 <= key <= 127:
                proc.add_midi_note(key, int(min(127, max(1, round(nt.vel * 127)))), pre + float(nt.time),
                                   max(0.01, float(nt.dur)))
        lead = int(pre * OUT_SR)
        for (trk, path_), spans in self.score.autos.items():
            if trk == tr.name and path_[0] == "plugin":
                i = param(path_[1])
                curve = self.control_curve(spans, proc.get_parameter(i), n_out, OUT_SR)
                curve = np.concatenate([np.full(lead, curve[0] if len(curve) else 0.0), curve])
                proc.set_automation(i, np.clip(curve, 0, 1).astype(np.float32))
        eng.load_graph([(proc, [])])
        tail = 4.0
        eng.render(pre + n_out / OUT_SR + tail)
        a = eng.get_audio()[:2]
        if a.shape[0] == 1:
            a = np.repeat(a, 2, axis=0)
        a = a[:, lead:lead + n_out].astype(np.float32)
        out[:, :a.shape[1]] = a
        if opt.get("calibrate", True):
            # One gain for the whole track (its own dynamics stay): loudest 400 ms at the reference level.
            win = int(0.4 * OUT_SR)
            sq = (out.astype(np.float64) ** 2).mean(axis=0)
            c = np.concatenate([[0], np.cumsum(sq)])
            mom = (c[win:] - c[:-win]) / win if len(sq) > win else np.array([sq.mean()])
            rms = math.sqrt(max(float(mom.max()), 1e-12))
            if rms > 1e-6:
                out *= 10 ** (REF_MOMENTARY_DB / 20) / rms
        return out

    # -- ambience ----------------------------------------------------------------------

    def ambience_source(self, tr: Track):
        """(wav path, stock.Sound) for an ambience track; downloaded once, then cached."""
        from . import stock
        opt = tr.inst.sample
        return stock.resolve(opt["query"], opt["license"], opt["pick"], opt["id"], opt["min_length"])

    def render_ambience(self, tr: Track, notes: list[Note], n_out: int, part=(0, 1)) -> np.ndarray:
        """Each run of back-to-back plays becomes one continuous stretch of the recording, looped
        with an equal-power crossfade and faded in and out at its edges. The recording runs on
        song time, so a bed that stops and starts picks up where the song is, not from the top."""
        out = np.zeros((2, n_out), np.float32)
        if part[0] != 0 or not notes:
            return out
        opt = tr.inst.sample
        cache = self.__dict__.setdefault("_ambience", {})
        path = str(self.ambience_source(tr)[0])
        if path not in cache:
            import soundfile as sf
            x, sr = sf.read(path, always_2d=True, dtype="float32")
            x = x.T
            if x.shape[0] == 1:
                x = np.repeat(x, 2, axis=0)
            x = x[:2]
            if sr != OUT_SR:
                from math import gcd
                from scipy.signal import resample_poly
                g = gcd(OUT_SR, sr)
                x = resample_poly(x, OUT_SR // g, sr // g, axis=1).astype(np.float32)
            x = x - x.mean(axis=1, keepdims=True)
            # Loop point: the tail crossfades into the head (equal power), so the loop has no seam.
            xf = min(int(2.0 * OUT_SR), x.shape[1] // 4)
            if xf > 16:
                t = np.linspace(0, np.pi / 2, xf, dtype=np.float32)
                loop = x[:, :-xf].copy()
                loop[:, :xf] = x[:, :xf] * np.sin(t) + x[:, -xf:] * np.cos(t)
            else:
                loop = x
            # Calibrate a steady bed by its average loudness (not its loudest moment, like a hit).
            rms = math.sqrt(max(float((loop.astype(np.float64) ** 2).mean()), 1e-12))
            cache[path] = loop * (10 ** (REF_MOMENTARY_DB / 20) / rms if rms > 1e-7 else 1.0)
        loop = cache[path]
        L = loop.shape[1]
        spans = []
        for nt in sorted(notes, key=lambda n: n.time):
            a, b = nt.time, nt.time + nt.dur
            if spans and a <= spans[-1][1] + 0.05:
                spans[-1][1] = max(spans[-1][1], b)
                spans[-1][2] = max(spans[-1][2], nt.vel)
            else:
                spans.append([a, b, nt.vel])
        off = int(opt["start"] * OUT_SR)
        for a, b, vel in spans:
            s, e = int(round(a * OUT_SR)), min(n_out, int(round(b * OUT_SR)))
            if e <= s:
                continue
            idx = (np.arange(s, e) + off) % L
            seg = loop[:, idx] * (vel / 0.8)
            f = min(int(opt["fade"] * OUT_SR), (e - s) // 2)
            if f > 1:
                ramp = np.sin(np.linspace(0, np.pi / 2, f, dtype=np.float32)) ** 2
                seg[:, :f] *= ramp
                seg[:, -f:] *= ramp[::-1]
            out[:, s:e] += seg
        return out

    # -- voices ------------------------------------------------------------------------

    def render_voice(self, tr: Track, notes: list[Note], n_out: int, part=(0, 1)) -> np.ndarray:
        """Sung: each note takes the next lyric word, re-pitched and stretched onto it.
        Spoken: each hit speaks the next lyric line. Lyrics advance in song order."""
        from fractions import Fraction as Fr
        from scipy.signal import resample_poly
        from . import tts
        opt = tr.inst.sample
        n_int = n_out * OS
        buf = np.zeros((2, n_int), np.float32)
        ups = Fr(INTERNAL_SR, tts.TTS_SR)
        # one lyric unit per onset (a chord's notes share one word, the top note sings it)
        onsets = sorted({round(n.time, 5) for n in notes})
        sung = opt["mode"] == "sung"
        units = tts.lyric_units(opt["lyrics"]) if sung else tts.lyric_lines(opt["lyrics"])
        if not units:
            return downsample(buf, OS)
        by_time: dict = {}
        for n in notes:
            by_time.setdefault(round(n.time, 5), []).append(n)
        rendered = []
        if sung:
            rendered = self._sung_phrases(opt, onsets, by_time, units, part)
        for i, t in enumerate([] if sung else onsets):
            if i % part[1] != part[0]:
                continue
            group = by_time[t]
            note = max(group, key=lambda n: n.pitch if n.pitch is not None else -1)
            text = units[i % len(units)]
            spoken = tts.speak(text, opt["voice"], opt["speed"], opt["lang"])
            if sung:
                midi = (note.pitch if note.pitch is not None else 69) + opt["transpose"]
                y, lead = tts.sing(spoken, midi, max(note.dur, 0.12), opt["inflection"], opt["vibrato"],
                                   opt["vib_rate"], opt["formant"])
            else:
                y, lead = tts._trim(spoken), 0.0
            doubled = opt["double"] > 0 and sung
            rendered.append((t - lead, note.vel, y, -0.35 if doubled else 0.0))
            if doubled:  # a second take: slightly sharp, a touch late, its own vibrato, panned opposite
                y2, lead2 = tts.sing(spoken, midi + 0.08, max(note.dur, 0.12), opt["inflection"] * 1.3,
                                     opt["vibrato"] * 1.2, opt["vib_rate"] * 0.93, opt["formant"])
                rendered.append((t - lead2 + 0.018, note.vel * opt["double"], y2, 0.35))
        if not rendered:
            return downsample(buf, OS)
        # level: loudest 200 ms of the sung material to the reference, like every other instrument
        cat = np.concatenate([y for _, _, y, _ in rendered])
        win = int(0.2 * tts.TTS_SR)
        sq = cat.astype(np.float64) ** 2
        c = np.concatenate([[0], np.cumsum(sq)])
        mom = (c[win:] - c[:-win]) / win if len(sq) > win else np.array([sq.mean()])
        level = 10 ** (REF_MOMENTARY_DB / 20) / max(np.sqrt(float(mom.max())), 1e-7)
        for t, vel, y, pan in rendered:
            up = resample_poly(y, ups.numerator, ups.denominator).astype(np.float32) * (level * vel / 0.8)
            s = int(round(max(t, 0) * INTERNAL_SR))
            e = min(n_int, s + len(up))
            if e <= s:
                continue
            p = pan * 0.5 + 0.5  # constant-power pan
            buf[0, s:e] += up[:e - s] * math.sqrt(1 - p) * math.sqrt(2)
            buf[1, s:e] += up[:e - s] * math.sqrt(p) * math.sqrt(2)
        return downsample(buf, OS)

    def _sung_phrases(self, opt, onsets, by_time, units, part):
        """Group notes into legato phrases and sing each as one continuous performance."""
        from . import tts
        notes = [max(by_time[t], key=lambda n: n.pitch if n.pitch is not None else -1) for t in onsets]
        phrases, cur = [], []
        for i, n in enumerate(notes):
            if cur:
                prev = notes[i - 1]
                if n.time - (prev.time + prev.dur) > 0.08:
                    phrases.append(cur)
                    cur = []
            cur.append(i)
        if cur:
            phrases.append(cur)
        out = []
        for pi, idxs in enumerate(phrases):
            if pi % part[1] != part[0]:
                continue
            t0 = notes[idxs[0]].time
            words = [tts.speak(units[i % len(units)], opt["voice"], opt["speed"], opt["lang"]) for i in idxs]
            spec = [((notes[i].pitch if notes[i].pitch is not None else 57) + opt["transpose"],
                     notes[i].time - t0, max(notes[i].dur, 0.1)) for i in idxs]
            vel = float(np.mean([notes[i].vel for i in idxs]))
            y, lead = tts.sing_phrase(words, spec, opt["vibrato"], opt["vib_rate"], formant=opt["formant"])
            lead += _audible_onset(y, tts.TTS_SR)
            doubled = opt["double"] > 0
            out.append((t0 - lead, vel, y, -0.3 if doubled else 0.0))
            if doubled:
                spec2 = [(m + 0.06, s, d) for m, s, d in spec]
                y2, lead2 = tts.sing_phrase(words, spec2, opt["vibrato"] * 1.15, opt["vib_rate"] * 0.94,
                                            formant=opt["formant"])
                lead2 += _audible_onset(y2, tts.TTS_SR)
                out.append((t0 - lead2, vel * opt["double"], y2, 0.3))
        return out

    # -- effects & mixing -------------------------------------------------------------

    def effect_params(self, eff) -> dict:
        p = dict(eff.params)
        if eff.name == "delay":
            if "time_ms" in p:
                p["time"] = p.pop("time_ms") / 1000.0
            else:
                p["time"] = self.secs(p.get("time", Fraction(3, 16)))
        if eff.name == "tremolo" and "sync" in p:
            p["rate"] = 1.0 / max(self.secs(p.pop("sync")), 1e-4)
        return {k: float(v) for k, v in p.items()}

    def apply_effect(self, eff, audio, autos: dict | None = None, tail=True) -> np.ndarray:
        code, spec, tailfn = FX.EFFECTS[eff.name]
        params = self.effect_params(eff)
        automation = None
        if autos:
            automation = {}
            n = audio.shape[1] + (int(tailfn(params) * OUT_SR) if tail else 0)
            for pname, spans in autos.items():
                automation[pname] = self.control_curve(spans, params.get(pname, spec[pname][0]), n, OUT_SR)
        out = self.fx.process(code, audio, params, automation, tail=0)
        return out

    def duck_curve(self, tr: Track, n: int) -> np.ndarray | None:
        amt = float(tr.mixer.get("duck", 0) or 0)
        if amt <= 0:
            return None
        src_name = tr.mixer.get("duck_by")
        times = []
        if src_name:
            if str(src_name) not in self.song.tracks:
                raise PulsarError(f"track {tr.name!r}: duck_by names unknown track {src_name!r}",
                                  hint=suggest(str(src_name), self.song.tracks))
            src = self.song.tracks[str(src_name)]
            times = [x.time for x in self.score.notes[src.name] if src.inst.kind != "drums" or x.drum == "bd"]
        else:
            for name, t2 in self.song.tracks.items():
                if t2.inst.kind == "drums":
                    times += [x.time for x in self.score.notes[name] if x.drum == "bd"]
        if not times:
            return None
        rel = float(tr.mixer.get("duck_release", 0.0)) or self.bar / 4 * 0.9
        g = np.ones(n, np.float32)
        L = int(rel * OUT_SR)
        att = int(0.004 * OUT_SR)
        x = np.arange(L) / L
        shape = (1 - amt * (1 - x) ** 2).astype(np.float32)
        pre = np.linspace(1, shape[0], att, dtype=np.float32)
        for t in sorted(set(round(t, 4) for t in times)):
            s = int(t * OUT_SR)
            a0 = max(0, s - att)
            g[a0:s] = np.minimum(g[a0:s], pre[att - (s - a0):])
            e = min(n, s + L)
            if e > s:
                g[s:e] = np.minimum(g[s:e], shape[:e - s])
        return g

    def mixer(self, tr: Track, audio: np.ndarray) -> np.ndarray:
        n = audio.shape[1]
        gain_spans = self.autos_for(tr.name, ["mixer", "gain"])
        base = float(tr.inst.base_gain)
        g_db = float(tr.mixer.get("gain", 0.0))
        if gain_spans:
            # gain automation is a volume envelope on top of the track's fader
            gain = 10 ** ((base + g_db + self.control_curve(gain_spans, 0.0, n, OUT_SR)) / 20)
        else:
            gain = np.float32(10 ** ((base + g_db) / 20))
        audio = audio * gain
        width = tr.mixer.get("width")
        if width is not None:
            m = (audio[0] + audio[1]) * 0.5
            s = (audio[0] - audio[1]) * 0.5 * float(width)
            audio = np.stack([m + s, m - s])
        pan_spans = self.autos_for(tr.name, ["mixer", "pan"])
        pan = float(tr.mixer.get("pan", 0.0))
        if pan_spans or pan:
            pv = self.control_curve(pan_spans, pan, n, OUT_SR) if pan_spans else np.float32(pan)
            pv = np.clip(pv, -1, 1) * 0.5 + 0.5
            audio = np.stack([audio[0] * np.sqrt(1 - pv) * math.sqrt(2), audio[1] * np.sqrt(pv) * math.sqrt(2)])
        duck = self.duck_curve(tr, n)
        if duck is not None:
            audio = audio * duck
        return audio.astype(np.float32)

    # -- top level --------------------------------------------------------------------

    def voice_audio(self, name: str, part=(0, 1)) -> np.ndarray:
        """A track's voices (or one interleaved share of them), summed and downsampled."""
        tr = self.song.tracks[name]
        notes = self.score.notes.get(name, [])
        n = self.length_samples(OUT_SR)
        if tr.inst.kind == "drums":
            return self.render_drums(tr, notes, n, part)[:, :n]
        if tr.inst.kind == "sampler":
            return self.render_sampler(tr, notes, n, part)[:, :n]
        if tr.inst.kind == "voice":
            return self.render_voice(tr, notes, n, part)[:, :n]
        if tr.inst.kind == "ambience":
            return self.render_ambience(tr, notes, n, part)
        if tr.inst.kind in ("piano", "orchestra"):
            return self.render_piano(tr, notes, n, part)
        if tr.inst.kind == "plugin":
            return self.render_plugin(tr, notes, n, part)
        return self.render_synth(tr, notes, n, part)[:, :n]

    def post_audio(self, name: str, audio: np.ndarray) -> np.ndarray:
        """Insert effects and the mixer (fader, width, pan, duck)."""
        tr = self.song.tracks[name]
        n = audio.shape[1]
        for i, eff in enumerate(tr.inst.inserts):
            autos = {k[1][2]: v for k, v in self.score.autos.items()
                     if k[0] == name and k[1][0] == "fx" and k[1][1] == i}
            audio = self.apply_effect(eff, audio, autos)[:, :n]
        return self.mixer(tr, audio)

    def track_audio(self, name: str) -> tuple[np.ndarray, float]:
        ts = time.time()
        return self.post_audio(name, self.voice_audio(name)), time.time() - ts

    def cost(self, name: str) -> float:
        """Rough render cost of a track, for splitting work across processes."""
        tr = self.song.tracks[name]
        notes = self.score.notes.get(name, [])
        if tr.inst.kind in ("drums", "sampler"):
            return len(notes) * 0.02
        if tr.inst.kind == "ambience":
            return 0.0
        if tr.inst.kind in ("piano", "orchestra"):
            return len(notes) * 0.03
        if tr.inst.kind == "plugin":
            return 0.0  # one performance, never split
        if tr.inst.kind == "voice":
            return len(notes) * 0.4
        uni = int(tr.inst.shape.get("unison", 1))
        rel = float(tr.inst.params.get("release", 0.3))
        return sum(nt.dur + rel for nt in notes) * (1 + uni)

    def render(self, want_stems=False, jobs: int | None = None) -> RenderResult:
        t0 = time.time()
        n = self.length_samples(OUT_SR)
        mix = np.zeros((2, n), np.float32)
        buses = {name: np.zeros((2, n), np.float32) for name in self.song.buses}
        stems, info = {}, {"tracks": {}}
        active = [name for name in self.song.tracks if self.score.notes.get(name)]
        for name in self.song.tracks:
            if name not in active:
                info["tracks"][name] = {"notes": 0}
        # Fetch stock recordings here, once, before any worker needs them; keep who made them.
        credits = []
        for name in active:
            tr = self.song.tracks[name]
            if tr.inst.kind == "ambience":
                self.log(f"  {name}: finding a recording for {tr.inst.sample['query']!r}")
                snd = self.ambience_source(tr)[1]
                self.log(f"  {name}: {snd.title} ({snd.license}, {snd.duration:.0f}s)")
                credits.append({"track": name, "title": snd.title, "creator": snd.creator,
                                "license": snd.license, "license_url": snd.license_url, "url": snd.page_url,
                                "credit": snd.credit()})
        from . import piano as _piano
        for kind_name in sorted({self.song.tracks[n].inst.sample["name"] for n in active
                                 if self.song.tracks[n].inst.kind == "piano"}):
            _piano.install(kind_name, self.log)
            users = [n for n in active if self.song.tracks[n].inst.kind == "piano"
                     and self.song.tracks[n].inst.sample["name"] == kind_name]
            credits.append(dict(_piano.credit(kind_name), track=", ".join(users)))
        if any(self.song.tracks[n].inst.kind == "orchestra" for n in active):
            from . import orchestra as _orch
            users = [t for t in active if self.song.tracks[t].inst.kind == "orchestra"]
            for t in users:  # fetch each instrument here, once, before any worker needs it
                o = self.song.tracks[t].inst.sample
                _orch.install("percussion" if o.get("kit") else o["name"], self.log)
            credits.append(dict(_orch.CREDIT, track=", ".join(users),
                                credit=f'"{_orch.CREDIT["title"]}" by {_orch.CREDIT["creator"]} -- CC0 (no '
                                       f'attribution required) -- {_orch.CREDIT["url"]}'))
        from . import library as _library
        for t in active:
            inst = self.song.tracks[t].inst
            if inst.kind == "sampler" and inst.sample.get("ref"):
                it = _library.fetch(inst.sample["ref"], self.log)[1]  # download once, before any worker
                credits.append({"track": t, "title": it.title, "creator": it.creator, "license": it.license,
                                "license_url": "", "url": it.url, "credit": it.credit()})
        from . import plugins as _plugins
        for t in active:
            inst = self.song.tracks[t].inst
            if inst.kind == "plugin":
                _plugins.ensure(inst.sample["name"], self.log)  # install once, before any worker needs it
                k = _plugins.key(inst.sample["name"])
                if k in _plugins.KNOWN:
                    title, _, _, lic, url = _plugins.KNOWN[k]
                    credits.append({"track": t, "title": title, "creator": title, "license": lic, "license_url": "",
                                    "url": url, "credit": f"{title} ({lic}) -- {url} (a synthesizer; nothing to credit "
                                                          f"in the music itself)"})
        info["credits"] = credits
        for name, (audio, secs) in self._track_results(active, jobs):
            tr = self.song.tracks[name]
            notes = self.score.notes[name]
            for bus in self.song.buses:
                amt = tr.mixer.get(bus)
                spans = self.autos_for(name, ["mixer", bus])
                if amt or spans:
                    if spans:
                        curve = self.control_curve(spans, float(amt or 0), n, OUT_SR)
                        buses[bus] += audio * curve
                    else:
                        buses[bus] += audio * float(amt)
            mix += audio
            peak = float(np.abs(audio).max())
            rms = float(np.sqrt((audio.astype(np.float64) ** 2).mean()))
            from .analyze import band_energies
            info["tracks"][name] = {"notes": len(notes), "peak_db": _db(peak), "rms_db": _db(rms),
                                    "bands": band_energies(audio[:, ::2], OUT_SR // 2),
                                    "seconds": round(secs, 2)}
            if want_stems:
                stems[name] = audio
            self.log(f"  rendered {name} ({len(notes)} notes) in {secs:.1f}s")
        for bus, chain in self.song.buses.items():
            sig = buses[bus]
            if not np.any(sig):
                continue
            for eff in chain:
                eff2 = type(eff)(eff.name, {**eff.params, "mix": 1.0})
                sig = self.apply_effect(eff2, sig)[:, :n]
            mix += sig
            if want_stems:
                stems[f"bus_{bus}"] = sig
        for eff in self.song.master:
            mix = self.apply_effect(eff, mix)[:, :n]
        master_autos = {k[1][0]: v for k, v in self.score.autos.items() if k[0] == "master"}
        mparams = {"glue": 0.35}
        automation = {k: self.control_curve(v, mparams.get(k, 0.0), n, OUT_SR) for k, v in master_autos.items()
                      if k != "gain"}
        mix = self.fx.process(FX.MASTER, mix, mparams, automation or None)[:, :n]
        if "gain" in master_autos:
            mix = mix * 10 ** (self.control_curve(master_autos["gain"], 0.0, n, OUT_SR) / 20)
        pre_peak = float(np.abs(mix).max())
        end = self.trim_point(mix)
        mix = mix[:, :end]
        out, minfo = master_bus(mix, OUT_SR, self.song.loudness, self.song.ceiling)
        fade = min(int(0.05 * OUT_SR), out.shape[1])
        out[:, out.shape[1] - fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        info.update(master=minfo, pre_master_peak_db=_db(pre_peak), seconds=round(out.shape[1] / OUT_SR, 2),
                    render_seconds=round(time.time() - t0, 2))
        if want_stems:
            stems = {k: v[:, :end] for k, v in stems.items()}
        return RenderResult(out, OUT_SR, stems, info)

    def _track_results(self, names, jobs):
        """Yield (name, (audio, seconds)) per track.

        With the song source available, heavy tracks are split into
        interleaved note shares rendered in parallel worker processes (each
        share is summed at the internal rate before downsampling, so the sum
        equals a serial render), then every track's effects and mixer run in
        parallel too. Workers are spawned (not forked: forking after the audio
        libraries load is unsafe on macOS) and rebuild the score from source.
        Buffers travel through temporary .npy files."""
        import multiprocessing as mp
        import tempfile

        jobs = jobs if jobs is not None else min(os.cpu_count() or 1, 8)
        if jobs <= 1 or len(names) == 0 or self.rebuild is None:
            for name in names:
                yield name, self.track_audio(name)
            return
        costs = {k: self.cost(k) for k in names}
        share = max(sum(costs.values()) / jobs, 1e-9)
        units = []
        for k in names:
            parts = max(1, min(jobs, int(round(costs[k] / share))))
            units += [(k, i, parts) for i in range(parts)]
        units.sort(key=lambda u: -costs[u[0]] / u[2])
        ctx = mp.get_context("spawn")
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="pulsar-") as tmp, \
                ctx.Pool(min(jobs, len(units)), initializer=_child_init, initargs=(self.rebuild,)) as pool:
            sums: dict = {}
            for name, path in pool.imap_unordered(_child_voice, [(u, tmp) for u in units]):
                part = np.load(path)
                os.unlink(path)
                sums[name] = part if name not in sums else sums[name] + part
            voices_s = time.time() - t0
            for name, audio in sums.items():
                np.save(os.path.join(tmp, f"s-{name}.npy"), audio)
            sums.clear()
            results = dict(pool.imap_unordered(_child_post, [(k, tmp) for k in names]))
            for name in names:
                path, secs = results[name]
                audio = np.load(path)
                os.unlink(path)
                yield name, (audio, voices_s + secs)

    def trim_point(self, mix) -> int:
        song_end = int(self.score.seconds * OUT_SR)
        a = np.abs(mix[:, song_end:]).max(axis=0) if mix.shape[1] > song_end else np.zeros(0)
        loud = np.nonzero(a > 10 ** (-70 / 20) * max(float(np.abs(mix).max()), 1e-9))[0]
        return song_end + (int(loud[-1]) + 1 if len(loud) else 0)


def _audible_onset(y: np.ndarray, sr: int, frac: float = 0.25, cap: float = 0.15) -> float:
    """Seconds from the start of a rendered phrase until it becomes clearly audible (its
    envelope first reaches `frac` of the peak). Sung phrases are placed that much earlier so
    a soft vowel swell lands on the beat instead of after it."""
    if len(y) == 0:
        return 0.0
    win = max(1, int(0.005 * sr))
    env = np.sqrt(np.convolve(y.astype(np.float64) ** 2, np.ones(win) / win, "same"))
    idx = np.nonzero(env >= frac * env.max())[0]
    return min(float(idx[0]) / sr, cap) if len(idx) else 0.0


def _db(x: float) -> float:
    return round(20 * math.log10(max(x, 1e-9)), 1)


def _load_calib() -> dict:
    try:
        return json.loads(_CALIB_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_calib(d: dict):
    try:
        _CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
        merged = {**_load_calib(), **d}
        tmp = _CALIB_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(merged))
        os.replace(tmp, _CALIB_PATH)
    except OSError:
        pass


_CHILD = None


def _child_init(rebuild):
    global _CHILD
    from .song import build_score
    source, filename, only = rebuild
    _, score, _ = build_score(source, filename, only)
    _CHILD = Renderer(score)


def _child_voice(arg):
    (name, part, parts), tmp = arg
    audio = _CHILD.voice_audio(name, (part, parts))
    path = os.path.join(tmp, f"v-{name}-{part}-{os.getpid()}.npy")
    np.save(path, audio)
    return name, path


def _child_post(arg):
    name, tmp = arg
    t = time.time()
    src = os.path.join(tmp, f"s-{name}.npy")
    audio = _CHILD.post_audio(name, np.load(src))
    path = os.path.join(tmp, f"p-{name}.npy")
    np.save(path, audio)
    return name, (path, time.time() - t)


def render(score: Score, stems=False, log=None, jobs: int | None = None, rebuild=None) -> RenderResult:
    """Render a score. Pass rebuild=(source, filename, only) to enable parallel workers."""
    return Renderer(score, log, rebuild).render(stems, jobs)
