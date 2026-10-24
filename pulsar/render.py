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
