"""Stereo effects as Faust programs. Each takes two inputs and gives two outputs.

`EFFECTS[name]` = (faust source, {param: (default, lo, hi)}, tail seconds fn).
Time-like parameters that the language expresses in note values (1/8, 3/16)
are converted to seconds by the caller before they reach the DSP.
"""
from __future__ import annotations

# Parameter smoothing that starts at the parameter's value instead of 0, so
# filters never see a 0 Hz cutoff on the first samples.
HEAD = r'''import("stdfaust.lib");
smstep(y, x) = select2(ba.time < 1, y + (x - y) * (1 - exp(-1 / (0.005 * ma.SR))), x);
smo = smstep ~ _;
'''


def _sliders(params):
    return "\n".join(f'{k} = hslider("{k}", {d}, {lo}, {hi}, 0.00001) : smo;' for k, (d, lo, hi) in params.items())


def _fx(params, body, tail=lambda p: 0.0, smooth=True):
    return (HEAD + (_sliders(params) if smooth else _sliders(params).replace(" : smo", "")) + "\n" + body, params, tail)


EFFECTS = {
    "reverb": _fx(
        {"size": (0.6, 0, 1), "decay": (2.5, 0.2, 20), "damp": (0.4, 0, 1), "predelay": (0.02, 0, 0.25),
         "mix": (0.25, 0, 1), "lowcut": (200, 20, 2000), "width": (1.0, 0, 1)},
        r'''
pd = predelay * ma.SR;
wetl(x) = x : fi.highpass(2, lowcut);
rev = (de.fdelay(24000, pd) : wetl), (de.fdelay(24000, pd) : wetl)
    : re.zita_rev1_stereo(0, 180 + size * 300, 2500 + (1 - damp) * 13000, decay * (1.4 - damp * 0.6), decay, 96000);
ms(l, r) = (l + r) * 0.5 + (l - r) * 0.5 * width, (l + r) * 0.5 - (l - r) * 0.5 * width;
process = _, _ <: (rev : ms), _, _ : (_, _, _, _) : ro.interleave(2, 2) : (_ * mix + _ * (1 - mix)), (_ * mix + _ * (1 - mix));
''', tail=lambda p: p.get("decay", 2.5) * 1.5 + 0.5),

    "delay": _fx(
        {"time": (0.375, 0.001, 4), "feedback": (0.4, 0, 0.95), "mix": (0.25, 0, 1), "tone": (0.5, 0, 1),
         "pingpong": (1, 0, 1), "spread": (0.0, 0, 1)},
        r'''
dt = time * ma.SR;
lp = fi.lowpass(1, 800 + tone * tone * 16000) : fi.highpass(1, 120);
// Ping-pong: the left echo feeds the right line and vice versa.
loop(inl, inr) = (routing ~ (_, _)) with {
  routing(fl, fr) = (inl * 1 + fl * feedback * (1 - pingpong) + fr * feedback * pingpong : de.fdelay(4 * 96000, dt) : lp),
                    (inr * (1 - pingpong) + fr * feedback * (1 - pingpong) + fl * feedback * pingpong : de.fdelay(4 * 96000, dt * (1 + spread * 0.03)) : lp);
};
process = _, _ <: (loop, _, _) : ro.interleave(2, 2) : (_ * mix + _ * (1 - mix)), (_ * mix + _ * (1 - mix));
''', tail=lambda p: p.get("time", 0.375) * min(30, 1 + 6.9 / max(0.01, -__import__("math").log(max(p.get("feedback", 0.4), 0.01)))) + 0.2),

    "chorus": _fx(
        {"rate": (0.6, 0.01, 10), "depth": (0.4, 0, 1), "mix": (0.5, 0, 1)},
        r'''
cho(ph) = _ <: _, de.fdelay(4096, (0.012 + 0.006 * depth * (1 + os.oscp(rate, ph))) * ma.SR) : (_ * (1 - mix * 0.5)), (_ * mix) :> _;
process = cho(0), cho(ma.PI / 2);
'''),

    "drive": _fx(
        {"amount": (0.3, 0, 1), "tone": (0.7, 0, 1), "mix": (1.0, 0, 1), "out": (1.0, 0, 2)},
        r'''
sat(x) = ma.tanh(x * (1 + amount * 15)) / ma.tanh(1 + amount * 15) * (1 + amount * 0.5);
ch = _ <: (sat : fi.lowpass(1, 1500 + tone * tone * 18000)) * mix + _ * (1 - mix) : *(out);
process = ch, ch;
'''),

    "crush": _fx(
        {"bits": (8, 1, 24), "rate": (0.25, 0.01, 1), "mix": (1.0, 0, 1)},
        r'''
q(x) = floor(x * pow(2, bits - 1) + 0.5) / pow(2, bits - 1);
hold = ba.sAndH(ba.time % max(1, int(1 / rate)) == 0);
ch = _ <: (q : hold) * mix + _ * (1 - mix);
process = ch, ch;
''', smooth=False),

    "filter": _fx(
        {"cutoff": (0.7, 0, 1), "resonance": (0.2, 0, 1), "mode": (0, 0, 2)},
        r'''
fc = 20 * pow(1000, cutoff);
q = 0.6 + resonance * resonance * 10;
f = _ <: fi.svf.lp(fc, q), fi.svf.hp(fc, q), fi.svf.bp(fc, q) : ba.selectn(3, int(mode + 0.5));
process = f, f;
'''),

    "lp": _fx({"freq": (8000, 20, 20000), "q": (0.707, 0.3, 10)},
              "f = fi.svf.lp(freq, q);\nprocess = f, f;\n"),
    "hp": _fx({"freq": (120, 20, 20000), "q": (0.707, 0.3, 10)},
              "f = fi.svf.hp(freq, q);\nprocess = f, f;\n"),

    "eq": _fx(
        {"low": (0, -24, 24), "low_freq": (120, 20, 1000), "mid": (0, -24, 24), "mid_freq": (1000, 100, 10000),
         "mid_q": (1.0, 0.1, 10), "high": (0, -24, 24), "high_freq": (8000, 1000, 20000)},
        r'''
f = fi.low_shelf(low, low_freq) : fi.peak_eq_cq(mid, mid_freq, mid_q) : fi.high_shelf(high, high_freq);
process = f, f;
'''),

    "comp": _fx(
        {"threshold": (-18, -60, 0), "ratio": (4, 1, 20), "attack": (0.005, 0.0001, 0.5),
         "release": (0.12, 0.005, 2), "makeup": (0, 0, 24), "knee": (6, 0, 24), "mix": (1, 0, 1)},
        r'''
c = co.FFcompressor_N_chan(1 - 1 / ratio, threshold, attack, release, knee, 0, 1, _, 2);
process = _, _ <: (c : (_ * ba.db2linear(makeup)), (_ * ba.db2linear(makeup))), _, _
    : ro.interleave(2, 2) : (_ * mix + _ * (1 - mix)), (_ * mix + _ * (1 - mix));
''', smooth=False),

    "width": _fx({"amount": (1.3, 0, 2), "bass_mono": (120, 0, 500)},
                 r'''
process(l, r) = m + s, m - s with {
  m = (l + r) * 0.5;
  s = (l - r) * 0.5 * amount : fi.highpass(2, max(bass_mono, 1));
};
'''),

    "phaser": _fx({"rate": (0.3, 0.01, 10), "depth": (0.7, 0, 1), "feedback": (0.4, 0, 0.95)},
                  r'''
process = pf.phaser2_stereo(4, 0, 1000, 400, 1.5, 1500 + depth * 4000, rate, depth, feedback, 0);
'''),

    "tremolo": _fx({"rate": (4, 0.01, 40), "depth": (0.5, 0, 1), "stereo": (0, 0, 1)},
                   r'''
g(ph) = 1 - depth * 0.5 * (1 + os.oscp(rate, ph));
process = _ * g(0), _ * g(stereo * ma.PI);
'''),

    # One broad band per report band, for matching a reference's tonal balance.
    "tone": _fx(
        {"sub": (0, -18, 18), "bass": (0, -18, 18), "low_mid": (0, -18, 18), "mid": (0, -18, 18),
         "presence": (0, -18, 18), "high": (0, -18, 18), "air": (0, -18, 18)},
        r'''
f = fi.low_shelf(sub, 50) : fi.peak_eq_cq(bass, 125, 0.9) : fi.peak_eq_cq(low_mid, 450, 0.9)
  : fi.peak_eq_cq(mid, 1400, 0.9) : fi.peak_eq_cq(presence, 3900, 0.9) : fi.peak_eq_cq(high, 8500, 0.9)
  : fi.high_shelf(air, 13000);
process = f, f;
'''),

    "gain": _fx({"db": (0, -60, 24)}, "process = _ * ba.db2linear(db), _ * ba.db2linear(db);\n"),

    "pan": _fx({"pos": (0, -1, 1)}, r'''
process(l, r) = l * sqrt(1 - p) * 1.4142, r * sqrt(p) * 1.4142 with { p = pos * 0.5 + 0.5; };
'''),
}

# The master bus glue: gentle program-dependent compression plus a soft
# low-end tightening. The final loudness and peak control happen in numpy
# (see master.py) where we can look ahead and oversample.
MASTER = HEAD + r'''
glue = hslider("glue", 0.4, 0, 1, 0.0001);
low = hslider("low", 0, -12, 12, 0.0001);
high = hslider("high", 0, -12, 12, 0.0001);
air = hslider("air", 0, -12, 12, 0.0001);
c = co.FFcompressor_N_chan(1 - 1 / (1 + glue * 2), -20 + (1 - glue) * 8, 0.02, 0.25, 8, 0, 1, _, 2);
eqc = fi.highpass(2, 25) : fi.low_shelf(low, 100) : fi.high_shelf(high, 6000) : fi.high_shelf(air, 14000);
process = (eqc, eqc) : c : (_ * ba.db2linear(glue * 3)), (_ * ba.db2linear(glue * 3));
'''

FX_ALIASES = {"dist": "drive", "distortion": "drive", "saturate": "drive", "bitcrush": "crush",
              "lofi": "crush", "compressor": "comp", "stereo": "width", "highpass": "hp", "lowpass": "lp"}
