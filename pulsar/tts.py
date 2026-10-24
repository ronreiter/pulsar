"""Neural text-to-speech voices, spoken or sung.

Speech comes from Kokoro-82M (Apache-2.0), run locally through ONNX. For
singing, each lyric word is spoken by the model, analysed with the WORLD
vocoder (pitch, spectral envelope, aperiodicity), given the target note's
pitch with a little of its natural inflection plus a delayed vibrato,
time-stretched to the note's length (consonants keep their natural speed, the
vowel stretches), and resynthesized. WORLD keeps the spectral envelope, so
re-pitching does not make the voice sound like a chipmunk.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

import numpy as np

from .errors import PulsarError

MODEL_DIR = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar")) / "models" / "kokoro"
CACHE_DIR = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar")) / "tts"
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
TTS_SR = 24000
FRAME_MS = 5.0

_kokoro = None


def ensure_models(log=None) -> Path:
    """Download the Kokoro model files on first use (about 350 MB)."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        dest = MODEL_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        (log or (lambda m: print(m, file=sys.stderr)))(f"downloading voice model {name} (first use only)…")
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(RELEASE + name) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        except OSError as e:
            raise PulsarError(f"could not download the voice model ({e}); check your connection or put "
                              f"{', '.join(FILES)} in {MODEL_DIR}") from None
        tmp.replace(dest)
    return MODEL_DIR


def kokoro():
    global _kokoro
    if _kokoro is None:
        try:
            from kokoro_onnx import Kokoro
        except ImportError:
            raise PulsarError("voices need the kokoro-onnx package: pip install 'pulsar-music[voice]'") from None
        d = ensure_models()
        _kokoro = Kokoro(str(d / FILES[0]), str(d / FILES[1]))
    return _kokoro


def voices() -> list[str]:
    return list(kokoro().get_voices())


def speak(text: str, voice: str, speed: float = 1.0, lang: str = "en-us") -> np.ndarray:
    """Spoken audio (mono float32 at 24 kHz), cached on disk."""
    key = hashlib.sha1(f"{voice}|{speed:.3f}|{lang}|{text}".encode()).hexdigest()[:20]
    path = CACHE_DIR / f"{key}.npy"
    if path.exists():
        return np.load(path)
    k = kokoro()
    if voice not in k.get_voices():
        from .errors import suggest
        raise PulsarError(f"unknown voice {voice!r}", hint=suggest(voice, k.get_voices()) or
                          "run `pulsar voices` to list them")
    audio, sr = k.create(text, voice=voice, speed=float(speed), lang=lang)
    audio = np.asarray(audio, dtype=np.float32)
    if sr != TTS_SR:
        from scipy.signal import resample_poly
        audio = resample_poly(audio, TTS_SR, sr).astype(np.float32)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(path, audio)
    return audio


def lyric_units(text: str) -> list[str]:
    """Words to sing, one per note. Punctuation is dropped; '-' keeps a hyphenated word together."""
    words = re.findall(r"[A-Za-z0-9'’\-]+", text)
    return [w.strip("-") for w in words if w.strip("-")]


def lyric_lines(text: str) -> list[str]:
    """Lines to speak, one per hit: split on '/' or newlines."""
    return [ln.strip() for ln in re.split(r"[/\n]+", text) if ln.strip()]


def _trim(x: np.ndarray, thresh_db: float = -45) -> np.ndarray:
    if len(x) == 0:
        return x
    env = np.abs(x)
    thr = env.max() * 10 ** (thresh_db / 20)
    idx = np.nonzero(env > thr)[0]
    if len(idx) == 0:
        return x[:1]
    pad = int(0.01 * TTS_SR)
    return x[max(0, idx[0] - pad): idx[-1] + pad]


_world_cache: dict = {}


def _analyse(x: np.ndarray):
    import pyworld as pw
    key = hashlib.sha1(x.tobytes()).hexdigest()
    if key not in _world_cache:
        xd = x.astype(np.float64)
        f0, t = pw.harvest(xd, TTS_SR, f0_floor=60, f0_ceil=900, frame_period=FRAME_MS)
        f0 = pw.stonemask(xd, f0, t, TTS_SR)
        sp = pw.cheaptrick(xd, f0, t, TTS_SR)
        ap = pw.d4c(xd, f0, t, TTS_SR)
        _world_cache[key] = (f0, sp, ap)
    return _world_cache[key]


def sing(word_audio: np.ndarray, midi: float, seconds: float, inflection: float = 0.15, vibrato: float = 0.35,
         vib_rate: float = 5.4, formant: float = 0.0) -> tuple[np.ndarray, float]:
    """Re-pitch and stretch a spoken word onto one note.

    Returns (audio at 24 kHz, lead-in seconds): the lead-in is how long the
    leading consonant lasts, so the caller can start the word that much early
    and land the vowel on the beat."""
    import pyworld as pw
    x = _trim(word_audio)
    f0, sp, ap = _analyse(x)
    n = len(f0)
    voiced = f0 > 0
    if not voiced.any():  # a purely unvoiced word: just stretch it a little
        return x, 0.0
    first_v = int(np.argmax(voiced))
    last_v = n - 1 - int(np.argmax(voiced[::-1]))
    target_frames = max(int(seconds * 1000 / FRAME_MS), last_v + 4)
    # Time map: consonant onset and offset keep their speed; the voiced core stretches.
    core = max(1, last_v - first_v)
    tail = n - last_v
    new_core = max(core, target_frames - first_v - tail)
    src = np.concatenate([
        np.arange(first_v, dtype=np.float64),
        first_v + np.linspace(0, core, new_core, endpoint=False),
        last_v + np.arange(tail, dtype=np.float64),
    ])
    src = np.clip(src, 0, n - 1)
    lo = np.floor(src).astype(int)
    hi = np.minimum(lo + 1, n - 1)
    w = (src - lo)[:, None]
    sp2 = sp[lo] * (1 - w) + sp[hi] * w
    ap2 = ap[lo] * (1 - w) + ap[hi] * w
    v2 = voiced[np.rint(src).astype(int)]
    # Pitch: the note, a little of the spoken contour around its median, and vibrato that fades in.
    target = 440.0 * 2 ** ((midi - 69) / 12)
    orig = f0[np.rint(src).astype(int)]
    med = np.median(f0[voiced])
    dev = np.where(v2 & (orig > 0), np.log2(np.maximum(orig, 1) / med), 0.0)
    tt = np.arange(len(src)) * FRAME_MS / 1000
    vib_env = np.clip((tt - 0.18) / 0.25, 0, 1)
    cents = vibrato * 40 * vib_env * np.sin(2 * np.pi * vib_rate * tt)
    f0_new = np.where(v2, target * 2 ** (inflection * dev + cents / 1200), 0.0)
    if formant:  # shift the spectral envelope (positive = brighter / smaller voice)
        bins = sp2.shape[1]
        idx = np.clip(np.arange(bins) / (2 ** (formant / 12)), 0, bins - 1)
        l2 = np.floor(idx).astype(int)
        h2 = np.minimum(l2 + 1, bins - 1)
        ww = idx - l2
        sp2 = sp2[:, l2] * (1 - ww) + sp2[:, h2] * ww
    y = pw.synthesize(np.ascontiguousarray(f0_new), np.ascontiguousarray(sp2), np.ascontiguousarray(ap2),
                      TTS_SR, FRAME_MS).astype(np.float32)
    lead_in = first_v * FRAME_MS / 1000
    return y, min(lead_in, 0.12)


def natural_pitch(voice: str) -> float:
    """Median speaking pitch (Hz) of a voice, measured once from a test sentence."""
    y = speak("The evening light falls slowly over the quiet city.", voice)
    f0, _, _ = _analyse(_trim(y))
    v = f0[f0 > 0]
    return float(np.median(v)) if len(v) else 200.0


def _word_frames(word_audio: np.ndarray, frames: int):
    """WORLD frames for one word, time-mapped to `frames`: the leading consonant and the
    tail keep their natural speed, the voiced core stretches. Returns (sp, ap, voiced, lead_frames)."""
    x = _trim(word_audio)
    f0, sp, ap = _analyse(x)
    n = len(f0)
    voiced = f0 > 0
    if not voiced.any():
        src = np.linspace(0, n - 1, max(frames, 2))
        idx = np.rint(src).astype(int)
        return sp[idx], ap[idx], voiced[idx], 0
    first_v = int(np.argmax(voiced))
    last_v = n - 1 - int(np.argmax(voiced[::-1]))
    lead = min(first_v, int(0.12 * 1000 / FRAME_MS))
    tail = n - 1 - last_v
    core = max(1, last_v - first_v)
    new_core = max(4, frames + lead - first_v - tail)
    if new_core < core:  # shorter than spoken: compress the core, keep consonants
        new_core = max(4, new_core)
    src = np.concatenate([
        np.arange(first_v, dtype=np.float64),
        first_v + np.linspace(0, core, new_core, endpoint=False),
        last_v + np.arange(tail + 1, dtype=np.float64),
    ])
    src = np.clip(src, 0, n - 1)
    lo = np.floor(src).astype(int)
    hi = np.minimum(lo + 1, n - 1)
    w = (src - lo)[:, None]
    sp2 = np.exp(np.log(sp[lo] + 1e-16) * (1 - w) + np.log(sp[hi] + 1e-16) * w)
    ap2 = ap[lo] * (1 - w) + ap[hi] * w
    v2 = voiced[np.rint(src).astype(int)]
    return sp2, ap2, v2, lead


def sing_phrase(words: list[np.ndarray], notes: list[tuple[float, float, float]], vibrato: float = 0.3,
                vib_rate: float = 5.2, glide: float = 0.07, formant: float = 0.0) -> tuple[np.ndarray, float]:
    """Sing a legato phrase as one continuous performance.

    `notes` are (midi, start_seconds_from_phrase_start, duration). Words are joined frame by frame in
    the WORLD domain (consonants overlap the previous word's tail with a crossfade), the pitch is one
    continuous line with short glides between notes, and the phrase is resynthesized in one pass.
    Returns (audio at 24 kHz, lead-in seconds before the first note)."""
    import pyworld as pw
    fp = FRAME_MS / 1000
    parts = []
    for audio, (midi, start, dur) in zip(words, notes):
        frames = max(8, int(round(dur / fp)))
        sp, ap, v, lead = _word_frames(audio, frames)
        parts.append((int(round(start / fp)) - lead, sp, ap, v, midi, int(round(start / fp)), frames))
    origin = min(p[0] for p in parts)
    total = max(p[0] + len(p[1]) for p in parts) - origin + 1
    bins = parts[0][1].shape[1]
    logsp = np.zeros((total, bins))
    apm = np.ones((total, bins))
    wsum = np.zeros(total)
    voiced = np.zeros(total, bool)
    xf = int(0.03 / fp)  # 30 ms crossfades at word edges
    for (s0, sp, ap, v, *_rest) in parts:
        s = s0 - origin
        n = len(sp)
        w = np.ones(n)
        k = min(xf, n // 3)
        if k > 0:
            w[:k] = np.linspace(0.05, 1, k)
            w[-k:] = np.minimum(w[-k:], np.linspace(1, 0.05, k))
        logsp[s:s + n] += np.log(sp + 1e-16) * w[:, None]
        apm[s:s + n] = np.where(wsum[s:s + n, None] > 0, (apm[s:s + n] * wsum[s:s + n, None] + ap * w[:, None])
                                / (wsum[s:s + n, None] + w[:, None]), ap)
        wsum[s:s + n] += w
        voiced[s:s + n] |= v
    gap = wsum < 1e-6
    wsum[gap] = 1
    sp_all = np.exp(logsp / wsum[:, None])
    sp_all[gap] = 1e-12
    apm[gap] = 1.0
    voiced[gap] = False
    # one continuous pitch line: hold each note, glide into the next just before it starts
    target = np.zeros(total)
    marks = sorted((p[5] - origin, p[4], p[6]) for p in parts)
    for i, (s, midi, frames) in enumerate(marks):
        e = marks[i + 1][0] if i + 1 < len(marks) else total
        target[s:e] = midi
        if i == 0:
            target[:s] = midi
    g = max(1, int(glide / fp))
    for i in range(1, len(marks)):
        s = marks[i][0]
        a, b = marks[i - 1][1], marks[i][1]
        lo = max(0, s - g // 2)
        hi = min(total, s + g // 2 + 1)
        tt = np.linspace(0, 1, hi - lo)
        target[lo:hi] = a + (b - a) * (0.5 - 0.5 * np.cos(np.pi * tt))
    # vibrato only on held notes, fading in after 250 ms
    vib = np.zeros(total)
    t_all = np.arange(total) * fp
    for i, (s, midi, frames) in enumerate(marks):
        e = min(total, s + frames)
        if frames * fp < 0.35:
            continue
        local = (np.arange(e - s)) * fp
        env = np.clip((local - 0.25) / 0.3, 0, 1)
        vib[s:e] = vibrato * 35 * env * np.sin(2 * np.pi * vib_rate * t_all[s:e])
    # Human touches: scoop up into notes that start a phrase or follow a gap, a slow pitch drift,
    # and a loudness swell on held notes that tapers at the end.
    rng = np.random.default_rng(int(sum(m for _, m, _ in marks) * 1000) % (2 ** 32))
    cents = vib.copy()
    for i, (s, midi, frames) in enumerate(marks):
        legato_in = i > 0 and abs(marks[i - 1][1] - midi) > 0 and s - (marks[i - 1][0] + marks[i - 1][2]) < 3
        if not legato_in:
            k = min(int(0.09 / fp), frames)
            cents[s:s + k] += -70 * (1 - np.linspace(0, 1, k)) ** 2
    drift = np.convolve(rng.normal(0, 1, total + 200), np.hanning(120) / np.hanning(120).sum(), "same")[100:100 + total]
    cents += 8 * drift / (np.abs(drift).max() + 1e-9)
    f0 = np.where(voiced, 440.0 * 2 ** ((target - 69) / 12 + cents / 1200), 0.0)
    gain = np.ones(total)
    for s, midi, frames in marks:
        e = min(total, s + frames)
        n = e - s
        if n < 6:
            continue
        x = np.linspace(0, 1, n)
        gain[s:e] *= (0.82 + 0.18 * np.minimum(x * 3, 1)) * np.where(x > 0.85, 1 - (x - 0.85) / 0.15 * 0.5, 1)
    sp_all = sp_all * (gain ** 2)[:, None]
    if formant:
        idx = np.clip(np.arange(bins) / (2 ** (formant / 12)), 0, bins - 1)
        l2 = np.floor(idx).astype(int)
        h2 = np.minimum(l2 + 1, bins - 1)
        ww = idx - l2
        sp_all = sp_all[:, l2] * (1 - ww) + sp_all[:, h2] * ww
    y = pw.synthesize(np.ascontiguousarray(f0), np.ascontiguousarray(sp_all), np.ascontiguousarray(apm),
                      TTS_SR, FRAME_MS).astype(np.float32)
    lead_in = (marks[0][0]) * fp
    return y, lead_in
