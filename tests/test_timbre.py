import numpy as np

from pulsar import timbre


def _plucks(sr=48000, f=220.0, decay=0.15, seconds=6.0, gap=0.5):
    t = np.arange(int(sr * seconds)) / sr
    x = np.zeros_like(t)
    for s in np.arange(0.1, seconds - 1, gap):
        m = t >= s
        tt = t[m] - s
        x[m] += np.sign(np.sin(2 * np.pi * f * tt)) * np.exp(-tt / decay) * 0.3
    return np.stack([x, x]), sr


def test_fingerprint_finds_onsets_register_and_decay():
    x, sr = _plucks()
    fp = timbre.fingerprint(x, sr)
    assert 9 <= fp["onsets"] <= 11
    assert 200 < fp["register_hz"][0] < 240
    env = fp["envelope"]
    assert env[0] > 0.9 and env[30] < 0.3  # decays within 300 ms


def test_distance_prefers_the_closer_decay():
    a, sr = _plucks(decay=0.15)
    b, _ = _plucks(decay=0.16)
    c, _ = _plucks(decay=0.6)
    fa, fb, fc = (timbre.fingerprint(v, sr) for v in (a, b, c))
    assert timbre.distance(fa, fb)[0] < timbre.distance(fa, fc)[0]
