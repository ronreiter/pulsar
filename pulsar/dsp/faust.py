"""Faust source for every voice and effect.

Voices are rendered one note at a time: each render starts at sample 0, so
`ba.time` is the time since note-on and the gate is computed inside the DSP
from the note length (sample-exact, no automation needed). Every numeric
parameter is an hslider so it can be set per note or automated.
"""
from __future__ import annotations

PRELUDE = r'''
import("stdfaust.lib");
freq = hslider("freq", 440, 1, 20000, 0.0001);
vel  = hslider("vel", 0.8, 0, 1, 0.0001);
dur  = hslider("dur", 0.25, 0, 3600, 0.00001);
seed = hslider("seed", 0, 0, 1000, 0.0001);
level = hslider("level", 1, 0, 4, 0.0001);
gate = ba.time < dur * ma.SR;
t = ba.time / ma.SR;
hash(x) = ma.frac(sin(x * 12.9898 + 78.233) * 43758.5453);
expdec(t60) = exp(-6.9078 * t / max(t60, 0.0001));
phasor(f, ph0) = (+(f / ma.SR) : ma.frac) ~ _ : +(ph0) : ma.frac;
blep(dt) = \(p).(select2(p < dt, select2(p > 1 - dt, 0, ((p - 1) / dt) : \(x).(x * x + 2 * x + 1)), (p / dt) : \(x).(2 * x - x * x - 1)));
bsaw(f, ph0) = phasor(f, ph0) : \(p).(2 * p - 1 - (p : blep(f / ma.SR)));
bsquare(f, pw, ph0) = phasor(f, ph0) : \(p).(select2(p < pw, -1, 1) + (p : blep(f / ma.SR)) - (ma.frac(p + 1 - pw) : blep(f / ma.SR)));
btri(f, ph0) = phasor(f, ph0) : \(p).(2 * abs(2 * p - 1) - 1);
bsine(f, ph0) = sin(2 * ma.PI * phasor(f, ph0));
softclip(x) = ma.tanh(x);
drivefx(d) = _ <: (_ * (1 + d * 12) : softclip) / softclip(1 + d * 12) * (d > 0.001) + _ * (d <= 0.001);
'''


def _slider(name, default, lo, hi):
    return f'{name} = hslider("{name}", {default}, {lo}, {hi}, 0.00001);'


# --------------------------------------------------------------------------
# Analog (virtual-analog subtractive) voice
# --------------------------------------------------------------------------

ANALOG_PARAMS = {
    # name: (default, lo, hi)
    "osc2_semi": (0, -48, 48), "osc2_fine": (0.07, -1, 1), "osc_mix": (0.5, 0, 1),
    "pw": (0.5, 0.02, 0.98), "sub": (0, 0, 1), "noise": (0, 0, 1),
    "detune": (0.15, 0, 1), "spread": (0.6, 0, 1),
    "cutoff": (0.55, 0, 1), "resonance": (0.2, 0, 1), "env_amount": (2.0, -8, 8),
    "keytrack": (0.5, 0, 1), "vel_filter": (0.4, 0, 1),
    "attack": (0.003, 0, 20), "decay": (0.3, 0.001, 20), "sustain": (0.7, 0, 1), "release": (0.25, 0.001, 30),
    "fattack": (0.002, 0, 20), "fdecay": (0.25, 0.001, 20), "fsustain": (0.2, 0, 1), "frelease": (0.3, 0.001, 30),
    "drive": (0.0, 0, 1), "vel_amp": (0.6, 0, 1), "vibrato": (0.0, 0, 1), "vib_rate": (5.0, 0.01, 20),
    "drift": (0.25, 0, 1), "glide": (0.0, 0, 2), "from_freq": (0, 0, 20000), "accent": (0, 0, 1),
    "pitch_env": (0.0, -48, 48), "pitch_decay": (0.05, 0.001, 5),
    "flfo_rate": (0.0, 0, 50), "flfo_depth": (0.0, 0, 4), "hp": (0.0, 0, 1),
    "rise": (0.0, -60, 60), "sweep": (0.0, -10, 10), "swell": (0.0, 0, 4), "fade": (0.0, 0, 4),
}

WAVES = {"saw", "square", "pulse", "tri", "sine", "none"}
FILTERS = {"ladder", "diode", "korg", "oberheim", "svf", "hp", "bp", "none"}


def _osc(wave, f, ph):
    if wave == "saw":
        return f"bsaw({f}, {ph})"
    if wave in ("square", "pulse"):
        return f"bsquare({f}, {'pw' if wave == 'pulse' else 0.5}, {ph})"
    if wave == "tri":
        return f"btri({f}, {ph})"
    if wave == "sine":
        return f"bsine({f}, {ph})"
    return "0"


def analog_voice(wave1="saw", wave2="saw", unison=1, filter="ladder") -> str:
    if wave1 not in WAVES or wave2 not in WAVES:
        raise ValueError(f"wave must be one of {sorted(WAVES)}")
    if filter not in FILTERS:
        raise ValueError(f"filter must be one of {sorted(FILTERS)}")
    unison = max(1, min(int(unison), 9))
    sliders = "\n".join(_slider(k, *v) for k, v in ANALOG_PARAMS.items())
    # Per-voice frequency: optional glide from a previous note, pitch envelope,
    # vibrato and a small per-note analog drift.
    lines = [PRELUDE, sliders, r'''
gl = (glide > 0.0001) & (from_freq > 0);
fbase = select2(gl, freq, from_freq * pow(freq / max(from_freq, 1), 1 - exp(-t / max(glide, 0.0001) * 4)));
prog = min(t / max(dur, 0.001), 1);
pitchmod = pow(2, (rise * prog + pitch_env * expdec(pitch_decay) + vibrato * 0.5 * os.osc(vib_rate) * min(t * 2, 1)) / 12);
driftc = (hash(seed + 3.1) - 0.5) * drift * 0.012 + drift * 0.002 * os.osc(0.3 + hash(seed) * 0.4);
f0 = fbase * pitchmod * (1 + driftc);
''']
    voices_l, voices_r = [], []
    for u in range(unison):
        off = 0.0 if unison == 1 else (u / (unison - 1) * 2 - 1)
        pan = 0.5 + 0.5 * off  # 0 = left, 1 = right before spread scaling
        ph1 = f"hash(seed + {u * 7 + 1})"
        ph2 = f"hash(seed + {u * 7 + 4})"
        fu = f"f0 * (1 + detune * 0.02 * {off:.6f})"
        o1 = _osc(wave1, fu, ph1)
        o2 = _osc(wave2, f"{fu} * pow(2, (osc2_semi + osc2_fine) / 12)", ph2)
        lines.append(f"u{u} = {o1} * (1 - osc_mix) + {o2} * osc_mix;")
        lines.append(f"pl{u} = sqrt(1 - (0.5 + ({pan:.6f} - 0.5) * spread));")
        lines.append(f"pr{u} = sqrt(0.5 + ({pan:.6f} - 0.5) * spread);")
        voices_l.append(f"u{u} * pl{u}")
        voices_r.append(f"u{u} * pr{u}")
    norm = 1.0 / (unison ** 0.5)
    lines.append(f"oscl = ({' + '.join(voices_l)}) * {norm:.6f};")
    lines.append(f"oscr = ({' + '.join(voices_r)}) * {norm:.6f};")
    lines.append(r'''
subo = bsquare(f0 * 0.5, 0.5, 0) * sub;
nz = no.noise * noise;
fenv = en.adsre(fattack, fdecay, fsustain, frelease, gate);
aenv = en.adsre(attack, decay, sustain, release, gate);
velf = 1 - vel_filter + vel_filter * vel;
acc = 1 + accent * 0.6;
oct = sweep * prog + env_amount * fenv * velf * acc + keytrack * (log(f0 / 261.63) / log(2)) + flfo_depth * os.osc(flfo_rate);
fc = min(20 * pow(1000, cutoff) * pow(2, oct), min(ma.SR * 0.42, 20000)) : max(20);
r2 = resonance * resonance;
nf = log10(fc / 20) / 3;  // VA filters take cutoff on a log scale: 0 = 20 Hz, 1 = 20 kHz
pre(x) = (x + subo + nz) : drivefx(drive);
''')
    filt = {
        "ladder": "ve.moogLadder(nf, 0.707 + 24 * r2)",
        "diode": "ve.diodeLadder(nf, 0.7 + 17 * r2)",
        "korg": "ve.korg35LPF(nf, 0.707 + 9 * r2)",
        "oberheim": "ve.oberheimLPF(nf, 0.5 + 9.5 * r2)",
        "svf": "fi.svf.lp(fc, 0.5 + resonance * 12)",
        "hp": "fi.svf.hp(fc, 0.5 + resonance * 12)",
        "bp": "fi.svf.bp(fc, 0.5 + resonance * 12)",
        "none": "_",
    }[filter]
    lines.append(f"flt = {filt};")
    lines.append(r'''
hpf = fi.highpass(2, 20 + hp * hp * 2000);
velamp = 1 - vel_amp + vel_amp * vel;
shape = select2(swell > 0, 1, pow(prog, swell)) * select2(fade > 0, 1, pow(1 - prog, fade));
amp = aenv * velamp * acc * level * 0.5 * shape;
process = (pre(oscl) : flt : hpf) * amp, (pre(oscr) : flt : hpf) * amp;
''')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# FM voice: two modulator->carrier stacks plus feedback
# --------------------------------------------------------------------------

FM_PARAMS = {
    "ratio1": (1.0, 0.01, 32), "index1": (2.0, 0, 30), "idecay1": (0.6, 0.001, 20), "isustain1": (0.2, 0, 1),
    "ratio2": (14.0, 0.01, 32), "index2": (0.0, 0, 30), "idecay2": (0.05, 0.001, 20),
    "carrier2": (0.0, 0, 1), "cratio2": (1.0, 0.01, 32),
    "attack": (0.002, 0, 20), "decay": (1.5, 0.001, 30), "sustain": (0.3, 0, 1), "release": (0.4, 0.001, 30),
    "vel_index": (0.6, 0, 1), "vel_amp": (0.7, 0, 1), "detune": (0.0, 0, 1), "drift": (0.2, 0, 1),
    "tremolo": (0.0, 0, 1), "trem_rate": (4.0, 0.01, 20), "brightness": (0.8, 0, 1),
}


def fm_voice() -> str:
    sliders = "\n".join(_slider(k, *v) for k, v in FM_PARAMS.items())
    return PRELUDE + sliders + r'''
driftc = (hash(seed + 3.1) - 0.5) * drift * 0.006;
f0 = freq * (1 + driftc);
vi = 1 - vel_index + vel_index * vel;
ienv1 = isustain1 + (1 - isustain1) * expdec(idecay1);
ienv2 = expdec(idecay2);
m1 = bsine(f0 * ratio1, 0) * index1 * ienv1 * vi;
m2 = bsine(f0 * ratio2, 0) * index2 * ienv2 * vi;
fbosc = (+(f0 / ma.SR) : ma.frac) ~ _ ;
c1 = sin(2 * ma.PI * (fbosc + m1 / (2 * ma.PI) + m2 / (2 * ma.PI)));
c2 = sin(2 * ma.PI * phasor(f0 * cratio2 * (1 + detune * 0.003), 0.25) + m1 * 0.5) * carrier2;
aenv = en.adsre(attack, decay, sustain, release, gate);
trem = 1 - tremolo * 0.5 * (1 + os.osc(trem_rate));
velamp = 1 - vel_amp + vel_amp * vel;
tone = fi.lowpass(1, 800 + brightness * 18000 * (0.4 + 0.6 * vel));
sig = (c1 + c2) / (1 + carrier2) * aenv * velamp * trem * level * 0.5 : tone;
process = sig <: _, _;
'''


# --------------------------------------------------------------------------
# Plucked string (Karplus-Strong with a shaped excitation)
# --------------------------------------------------------------------------

PLUCK_PARAMS = {
    "damping": (0.4, 0, 1), "brightness": (0.7, 0, 1), "decay": (3.0, 0.05, 30),
    "release": (0.15, 0.001, 10), "body": (0.3, 0, 1), "pick": (0.25, 0.01, 0.99),
}


def pluck_voice() -> str:
    sliders = "\n".join(_slider(k, *v) for k, v in PLUCK_PARAMS.items())
    return PRELUDE + sliders + r'''
period = ma.SR / freq;
excite = no.noise * (ba.time < period) : fi.lowpass(2, 500 + brightness * vel * 12000);
pickcomb = _ <: _ - de.fdelay(8192, period * pick) * 0.9;
fb = pow(0.001, 1 / (freq * max(decay, 0.01)));
lp(x) = (x * (1 - damping * 0.5) + x' * damping * 0.5);
loop = (+ : de.fdelay4(8192, period - 1.5) : lp : *(fb)) ~ _;
rel = select2(gate, exp(-6.9 * max(t - dur, 0) / release), 1) : si.smoo;
bodyf = _ <: _ + fi.resonbp(220, 1.2, body) + fi.resonbp(480, 1.5, body * 0.6);
sig = excite : pickcomb : loop : bodyf : *(rel * level * (0.4 + 0.6 * vel) * 1.4);
process = sig <: _, _;
'''


# --------------------------------------------------------------------------
# Drum machine voices
# --------------------------------------------------------------------------

DRUM_PARAMS = {
    "bd": {"tune": (48, 20, 200), "punch": (3.2, 0, 6), "pdecay": (0.045, 0.001, 1), "decay": (0.5, 0.01, 4),
           "click": (0.5, 0, 2), "drive": (0.25, 0, 1), "tone": (0.5, 0, 1)},
    "sd": {"tune": (180, 60, 600), "decay": (0.18, 0.01, 2), "snappy": (0.6, 0, 1), "tone": (0.6, 0, 1),
           "ndecay": (0.22, 0.01, 2)},
    "cp": {"tone": (1100, 300, 6000), "decay": (0.28, 0.01, 3), "spread": (0.010, 0.002, 0.04)},
    "hh": {"tune": (1.0, 0.3, 3), "decay": (0.06, 0.005, 2), "tone": (0.6, 0, 1), "noise": (0.4, 0, 1)},
    "oh": {"tune": (1.0, 0.3, 3), "decay": (0.45, 0.01, 4), "tone": (0.55, 0, 1), "noise": (0.4, 0, 1)},
    "cr": {"tune": (1.0, 0.3, 3), "decay": (1.8, 0.1, 8), "tone": (0.5, 0, 1), "noise": (0.6, 0, 1)},
    "rd": {"tune": (1.0, 0.3, 3), "decay": (1.4, 0.1, 8), "tone": (0.4, 0, 1), "noise": (0.25, 0, 1)},
    "lt": {"tune": (90, 30, 400), "punch": (1.2, 0, 4), "decay": (0.35, 0.01, 3)},
    "mt": {"tune": (130, 30, 400), "punch": (1.2, 0, 4), "decay": (0.3, 0.01, 3)},
    "ht": {"tune": (190, 30, 600), "punch": (1.2, 0, 4), "decay": (0.25, 0.01, 3)},
    "rim": {"tune": (1700, 300, 5000), "decay": (0.035, 0.005, 1)},
    "cb": {"tune": (1.0, 0.3, 3), "decay": (0.3, 0.01, 3)},
    "sh": {"tone": (6500, 1000, 15000), "decay": (0.07, 0.01, 1), "attack": (0.012, 0.0005, 0.1)},
    "pc": {"tune": (420, 50, 4000), "punch": (1.5, 0, 6), "decay": (0.12, 0.01, 2)},
}

METAL = "205.3, 304.4, 369.6, 522.7, 540.0, 800.0"

_DRUM_BODY = {
    "bd": r'''
f = tune * pow(2, punch * expdec(pdecay));
body = sin(2 * ma.PI * phasor(f, 0)) * expdec(decay);
clk = (no.noise : fi.highpass(2, 1200) : fi.lowpass(2, 3000 + tone * 9000)) * expdec(0.006) * click * 0.6
    + sin(2 * ma.PI * phasor(f * 6, 0)) * expdec(0.004) * click * 0.4;
sig = (body + clk) : drivefx(drive) : fi.lowpass(2, 3000 + tone * 15000);
''',
    "sd": r'''
f = tune * pow(2, 0.6 * expdec(0.02));
body = (sin(2 * ma.PI * phasor(f, 0)) + 0.6 * sin(2 * ma.PI * phasor(f * 1.84, 0))) * expdec(decay) * (1 - snappy * 0.6);
nz = no.noise : fi.highpass(2, 1200) : fi.peak_eq(4, 5000 + tone * 3000, 2000) : fi.lowpass(2, 6000 + tone * 9000);
sig = body * 0.8 + nz * expdec(ndecay) * snappy * 0.7 : drivefx(0.15);
''',
    "cp": r'''
burst(o) = (t >= o) * exp(-6.9 * max(t - o, 0) / 0.012);
env = max(max(burst(0), burst(spread)), max(burst(spread * 2), burst(spread * 3) * 0.9))
    + (t >= spread * 3) * exp(-6.9 * max(t - spread * 3, 0) / decay) * 0.6;
sig = no.noise : fi.resonbp(tone, 1.6, 1) : fi.highpass(2, 500) : *(env * 1.5) : drivefx(0.2);
''',
    "hh": r'''
metal = par(i, 6, bsquare(ba.take(i + 1, (METAL)) * tune * 1.0, 0.5, hash(i + seed))) :> _ / 6;
mix = metal * (1 - noise) + no.noise * noise;
// A broad presence lift instead of a narrow 10 kHz resonance, and a softer
// top, so hats sit in the mix like the analog originals rather than fizz.
sig = mix : fi.highpass(2, 4200 + tone * 2400) : fi.highpass(1, 2500) : fi.peak_eq(2.5, 7500, 5000)
    : fi.lowpass(2, 9500 + tone * 5000) : *(expdec(decay) * 1.5);
''',
    "oh": None,  # same as hh with a longer default decay
    "cr": r'''
metal = par(i, 6, bsquare(ba.take(i + 1, (METAL)) * tune * 1.7, 0.5, hash(i + seed))) :> _ / 6;
mix = metal * (1 - noise) + no.noise * noise;
sig = mix : fi.highpass(2, 3200 + tone * 2400) : fi.lowpass(2, 9000 + tone * 5000)
    : *((expdec(decay) * 0.8 + expdec(0.05) * 0.4) * 0.9);
''',
    "rd": r'''
metal = par(i, 6, bsquare(ba.take(i + 1, (METAL)) * tune * 2.2, 0.5, hash(i + seed))) :> _ / 6;
bell = (sin(2 * ma.PI * phasor(3200 * tune, 0)) + sin(2 * ma.PI * phasor(5100 * tune, 0)) * 0.5) * expdec(0.4) * 0.25;
mix = metal * (1 - noise) + no.noise * noise;
sig = (mix : fi.highpass(2, 4000 + tone * 2000) : fi.lowpass(2, 9500)) * expdec(decay) * 0.6 + bell;
''',
    "lt": r'''
f = tune * pow(2, punch * expdec(0.06));
sig = (sin(2 * ma.PI * phasor(f, 0)) * expdec(decay) + (no.noise : fi.lowpass(2, 2500)) * expdec(0.01) * 0.2) : drivefx(0.2);
''',
    "rim": r'''
sig = (sin(2 * ma.PI * phasor(tune, 0)) * 0.7 + (no.noise : fi.resonbp(tune * 1.4, 3, 1)) * 0.6
    + sin(2 * ma.PI * phasor(tune * 0.32, 0)) * 0.5) * expdec(decay) : fi.highpass(2, 300);
''',
    "cb": r'''
sig = (bsquare(540 * tune, 0.5, 0) + bsquare(800 * tune, 0.5, 0.3)) * 0.5 : fi.resonbp(2600 * tune, 1.5, 1)
    : fi.highpass(2, 400) : *((expdec(decay) * 0.8 + expdec(0.015) * 0.5) * 0.9);
''',
    "sh": r'''
env = min(t / attack, 1) * exp(-6.9 * max(t - attack, 0) / decay);
sig = no.noise : fi.resonbp(tone, 1.2, 1) : fi.highpass(2, 3000) : *(env * 0.9);
''',
    "pc": r'''
f = tune * pow(2, punch * expdec(0.015));
sig = sin(2 * ma.PI * phasor(f, 0)) * expdec(decay) : drivefx(0.1);
''',
}
_DRUM_BODY["oh"] = _DRUM_BODY["hh"]
_DRUM_BODY["mt"] = _DRUM_BODY["lt"]
_DRUM_BODY["ht"] = _DRUM_BODY["lt"]

DRUM_NAMES = sorted(DRUM_PARAMS)


def drum_voice(name: str) -> str:
    params = DRUM_PARAMS[name]
    sliders = "\n".join(_slider(k, *v) for k, v in params.items())
    body = _DRUM_BODY[name].replace("(METAL)", f"({METAL})")
    # `dur` acts as a choke: when it runs out the voice fades within ~5 ms.
    return PRELUDE + sliders + body + r'''
choke = gate : si.smooth(ba.tau2pole(0.002));
declick = min(ba.time / (0.0003 * ma.SR), 1);
process = sig * choke * declick * (0.25 + 0.75 * vel) * level <: _, _;
'''
