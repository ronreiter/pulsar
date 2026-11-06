"""Final loudness and peak control, in numpy.

The limiter detects true peaks on a 4x oversampled copy, looks ahead so gain
reduction is already in place when a peak arrives, ramps in over the
lookahead window and releases exponentially. Loudness is set by measuring
integrated LUFS (ITU-R BS.1770 via pyloudnorm) and iterating gain + limiting.
"""
from __future__ import annotations

import numpy as np
import pyloudnorm as pyln
from scipy.ndimage import maximum_filter1d
from scipy.signal import resample_poly


def true_peak_env(x: np.ndarray, os: int = 4, chunk: int = 1 << 18) -> np.ndarray:
    """Per-sample peak magnitude across channels, measured at `os`x oversampling."""
    n = x.shape[1]
    out = np.empty(n, np.float32)
    pad = 64
    for s in range(0, n, chunk):
        lo, hi = max(0, s - pad), min(n, s + chunk + pad)
        seg = x[:, lo:hi]
        up = np.abs(resample_poly(seg, os, 1, axis=1)).max(axis=0)
        up = up[: (hi - lo) * os].reshape(hi - lo, os).max(axis=1)
        out[s:min(n, s + chunk)] = up[s - lo: s - lo + min(chunk, n - s)]
    return np.maximum(out, np.abs(x).max(axis=0))


def _decay_hold(r: np.ndarray, a: float, block: int = 8192) -> np.ndarray:
    """e[n] = max(r[n], a * e[n-1]) computed blockwise in the log domain."""
    out = np.empty_like(r)
    carry = 0.0
    la = np.log(a)
    for s in range(0, len(r), block):
        x = r[s:s + block]
        n = np.arange(len(x))
        lx = np.log(np.maximum(x, 1e-12)) - n * la
        e = np.exp(np.maximum.accumulate(lx) + n * la)
        e = np.maximum(e, carry * np.exp(la * (n + 1)))
        e[x <= 1e-12] = np.maximum(e[x <= 1e-12], 0)
        out[s:s + block] = e
        carry = float(e[-1])
    return out


def limit(x: np.ndarray, sr: int, ceiling_db: float = -1.0, lookahead: float = 0.004,
          release: float = 0.12) -> tuple[np.ndarray, float]:
    """Brickwall true-peak limiter. Returns (audio, max gain reduction in dB)."""
    ceil = 10 ** (ceiling_db / 20)
    peak = true_peak_env(x)
    need = np.where(peak > ceil, 1 - ceil / np.maximum(peak, 1e-12), 0).astype(np.float64)
    if not need.any():
        return x, 0.0
    L = max(2, int(lookahead * sr))
    padded = np.concatenate([need, np.zeros(L)])
    ahead = maximum_filter1d(padded, size=L, mode="constant")[L // 2: L // 2 + len(need)]
    c = np.concatenate([[0], np.cumsum(ahead)])
    idx = np.arange(len(ahead))
    lo = np.maximum(0, idx - L + 1)
    ramp = (c[idx + 1] - c[lo]) / L
    ramp = np.maximum(ramp, need)  # never let a peak through
    red = _decay_hold(ramp, float(np.exp(-1.0 / (release * sr))))
    g = (1 - red).astype(np.float32)
    return x * g, float(-20 * np.log10(max(g.min(), 1e-6)))


def soft_clip(x: np.ndarray, ceiling: float, os: int = 4, chunk: int = 1 << 18) -> np.ndarray:
    """Oversampled soft clipper that rounds off transients (mostly kick peaks)
    above about half the ceiling, so the limiter after it can work gently."""
    knee = ceiling * 0.5
    span = ceiling * 1.12 - knee
    out = np.empty_like(x)
    n = x.shape[1]
    pad = 256
    for s in range(0, n, chunk):
        lo, hi = max(0, s - pad), min(n, s + chunk + pad)
        up = resample_poly(x[:, lo:hi], os, 1, axis=1)
        a = np.abs(up)
        over = a > knee
        up[over] = np.sign(up[over]) * (knee + span * np.tanh((a[over] - knee) / span))
        down = resample_poly(up, 1, os, axis=1)
        out[:, s:min(n, s + chunk)] = down[:, s - lo: s - lo + min(chunk, n - s)]
    return out


def lufs(x: np.ndarray, sr: int) -> float:
    meter = pyln.Meter(sr)
    try:
        v = meter.integrated_loudness(x.T.astype(np.float64))
    except ValueError:
        return -70.0
    return float(v) if np.isfinite(v) else -70.0


def master(x: np.ndarray, sr: int, target_lufs: float = -10.0, ceiling_db: float = -1.0,
           clip: bool = True) -> tuple[np.ndarray, dict]:
    """Bring a mix to target loudness under a true-peak ceiling."""
    x = x.astype(np.float32)
    x = x - x.mean(axis=1, keepdims=True)
    start = lufs(x, sr)
    if start <= -69:
        return x, {"lufs": start, "gain_db": 0.0, "max_reduction_db": 0.0}
    gain_db = target_lufs - start
    out, red = x, 0.0
    ceil = 10 ** (ceiling_db / 20)
    # A fixed number of iterations (no early exit) keeps the result a smooth
    # function of the input, so tiny upstream differences stay tiny.
    for _ in range(3):
        y = x * 10 ** (gain_db / 20)
        if clip:
            y = soft_clip(y, ceil)
        out, red = limit(y, sr, ceiling_db)
        got = lufs(out, sr)
        gain_db = min(gain_db + (target_lufs - got), 30)
    y = x * 10 ** (gain_db / 20)
    if clip:
        y = soft_clip(y, ceil)
    out, red = limit(y, sr, ceiling_db)
    return out, {"lufs": round(lufs(out, sr), 2), "gain_db": round(gain_db, 2), "max_reduction_db": round(red, 2)}


def downsample(x: np.ndarray, factor: int) -> np.ndarray:
    if factor == 1:
        return x
    return resample_poly(x, 1, factor, axis=1).astype(np.float32)
