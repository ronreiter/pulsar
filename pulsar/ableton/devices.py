"""Ableton Live's audio devices, emulated from their stored parameters (for importing Live sets).

Each device becomes a Faust program (compiled to native code, sample-accurate automation) or a
numpy routine, driven by the exact values the set stores: EQ Eight's bands, Compressor's
threshold / ratio / knee / attack / release and its filtered sidechain, Utility's gain and width,
Saturator's curve and drive, Delay's synced times and filtered feedback, Reverb's decay and
damping, Auto Filter, Multiband Dynamics, Limiter... Ableton's own algorithms are proprietary,
so these are close emulations built from the documented behaviour, not bit-exact copies.

Values arrive as {param: number or per-sample array}. Gains in the set are linear amplitudes
unless named in dB; frequencies in Hz; times in ms.
"""
from __future__ import annotations

import math

import numpy as np

SR = 48000

# ---- helpers ---------------------------------------------------------------------------------


def db(x):
    return 20 * np.log10(np.maximum(np.abs(x), 1e-12))


def lin(d):
    return 10 ** (np.asarray(d, dtype=np.float64) / 20)


def is_array(v) -> bool:
    return isinstance(v, np.ndarray) and v.ndim == 1 and v.size > 1


def const(v):
    """The value at the start, for parameters a device only reads once."""
    return float(v[0]) if is_array(v) else float(v)


def sync_seconds(sixteenths: float, tempo: float) -> float:
    return sixteenths * 60.0 / tempo / 4


# Ableton Delay's synced choices, in sixteenths (SyncedSixteenth index 0..7).
DELAY_SIXTEENTHS = [1, 2, 3, 4, 5, 6, 8, 16]
# Echo's synced divisions: Delay_SyncedDivision -6..0 is 1/64 .. 1 bar (powers of two), and
# Delay_SyncedSixteenth the multiplier within that division.
ECHO_DIVISIONS = {-6: 1 / 64, -5: 1 / 32, -4: 1 / 16, -3: 1 / 8, -2: 1 / 4, -1: 1 / 2, 0: 1.0}


# ---- Faust programs (stereo in, stereo out; a sidechain adds two more inputs) ---------------

BIQUAD = r"""
// RBJ cookbook biquads; mode: 0 LC48, 1 LC12, 2 low shelf, 3 bell, 4 notch, 5 high shelf, 6 HC12, 7 HC48
w0(f) = 2*ma.PI*min(f, ma.SR*0.49)/ma.SR;
alpha(f,q) = sin(w0(f))/(2*max(q,0.05));
A(g) = pow(10, g/40);
hp(f,q) = fi.tf2(b0/a0,b1/a0,b2/a0,a1/a0,a2/a0) with { c=cos(w0(f)); al=alpha(f,q);
  b0=(1+c)/2; b1=0-(1+c); b2=(1+c)/2; a0=1+al; a1=-2*c; a2=1-al; };
lp(f,q) = fi.tf2(b0/a0,b1/a0,b2/a0,a1/a0,a2/a0) with { c=cos(w0(f)); al=alpha(f,q);
  b0=(1-c)/2; b1=1-c; b2=(1-c)/2; a0=1+al; a1=-2*c; a2=1-al; };
bell(f,g,q) = fi.tf2(b0/a0,b1/a0,b2/a0,a1/a0,a2/a0) with { c=cos(w0(f)); al=alpha(f,q); a=A(g);
  b0=1+al*a; b1=-2*c; b2=1-al*a; a0=1+al/a; a1=-2*c; a2=1-al/a; };
notch(f,q) = fi.tf2(b0/a0,b1/a0,b2/a0,a1/a0,a2/a0) with { c=cos(w0(f)); al=alpha(f,q);
  b0=1; b1=-2*c; b2=1; a0=1+al; a1=-2*c; a2=1-al; };
lshelf(f,g,q) = fi.tf2(b0/a0,b1/a0,b2/a0,a1/a0,a2/a0) with { c=cos(w0(f)); a=A(g); al=sin(w0(f))/2*sqrt((a+1/a)*(1/max(q,0.05)-1)+2);
  b0=a*((a+1)-(a-1)*c+2*sqrt(a)*al); b1=2*a*((a-1)-(a+1)*c); b2=a*((a+1)-(a-1)*c-2*sqrt(a)*al);
  a0=(a+1)+(a-1)*c+2*sqrt(a)*al; a1=-2*((a-1)+(a+1)*c); a2=(a+1)+(a-1)*c-2*sqrt(a)*al; };
hshelf(f,g,q) = fi.tf2(b0/a0,b1/a0,b2/a0,a1/a0,a2/a0) with { c=cos(w0(f)); a=A(g); al=sin(w0(f))/2*sqrt((a+1/a)*(1/max(q,0.05)-1)+2);
  b0=a*((a+1)+(a-1)*c+2*sqrt(a)*al); b1=-2*a*((a-1)+(a+1)*c); b2=a*((a+1)+(a-1)*c-2*sqrt(a)*al);
  a0=(a+1)-(a-1)*c+2*sqrt(a)*al; a1=2*((a-1)-(a+1)*c); a2=(a+1)-(a-1)*c-2*sqrt(a)*al; };
// Butterworth stage Qs for the 48 dB/oct (8th-order) cuts
lc48(f,q) = hp(f,0.5098*q/0.7071) : hp(f,0.6013*q/0.7071) : hp(f,0.9000*q/0.7071) : hp(f,2.5629*q/0.7071);
hc48(f,q) = lp(f,0.5098*q/0.7071) : lp(f,0.6013*q/0.7071) : lp(f,0.9000*q/0.7071) : lp(f,2.5629*q/0.7071);
band(m,f,g,q) = _ <: (lc48(f,q), hp(f,q), lshelf(f,g,q), bell(f,g,q), notch(f,q), hshelf(f,g,q), lp(f,q), hc48(f,q)) : ba.selectn(8, m);
"""


def eq8_code(bands: list[dict]) -> str:
    """A Faust EQ Eight: only the active bands, each with its own (automatable) sliders."""
    parts = []
    for i, b in enumerate(bands):
        parts.append(f'eqband{i} = band({int(b["mode"])}, hslider("f{i}", 1000, 10, 22000, 0.01), '
                     f'hslider("g{i}", 0, -30, 30, 0.01), hslider("q{i}", 0.71, 0.05, 20, 0.001));')
    chain = " : ".join(f"eqband{i}" for i in range(len(bands))) or "_"
    return ('import("stdfaust.lib");\n' + BIQUAD + "\n".join(parts) +
            f'\nproc = {chain} : *(ba.db2linear(hslider("out", 0, -60, 30, 0.01)));\nprocess = proc, proc;\n')


UTILITY = r"""
import("stdfaust.lib");
gain = hslider("gain", 1, 0, 16, 0.0001);
width = hslider("width", 1, 0, 4, 0.0001);
mono = hslider("mono", 0, 0, 1, 1);
bassmono = hslider("bassmono", 0, 0, 1, 1);
bassf = hslider("bassf", 120, 20, 1000, 0.1);
bal = hslider("bal", 0, -1, 1, 0.0001);
invl = hslider("invl", 0, 0, 1, 1);
invr = hslider("invr", 0, 0, 1, 1);
chmode = hslider("chmode", 1, 0, 3, 1);
mute = hslider("mute", 0, 0, 1, 1);
lr4lp(f) = fi.lowpass(2,f) : fi.lowpass(2,f);
lr4hp(f) = fi.highpass(2,f) : fi.highpass(2,f);
pick(l, r) = ba.selectn(4, chmode, l, l, r, r), ba.selectn(4, chmode, l, r, r, l);  // left, stereo, right, swap
widen(l, r) = m + s*w, m - s*w with { m = (l+r)/2; s = (l-r)/2; w = select2(mono, width, 0); };
bmono(l, r) = select2(bassmono, l, low + lr4hp(bassf, l)), select2(bassmono, r, low + lr4hp(bassf, r))
  with { low = (lr4lp(bassf, l) + lr4lp(bassf, r))/2; };
balance(l, r) = l*min(1, 1-bal), r*min(1, 1+bal);
process(l, r) = (l*(1-2*invl), r*(1-2*invr)) : pick : widen : bmono : balance : (*(gain*(1-mute)), *(gain*(1-mute)));
"""

SATURATOR = r"""
import("stdfaust.lib");
type = hslider("type", 0, 0, 7, 1);
drive = ba.db2linear(hslider("drive", 0, -36, 36, 0.01));
out = ba.db2linear(hslider("out", 0, -36, 36, 0.01));
mix = hslider("mix", 1, 0, 1, 0.0001);
analog(x) = ma.tanh(x);
softsine(x) = select2(abs(x) < 1, ma.signum(x), sin(x*ma.PI/2));
medium(x) = x / (1 + abs(x));
hard(x) = max(-1, min(1, x*1.5 - 0.5*x*x*x));
fold(x) = sin(x*ma.PI/2);
digital(x) = max(-1, min(1, x));
shaper(x) = ma.tanh(x*1.3);
bass(x) = select2(x > 0, ma.tanh(x*0.7), ma.tanh(x));
curve(x) = ba.selectn(8, type, analog(x), softsine(x), medium(x), hard(x), fold(x), digital(x), shaper(x), bass(x));
sat(x) = x*(1-mix) + curve(x*drive)*out*mix;
process = sat, sat;
"""

COMPRESSOR = r"""
import("stdfaust.lib");
// Feed-forward compressor; inputs: L, R, sidechain L, sidechain R
thr = hslider("thr", 0, -80, 6, 0.01);
ratio = hslider("ratio", 4, 1, 1000, 0.001);
knee = hslider("knee", 0, 0, 24, 0.01);
att = hslider("att", 1, 0.01, 1000, 0.01) / 1000;
rel = hslider("rel", 50, 1, 5000, 0.1) / 1000;
makeup = hslider("makeup", 0, -36, 36, 0.01);
mix = hslider("mix", 1, 0, 1, 0.0001);
rms = hslider("rms", 0, 0, 1, 1);
ext = hslider("ext", 0, 0, 1, 1);
scmode = hslider("scmode", -1, -1, 7, 1);
scf = hslider("scf", 100, 10, 22000, 0.01);
scg = hslider("scg", 0, -30, 30, 0.01);
scq = hslider("scq", 0.71, 0.05, 20, 0.001);
""" + BIQUAD + r"""
scfilter = _ <: (_, band(max(scmode, 0), scf, scg, scq)) : select2(scmode >= 0);
level(x) = select2(rms, abs(x), sqrt(max(fi.lowpass(1, 30, x*x), 0)));
gc(l) = select2(l > thr + knee/2, select2(l > thr - knee/2, 0, (1/ratio - 1)*pow(l - thr + knee/2, 2)/(2*max(knee, 0.001))), (1/ratio - 1)*(l - thr));
smooth = si.onePoleSwitching(rel, att);
process(l, r, sl, sr) = l*g*(mix) + l*(1-mix), r*g*(mix) + r*(1-mix) with {
  key = select2(ext, max(abs(l), abs(r)), max(abs(sl), abs(sr))) : scfilter;
  det = level(key) : ba.linear2db;
  g = ba.db2linear((gc(det) : smooth) + makeup);
};
"""

LIMITER = r"""
import("stdfaust.lib");
gain = ba.db2linear(hslider("gain", 0, -24, 36, 0.01));
ceiling = ba.db2linear(hslider("ceil", -0.3, -24, 0, 0.01));
rel = hslider("rel", 300, 1, 3000, 0.1) / 1000;
look = 48;  // ~1 ms lookahead
pk(l, r) = max(abs(l), abs(r)) * gain;
gr(p) = min(1, ceiling / max(p, 1e-9));
env = ba.slidingMin(look, 64) : si.onePoleSwitching(rel, 0.0);
process(l, r) = (l*gain : @(look)) * g, (r*gain : @(look)) * g with { g = gr(pk(l, r)) : env; };
"""

DELAY = r"""
import("stdfaust.lib");
tl = hslider("tl", 0.25, 0, 4, 0.000001) * ma.SR;
tr = hslider("tr", 0.25, 0, 4, 0.000001) * ma.SR;
fb = hslider("fb", 0, 0, 1.2, 0.0001);
mix = hslider("mix", 1, 0, 1, 0.0001);
pingpong = hslider("pingpong", 0, 0, 1, 1);
filt = hslider("filt", 0, 0, 1, 1);
ff = hslider("ff", 1000, 20, 20000, 0.1);
bw = hslider("bw", 8, 0.5, 9, 0.01);
lo = ff / pow(2, bw/2);
hi = min(ff * pow(2, bw/2), ma.SR*0.45);
bp = _ <: (_, (fi.highpass(1, lo) : fi.lowpass(1, hi))) : select2(filt);
dl = de.fdelay(4*ma.SR, tl);
dr = de.fdelay(4*ma.SR, tr);
// straight: each side feeds itself; ping-pong: the input goes left, and the sides feed each other
loop = (route(4, 4, (1,1),(3,2),(2,3),(4,4)) : (+, +) : (bp : dl), (bp : dr)) ~ (fbmap);  // (fb L + in L), (fb R + in R)
fbmap(a, b) = select2(pingpong, a*fb, b*fb), select2(pingpong, b*fb, a*fb);
process(l, r) = (wl, wr) : loop : (*(mix), *(mix)) : (+(l*(1-mix)), +(r*(1-mix))) with {
  wl = select2(pingpong, l, (l + r)/2);
  wr = select2(pingpong, r, 0);
};
"""

REVERB = r"""
import("stdfaust.lib");
pre = hslider("pre", 10, 0, 500, 0.01) / 1000 * ma.SR;
t60 = hslider("t60", 2, 0.1, 60, 0.001);
damp = hslider("damp", 6000, 500, 20000, 1);
lowcut = hslider("lowcut", 30, 10, 2000, 0.1);
highcut = hslider("highcut", 20000, 500, 22000, 1);
wet = hslider("wet", 0.3, 0, 1, 0.0001);
level = hslider("level", 1, 0, 4, 0.0001);
width = hslider("width", 1, 0, 1, 0.0001);
inf(x) = x : de.delay(ma.SR, pre) : fi.highpass(2, lowcut) : fi.lowpass(2, highcut);
rev = re.zita_rev1_stereo(0, 200, damp, t60*0.6, t60, ma.SR);
w(l, r) = m + s*width, m - s*width with { m = (l+r)/2; s = (l-r)/2; };
process(l, r) = (inf(l), inf(r)) : rev : w : (*(wet*level), *(wet*level)) : (+(l*(1-wet)), +(r*(1-wet)));
"""

AUTOFILTER = r"""
import("stdfaust.lib");
ftype = hslider("ftype", 0, 0, 4, 1);
cut = hslider("cut", 100, 10, 140, 0.0001);
res = hslider("res", 0.1, 0, 1.25, 0.0001);
slope24 = hslider("slope24", 0, 0, 1, 1);
lfoamt = hslider("lfoamt", 0, 0, 1, 0.0001);
lforate = hslider("lforate", 1, 0.01, 40, 0.0001);
mix = hslider("mix", 1, 0, 1, 0.0001);
drivedb = hslider("drive", 0, 0, 24, 0.01);
drive = ba.db2linear(drivedb);
note2hz(n) = 440 * pow(2, (n - 69)/12);
f = note2hz(cut + lfoamt * 24 * os.osc(lforate)) : min(20000) : max(20);
q = 0.7071 + res * res * 8;  // 0 -> flat Butterworth; Live's 1.25 is close to self-oscillation
one = _ <: (fi.svf.lp(f, q), fi.svf.hp(f, q), fi.svf.bp(f, q), fi.svf.notch(f, q), fi.svf.lp(f, q)) : ba.selectn(5, ftype);
two = one : one;
// drive only engages above 0 dB: at 0 dB the filter is clean and unity gain
filt(x) = select2(drivedb > 0.01, x, ma.tanh(x*drive)) <: (one, two) : select2(slope24);
proc(x) = x*(1-mix) + filt(x)*mix;
process = proc, proc;
"""

AUTOPAN = r"""
import("stdfaust.lib");
amt = hslider("amt", 0, 0, 1, 0.0001);
rate = hslider("rate", 1, 0.01, 40, 0.0001);
phase = hslider("phase", 180, 0, 360, 0.01) / 360;
shape = hslider("shape", 0, 0, 3, 1);
wave(p) = ba.selectn(4, shape, sin(2*ma.PI*p), 1 - 4*abs(p - floor(p + 0.5)), select2(p - floor(p) < 0.5, -1, 1), 2*(p - floor(p)) - 1);
ph = os.phasor(1, rate);
gl = 1 - amt * (0.5 + 0.5*wave(ph));
gr = 1 - amt * (0.5 + 0.5*wave(ph + phase));
process(l, r) = l*gl, r*gr;
"""

CHORUS = r"""
import("stdfaust.lib");
d1 = hslider("d1", 3, 0.1, 40, 0.001) / 1000 * ma.SR;
d2 = hslider("d2", 7, 0.1, 40, 0.001) / 1000 * ma.SR;
amt = hslider("amt", 1, 0, 20, 0.001) / 1000 * ma.SR;
rate = hslider("rate", 1, 0.01, 20, 0.001);
hpf = hslider("hpf", 20, 20, 2000, 0.1);
mix = hslider("mix", 0.5, 0, 1, 0.0001);
fb = hslider("fb", 0, 0, 0.95, 0.0001);
voice(x, d, ph) = x : fi.highpass(1, hpf) : de.fdelay(ma.SR/10, d + amt*(0.5 + 0.5*os.oscp(rate, ph)));
process(l, r) = l*(1-mix) + voice(l, d1, 0)*mix, r*(1-mix) + voice(r, d2, ma.PI/2)*mix;
"""

MULTIBAND = r"""
import("stdfaust.lib");
// Multiband Dynamics: three bands, each compressed downward above one threshold and upward below
// another (the "OTT" sound when pushed), then summed.
lowmid = hslider("lowmid", 120, 20, 2000, 0.1);
midhigh = hslider("midhigh", 2500, 500, 16000, 0.1);
amount = hslider("amount", 1, 0, 2, 0.0001);
tscale = hslider("tscale", 1, 0.1, 10, 0.0001);
outg = ba.db2linear(hslider("outg", 0, -36, 36, 0.01));
rms = hslider("rms", 1, 0, 1, 1);
bandctl(i) = (ba.db2linear(hslider("in%i", 0, -36, 36, 0.01)), hslider("athr%i", 0, -80, 6, 0.01), hslider("aslope%i", 0, -1, 2, 0.0001),
              hslider("bthr%i", -80, -100, 6, 0.01), hslider("bslope%i", 0, -1, 2, 0.0001), hslider("att%i", 10, 0.1, 1000, 0.01)/1000,
              hslider("rel%i", 100, 1, 5000, 0.1)/1000, ba.db2linear(hslider("gain%i", 0, -36, 36, 0.01)), hslider("on%i", 1, 0, 1, 1));
split(l) = l : fi.lowpass(4, lowmid), (l : fi.highpass(4, lowmid) : fi.lowpass(4, midhigh)), (l : fi.highpass(4, midhigh));
comp(i, x) = select2(on, x, x*ing*g*gain) with {
  ing = ctl : ba.selectn(9, 0); athr = ctl : ba.selectn(9, 1); aslope = ctl : ba.selectn(9, 2);
  bthr = ctl : ba.selectn(9, 3); bslope = ctl : ba.selectn(9, 4); att = ctl : ba.selectn(9, 5);
  rel = ctl : ba.selectn(9, 6); gain = ctl : ba.selectn(9, 7); on = ctl : ba.selectn(9, 8);
  ctl = bandctl(i);
  det = select2(rms, abs(x*ing), sqrt(max(fi.lowpass(1, 20, (x*ing)*(x*ing)), 0)));
  lv = det : si.onePoleSwitching(rel*tscale, att*tscale) : ba.linear2db;
  above = select2(lv > athr, 0, aslope*(lv - athr));
  below = select2(lv < bthr, 0, -bslope*(lv - bthr));
  g = ba.db2linear(min(30, (above + below)*amount));
};
mb(x) = x : split : (comp(0), comp(1), comp(2)) :> *(outg);
process = mb, mb;
"""

REDUX = r"""
import("stdfaust.lib");
rate = hslider("rate", 44100, 100, 96000, 1);
bits = hslider("bits", 16, 1, 24, 0.01);
mix = hslider("mix", 1, 0, 1, 0.0001);
step = max(1, ma.SR / rate);
hold(x) = x : ba.sAndH(ba.time % int(step) == 0);
q(x) = floor(x * pow(2, bits - 1) + 0.5) / pow(2, bits - 1);
proc(x) = x*(1-mix) + (hold(x) : q)*mix;
process = proc, proc;
"""

EROSION = r"""
import("stdfaust.lib");
freq = hslider("freq", 1000, 100, 20000, 0.1);
amp = hslider("amp", 0, 0, 2, 0.0001);
bq = hslider("bq", 1, 0.1, 20, 0.0001);
mode = hslider("mode", 0, 0, 2, 1);
mod = ba.selectn(3, mode, no.noise, no.noise, os.osc(freq)) : fi.resonbp(freq, bq, 1);
proc(x) = x + x * mod * amp * 0.25;
process = proc, proc;
"""


# ---- numpy devices -----------------------------------------------------------------------------

def shifter(x: np.ndarray, hz: float, mix: float = 1.0) -> np.ndarray:
    """Shifter's frequency-shift mode: every component moves by `hz` (single-sideband, Hilbert)."""
    from scipy.signal import hilbert
    if abs(hz) < 1e-6:
        return x
    t = np.arange(x.shape[1]) / SR
    out = np.empty_like(x)
    for ch in range(x.shape[0]):
        a = hilbert(x[ch].astype(np.float64))
        out[ch] = np.real(a * np.exp(2j * np.pi * hz * t)).astype(np.float32)
    return x * (1 - mix) + out * mix


def vinyl(x: np.ndarray, p: dict) -> np.ndarray:
    """Vinyl Distortion, approximated: its two resonant distortion bands as soft-saturated bells."""
    from scipy.signal import lfilter
    out = x.astype(np.float64).copy()
    for band in ("1", "2"):
        if not p.get(f"Band{band}On", False):
            continue
        g = float(p.get(f"Gain{band}", 0))
        f = float(p.get(f"BandFreq{band}", 1000))
        q = max(0.1, float(p.get(f"BandQ{band}", 1)))
        w0 = 2 * math.pi * f / SR
        al = math.sin(w0) / (2 * q)
        b = np.array([al, 0, -al]) / (1 + al)
        a = np.array([1, -2 * math.cos(w0) / (1 + al), (1 - al) / (1 + al)])
        bp = lfilter(b, a, out, axis=1)
        out = out + np.tanh(bp * (1 + g * 4)) * g * 0.5
    drive = float(p.get("Drive", 0))
    if drive > 0:
        out = np.tanh(out * (1 + drive * 3)) / np.tanh(1 + drive * 3)
    return out.astype(np.float32)
