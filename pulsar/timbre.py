"""Timbre fingerprints and patch matching.

A fingerprint describes how an instrument *sounds*, independent of what it
plays: its spectral envelope, the average loudness envelope after a note
starts, how its brightness moves after the onset (the filter envelope), and
its register. `match_patch` searches synth parameters so test notes rendered
through Pulsar produce the same fingerprint as a reference recording.
"""
from __future__ import annotations

import math
import random

import numpy as np
from scipy.signal import stft

N_BANDS = 32
BAND_EDGES = np.geomspace(60, 16000, N_BANDS + 1)
ENV_MS = np.arange(0, 600, 10)  # loudness envelope after onset
BRIGHT_MS = np.arange(0, 400, 20)  # centroid after onset
HOP = 256


def _mono(x):
    return x.mean(axis=0) if x.ndim == 2 else x


def onsets(mono: np.ndarray, sr: int, min_gap: float = 0.09) -> np.ndarray:
    """Onset times (s) from peaks of positive spectral flux."""
    f, t, Z = stft(mono, sr, nperseg=1024, noverlap=1024 - HOP)
    mag = np.log1p(10 * np.abs(Z))
    flux = np.maximum(np.diff(mag, axis=1), 0).sum(axis=0)
    flux = np.convolve(flux, np.hanning(5) / np.hanning(5).sum(), "same")
    thr = np.median(flux) + 1.5 * np.std(flux)
    gap = int(min_gap * sr / HOP)
    out, last = [], -gap
    for i in range(1, len(flux) - 1):
        if flux[i] > thr and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1] and i - last >= gap:
            out.append(t[i + 1])
            last = i
    return np.array(out)


def f0_track(mono: np.ndarray, sr: int, fmin=35.0, fmax=1500.0, frame=4096, hop=2048) -> np.ndarray:
    """Per-frame f0 (Hz) with the YIN cumulative-mean-normalized difference; NaN where unvoiced."""
    out = []
    lo, hi = max(2, int(sr / fmax)), min(frame // 2, int(sr / fmin))
    for s in range(0, len(mono) - frame, hop):
        x = mono[s:s + frame]
        x = x - x.mean()
        if float((x * x).mean()) < 1e-7:
            out.append(np.nan)
            continue
        w = frame // 2
        cs = np.concatenate([[0], np.cumsum(x * x)])
        r = np.fft.irfft(np.fft.rfft(x, 2 * frame) * np.conj(np.fft.rfft(x[:w], 2 * frame)))[:w]
        e0 = cs[w] - cs[0]
        et = cs[np.arange(w) + w] - cs[np.arange(w)]
        d = e0 + et - 2 * r
        d[0] = 0
        cm = d[1:] * np.arange(1, w) / (np.cumsum(d[1:]) + 1e-12)
        cm = np.concatenate([[1], cm])
        cand = np.nonzero(cm[lo:hi] < 0.15)[0]
        if len(cand) == 0:
            out.append(np.nan)
            continue
        k = lo + cand[0]
        while k + 1 < hi and cm[k + 1] < cm[k]:
            k += 1
        out.append(sr / k)
    return np.array(out)


def fingerprint(x: np.ndarray, sr: int, max_onsets: int = 400) -> dict:
    mono = _mono(np.asarray(x, dtype=np.float64))
    f, t, Z = stft(mono, sr, nperseg=8192, noverlap=8192 - 1024)
    P = np.abs(Z) ** 2
    # spectral envelope: long-term average power per log-spaced band, frames above -40 dB only
    frame_e = P.sum(axis=0)
    active = frame_e > frame_e.max() * 1e-4
    ltas = np.array([P[(f >= a) & (f < b)][:, active].mean() if np.any((f >= a) & (f < b)) else 1e-20
                     for a, b in zip(BAND_EDGES[:-1], BAND_EDGES[1:])])
    spec = 10 * np.log10(ltas + 1e-20)
    empty = spec < -150
    if empty.all():  # silence: a fingerprint that scores as far away as possible
        spec[:] = -60.0
    elif empty.any():  # bands narrower than an FFT bin: interpolate from neighbours
        idx = np.arange(N_BANDS)
        spec[empty] = np.interp(idx[empty], idx[~empty], spec[~empty])
    spec -= spec.max()
    # onset-aligned loudness and brightness
    ons = onsets(mono, sr)
    iois = np.diff(ons) if len(ons) > 2 else np.array([0.5])
    if len(ons) > max_onsets:
        ons = ons[np.linspace(0, len(ons) - 1, max_onsets).astype(int)]
    env = np.zeros(len(ENV_MS))
    bright = np.zeros(len(BRIGHT_MS))
    n = 0
    win = int(0.01 * sr)
    for o in ons:
        s = int(o * sr)
        seg = mono[s:s + int(0.6 * sr) + win]
        if len(seg) < int(0.6 * sr):
            continue
        e = np.array([np.sqrt((seg[int(m / 1000 * sr): int(m / 1000 * sr) + win] ** 2).mean() + 1e-20) for m in ENV_MS])
        if e.max() < 1e-5:
            continue
        env += e / e.max()
        cols = np.searchsorted(t, o + BRIGHT_MS / 1000.0)
        cols = np.clip(cols, 0, P.shape[1] - 1)
        cen = (f[:, None] * P[:, cols]).sum(axis=0) / (P[:, cols].sum(axis=0) + 1e-20)
        bright += np.log2(np.maximum(cen, 20))
        n += 1
    if n:
        env /= n
        bright /= n
        bright -= bright[0]
    f0 = f0_track(mono, sr)
    voiced = f0[np.isfinite(f0)]
    reg = (float(np.median(voiced)), float(np.percentile(voiced, 10)), float(np.percentile(voiced, 90))) \
        if len(voiced) > 10 else (220.0, 110.0, 440.0)
    return {"spectrum": spec, "envelope": env, "brightness": bright, "onsets": int(n),
            "ioi_s": [float(v) for v in np.percentile(iois[iois < 2.0], [10, 25, 50, 75, 90])] if np.any(iois < 2.0) else [0.5] * 5,
            "onset_rate": float(len(ons) / (len(mono) / sr)), "register_hz": reg}


def distance(a: dict, b: dict) -> tuple[float, dict]:
    """Weighted fingerprint distance and its parts."""
    ds = float(np.sqrt(np.mean((np.maximum(a["spectrum"], -60) - np.maximum(b["spectrum"], -60)) ** 2)))  # dB
    de = float(np.sqrt(np.mean((a["envelope"] - b["envelope"]) ** 2))) * 40  # ~dB-ish scale
    db = float(np.sqrt(np.mean((a["brightness"] - b["brightness"]) ** 2))) * 12  # semitones of centroid
    return ds + de + db, {"spectrum_db": round(ds, 2), "envelope": round(de, 2), "brightness": round(db, 2)}


def describe(fp: dict) -> str:
    reg = fp["register_hz"]
    from .theory import midi_name
    m = lambda hz: midi_name(int(round(69 + 12 * math.log2(hz / 440))))
    env = fp["envelope"]
    half = next((ENV_MS[i] for i in range(len(env)) if env[i] < 0.5 * env.max()), ENV_MS[-1])
    peak = ENV_MS[int(np.argmax(env))]
    br = fp["brightness"]
    return (f"{fp['onsets']} onsets analysed ({fp['onset_rate']:.1f}/s); register {m(reg[1])}-{m(reg[2])} "
            f"(median {m(reg[0])}); envelope peaks at {peak} ms, halves by {half} ms; "
            f"brightness moves {br[-1] - br.max():+.2f} oct from its peak over 400 ms")


# -- patch matching --------------------------------------------------------------

SPACE_DISCRETE = {
    "wave1": ["saw", "square", "pulse", "tri"],
    "wave2": ["saw", "square", "sine", "none"],
    "filter": ["ladder", "korg", "svf", "oberheim", "diode"],
    "unison": [1, 2, 3],
    "osc2_semi": [0, 12, -12, 7],
}
SPACE_CONT = {  # name: (lo, hi, log-scaled)
    "cutoff": (0.12, 0.85, False), "resonance": (0.0, 0.7, False), "env_amount": (0.0, 5.0, False),
    "fdecay": (0.02, 1.2, True), "fsustain": (0.0, 0.8, False), "attack": (0.001, 0.06, True),
    "decay": (0.04, 1.5, True), "sustain": (0.0, 1.0, False), "release": (0.03, 1.0, True),
    "osc_mix": (0.0, 1.0, False), "detune": (0.0, 0.5, False), "drive": (0.0, 0.6, False),
    "pw": (0.1, 0.5, False), "reverb": (0.0, 0.5, False),
}


def test_phrase(register_midi: tuple[int, int], ioi_s: list[float], seconds: float = 10.0,
                chord_prob: float = 0.35, seed: int = 7) -> list[tuple[float, float, list[int], float]]:
    """A generated (not transcribed) phrase of random notes and small chords in a register.
    Only the *statistics* of the reference's note spacing are used (its onset-gap percentiles)."""
    rng = random.Random(seed)
    lo, hi = register_midi
    out, t = [], 0.1
    while t < seconds - 0.6:
        gap = rng.choice(ioi_s)
        root = rng.randint(lo, max(lo, hi - 7))
        notes = [root] + ([root + rng.choice([3, 4]), root + 7] if rng.random() < chord_prob else [])
        out.append((t, gap * rng.uniform(0.6, 0.95), notes, rng.uniform(0.65, 1.0)))
        t += gap
    return out


class PatchMatcher:
    def __init__(self, target: dict, register_midi: tuple[int, int], log=print, seed: int = 1, role: str = "lead"):
        from .dsp.engine import Engine
        self.target = target
        self.role = role
        self.phrase = test_phrase(register_midi, target.get("ioi_s", [0.5] * 5),
                                  chord_prob=0.0 if role == "bass" else 0.35)
        self.voice = Engine(96000)
        self.fx = Engine(48000)
        self.log = log
        self.rng = random.Random(seed)
        self.evals = 0

    def render(self, patch: dict) -> np.ndarray:
        from .dsp import effects as FX
        from .dsp import faust as FZ
        from .dsp.master import downsample
        from .theory import midi_to_hz
        code = FZ.analog_voice(patch["wave1"], patch["wave2"], int(patch["unison"]), patch["filter"])
        params = {k: v for k, v in patch.items() if k in FZ.ANALOG_PARAMS}
        n = int(10.5 * 96000)
        buf = np.zeros((2, n), np.float32)
        for i, (t, dur, notes, vel) in enumerate(self.phrase):
            for m in notes:
                a = self.voice.render(code, dict(params, freq=midi_to_hz(m), vel=vel, dur=dur, seed=i, level=1.0),
                                      dur + float(patch["release"]) + 0.05)
                s = int(t * 96000)
                e = min(n, s + a.shape[1])
                buf[:, s:e] += a[:, :e - s]
        out = downsample(buf, 2)
        if patch.get("reverb", 0) > 0.01:
            code_r, _, _ = FX.EFFECTS["reverb"]
            out = self.fx.process(code_r, out, {"mix": float(patch["reverb"]), "decay": 2.5, "size": 0.7})
        return out

    def score(self, patch: dict) -> tuple[float, dict]:
        self.evals += 1
        fp = fingerprint(self.render(patch), 48000)
        return distance(fp, self.target)

    def space(self):
        disc, cont = dict(SPACE_DISCRETE), dict(SPACE_CONT)
        if self.role == "bass":  # keep basses musical: octave layers only, little reverb, no unison smear
            disc.update(osc2_semi=[0, 12, -12], unison=[1], wave2=["saw", "square", "sine", "none"])
            cont.update(reverb=(0.0, 0.08, False), detune=(0.0, 0.2, False))
        return disc, cont

    def random_patch(self) -> dict:
        disc, cont = self.space()
        p = {k: self.rng.choice(v) for k, v in disc.items()}
        for k, (lo, hi, lg) in cont.items():
            p[k] = math.exp(self.rng.uniform(math.log(lo), math.log(hi))) if lg else self.rng.uniform(lo, hi)
        return p

    def search(self, start: dict | None = None, random_trials: int = 40, rounds: int = 3) -> tuple[dict, float, dict]:
        best = start or self.random_patch()
        best_d, best_parts = self.score(best)
        for i in range(random_trials):
            cand = self.random_patch()
            d, parts = self.score(cand)
            if d < best_d:
                best, best_d, best_parts = cand, d, parts
                self.log(f"  random {i + 1}: {d:.2f} {parts}")
        disc, cont = self.space()
        for r in range(rounds):
            improved = False
            for k, opts in disc.items():
                for v in opts:
                    if v == best[k]:
                        continue
                    cand = dict(best, **{k: v})
                    d, parts = self.score(cand)
                    if d < best_d:
                        best, best_d, best_parts, improved = cand, d, parts, True
            scale = 0.35 / (r + 1)
            for k, (lo, hi, lg) in cont.items():
                for sign in (-1, 1):
                    if lg:
                        v = math.exp(min(math.log(hi), max(math.log(lo), math.log(best[k]) + sign * scale * 2)))
                    else:
                        v = min(hi, max(lo, best[k] + sign * scale * (hi - lo)))
                    cand = dict(best, **{k: v})
                    d, parts = self.score(cand)
                    if d < best_d:
                        best, best_d, best_parts, improved = cand, d, parts, True
            self.log(f"  round {r + 1}: {best_d:.2f} {best_parts} ({self.evals} renders)")
            if not improved:
                break
        return best, best_d, best_parts


def patch_to_pulse(patch: dict, preset_note: str = "") -> str:
    shape = ", ".join(f'{k}: "{patch[k]}"' for k in ("wave1", "wave2", "filter"))
    nums = ", ".join(f"{k}: {round(float(patch[k]), 3)}" for k in SPACE_CONT if k != "reverb")
    return (f'analog({shape}, unison: {int(patch["unison"])}, osc2_semi: {int(patch["osc2_semi"])}, {nums}, '
            f'reverb: {round(float(patch["reverb"]), 2)})')


# -- drum matching --------------------------------------------------------------

DRUM_BANDS = np.geomspace(30, 18000, 29)
KICK_MS = np.arange(0, 500, 10)
HAT_MS = np.arange(0, 100, 2)  # short window: later energy is usually the next hit
PITCH_MS = np.arange(5, 160, 5)


def _band_env(x, sr, lo, hi, times_ms, win_ms=5):
    from scipy.signal import butter, sosfilt
    sos = butter(4, [lo, hi], "bandpass", fs=sr, output="sos") if lo > 0 else butter(4, hi, "low", fs=sr, output="sos")
    y = sosfilt(sos, x)
    w = max(1, int(win_ms / 1000 * sr))
    return np.array([np.sqrt((y[int(m / 1000 * sr): int(m / 1000 * sr) + w] ** 2).mean() + 1e-20) for m in times_ms])


def _hit_spectrum(seg, sr, length_ms):
    n = int(length_ms / 1000 * sr)
    s = seg[:n] * np.hanning(2 * n)[n:] if len(seg) >= n else seg
    X = np.abs(np.fft.rfft(s, 16384)) ** 2
    f = np.fft.rfftfreq(16384, 1 / sr)
    spec = np.array([X[(f >= a) & (f < b)].mean() for a, b in zip(DRUM_BANDS[:-1], DRUM_BANDS[1:])])
    spec = 10 * np.log10(spec + 1e-20)
    return spec - spec.max()


def _kick_pitch(seg, sr):
    """Dominant frequency (Hz) below 250 Hz over the first 160 ms."""
    out = []
    n = int(0.03 * sr)
    for m in PITCH_MS:
        s = int(m / 1000 * sr)
        w = seg[s:s + n]
        if len(w) < n:
            out.append(np.nan)
            continue
        X = np.abs(np.fft.rfft(w * np.hanning(n), 16384))
        f = np.fft.rfftfreq(16384, 1 / sr)
        band = (f > 25) & (f < 250)
        out.append(f[band][np.argmax(X[band])])
    return np.array(out)


def classify_hits(mono, sr):
    """Onsets split into kick and hat hits by their spectral balance right after the onset."""
    ons = onsets(mono, sr, min_gap=0.06)
    kicks, hats = [], []
    for o in ons:
        s = int(o * sr)
        seg = mono[s:s + int(0.06 * sr)]
        if len(seg) < int(0.06 * sr):
            continue
        X = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.rfftfreq(len(seg), 1 / sr)
        low = X[(f > 30) & (f < 150)].sum()
        high = X[f > 6000].sum()
        mid = X[(f > 150) & (f < 6000)].sum()
        tot = low + mid + high + 1e-20
        if low / tot > 0.6:
            kicks.append(o)
        elif high / tot > 0.35 and low / tot < 0.15:
            hats.append(o)
    return np.array(kicks), np.array(hats)


def _median(rows):
    return np.median(np.array(rows), axis=0) if rows else None


def drum_fingerprint(x, sr, kind, times=None) -> dict:
    """Median fingerprint of kick or hat hits. `times` defaults to auto-detected hits;
    for a single rendered hit pass times=[0]."""
    mono = _mono(np.asarray(x, dtype=np.float64))
    if times is None:
        k, h = classify_hits(mono, sr)
        times = k if kind == "kick" else h
    specs, envs, pitches = [], [], []
    for o in times:
        s = int(o * sr)
        seg = mono[s:s + int(0.6 * sr)]
        if len(seg) < int(0.3 * sr):
            continue
        if kind == "kick":
            e = _band_env(seg, sr, 0, 200, KICK_MS)
            specs.append(_hit_spectrum(seg, sr, 200))
            pitches.append(_kick_pitch(seg, sr))
        else:
            e = _band_env(seg, sr, 4000, min(18000, sr / 2 - 100), HAT_MS, 3)
            specs.append(_hit_spectrum(seg, sr, 60))
        if e.max() < 1e-6:
            continue
        envs.append(20 * np.log10(e / e.max() + 1e-6))
    fp = {"kind": kind, "hits": len(envs), "spectrum": _median(specs), "envelope": _median(envs)}
    if kind == "kick":
        fp["pitch"] = np.nanmedian(np.array(pitches), axis=0) if pitches else None
    return fp


def drum_distance(a, b) -> tuple[float, dict]:
    if a["spectrum"] is None or b["spectrum"] is None:
        return 1e9, {}
    if a["kind"] == "kick":
        sel = DRUM_BANDS[:-1] < 4000
    else:
        sel = DRUM_BANDS[:-1] > 1500
    ds = float(np.sqrt(np.mean((np.maximum(a["spectrum"][sel], -50) - np.maximum(b["spectrum"][sel], -50)) ** 2)))
    de = float(np.sqrt(np.mean((np.maximum(a["envelope"], -50) - np.maximum(b["envelope"], -50)) ** 2)))
    parts = {"spectrum_db": round(ds, 2), "envelope_db": round(de, 2)}
    total = ds + de
    if a["kind"] == "kick" and a.get("pitch") is not None and b.get("pitch") is not None:
        pa, pb = a["pitch"], b["pitch"]
        ok = np.isfinite(pa) & np.isfinite(pb)
        dp = float(np.sqrt(np.mean((12 * np.log2(pa[ok] / pb[ok])) ** 2))) if ok.any() else 24.0
        parts["pitch_semitones"] = round(dp, 2)
        total += dp * 1.5
    return total, parts


class DrumMatcher:
    def __init__(self, voice: str, target: dict, seed: int = 3):
        from .dsp import faust as FZ
        from .dsp.engine import Engine
        self.voice, self.target = voice, target
        self.code = FZ.drum_voice(voice)
        self.ranges = FZ.DRUM_PARAMS[voice]
        self.eng = Engine(96000)
        self.rng = random.Random(seed)
        self.kind = "kick" if voice == "bd" else "hat"
        self.evals = 0

    def render(self, params: dict) -> np.ndarray:
        from .dsp.master import downsample
        a = self.eng.render(self.code, dict(params, vel=1.0, dur=10, seed=0, level=1.0), 0.7, trim=False)
        return downsample(a, 2)

    def score(self, params):
        self.evals += 1
        fp = drum_fingerprint(self.render(params), 48000, self.kind, times=[0.0])
        return drum_distance(fp, self.target)

    def search(self, trials=150, rounds=5, log=print):
        def rand():
            return {k: self.rng.uniform(lo, hi) for k, (d, lo, hi) in self.ranges.items()}
        best = {k: d for k, (d, lo, hi) in self.ranges.items()}
        best_d, best_p = self.score(best)
        for i in range(trials):
            c = rand()
            d, p = self.score(c)
            if d < best_d:
                best, best_d, best_p = c, d, p
        log(f"  random search: {best_d:.2f} {best_p}")
        for r in range(rounds):
            improved = False
            step = 0.25 / (r + 1)
            for k, (d0, lo, hi) in self.ranges.items():
                for sign in (-1, 1):
                    c = dict(best, **{k: min(hi, max(lo, best[k] + sign * step * (hi - lo)))})
                    d, p = self.score(c)
                    if d < best_d:
                        best, best_d, best_p, improved = c, d, p, True
            log(f"  round {r + 1}: {best_d:.2f} {best_p}")
            if not improved and r > 1:
                break
        return best, best_d, best_p
