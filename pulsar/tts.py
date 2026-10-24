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
