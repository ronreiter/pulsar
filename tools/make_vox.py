"""Synthesize original sung-vowel samples (soprano, FOF formant synthesis) for the sampler.

Usage: python tools/make_vox.py [output_dir]   (default: ./samples)
"""
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pulsar.dsp.engine import Engine  # noqa: E402

DSP = r'''
import("stdfaust.lib");
freq = hslider("freq", 440, 50, 2000, 0.01);
vowel = hslider("vowel", 0, 0, 4, 1);
t = ba.time / ma.SR;
vib = 1 + 0.012 * os.osc(5.2) * min(t / 0.6, 1) + 0.002 * no.lfnoise(3);
env = min(t / 0.18, 1) * select2(t > 2.6, 1, max(0, 1 - (t - 2.6) / 0.5));
breath = no.noise * 0.015 * env : fi.bandpass(2, 2500, 7000);
voice = pm.SFFormantModelFofSmooth(3, vowel, freq * vib, 1) * env * 0.5 + breath;
process = voice : fi.highpass(2, 120) <: _, _;
'''


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "samples"
    out.mkdir(parents=True, exist_ok=True)
    eng = Engine(48000)
    for name, vowel in (("aah", 0), ("ooh", 4)):
        a = eng.render(DSP, {"freq": 440.0, "vowel": vowel}, 3.2, trim=False)
        a = a / (np.abs(a).max() + 1e-9) * 0.7
        sf.write(out / f"vox_{name}_a4.wav", a.T, 48000, subtype="PCM_24")
        print("wrote", out / f"vox_{name}_a4.wav")


if __name__ == "__main__":
    main()
