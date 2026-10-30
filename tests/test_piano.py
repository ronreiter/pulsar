"""Sampled pianos: SFZ parsing, region choice and rendering, with a tiny synthetic set (offline)."""
import numpy as np
import pytest
import soundfile as sf

from pulsar import piano
from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.render import Renderer
from pulsar.song import compile_song

SFZ = r"""
// a test set: two keys, two velocity layers, a release sound
<global> ampeg_release=0.2
<group> amp_veltrack=50 hivel=80
<region> sample=s\a4p.wav lokey=66 hikey=71 pitch_keycenter=69
<region> sample=s\c4p.wav lokey=58 hikey=62
<group> amp_veltrack=50 lovel=81
<region> sample=s\a4f.wav lokey=66 hikey=71 pitch_keycenter=69 tune=-10
<region> sample=s\c4f.wav lokey=58 hikey=62
<group> trigger=release volume=-6 rt_decay=3
<region> sample=s\rel.wav lokey=0 hikey=127 pitch_keytrack=0
<group> lokey=-1 hikey=-1 on_locc64=126
<region> sample=s\pedal.wav
"""


@pytest.fixture
def kit(tmp_path, monkeypatch):
    (tmp_path / "s").mkdir()
    sr = 44100
    t = np.arange(sr * 2) / sr
    for name, f, a in [("a4p", 440, 0.2), ("a4f", 440, 0.6), ("c4p", 261.63, 0.2), ("c4f", 261.63, 0.6),
                       ("rel", 1000, 0.1), ("pedal", 100, 0.1)]:
        x = (a * np.sin(2 * np.pi * f * t) * np.exp(-t)).astype(np.float32)
        sf.write(tmp_path / "s" / f"{name}.wav", np.stack([x, x]).T, sr)
    sfz = tmp_path / "test.sfz"
    sfz.write_text(SFZ)
    monkeypatch.setattr(piano, "install", lambda name, log=print: sfz)
    return sfz


def test_parse_sfz(kit):
    rs = piano.parse_sfz(kit)
    assert len(rs) == 5  # the pedal noise (no key) is left out
    c4 = piano.find(rs, 60, 64)
    assert c4.path.name == "c4p.wav" and c4.center == 60 and c4.release == 0.2 and c4.veltrack == 0.5
    assert piano.find(rs, 69, 100).tune == -10
    assert piano.find(rs, 40, 64).path.name == "c4p.wav"  # outside every range: the nearest recorded key
    rel = piano.releases(rs, 60, 64)
    assert len(rel) == 1 and rel[0].keytrack == 0 and rel[0].rt_decay == 3


def test_piano_params_are_checked():
    with pytest.raises(PulsarError, match="no piano named"):
        run('track p = piano("harpsichord")\n')
    with pytest.raises(PulsarError, match="has no parameter"):
        run('track p = piano(sustain: 1)\n')


def test_track_hiding_a_builtin_explains_itself():
    with pytest.raises(PulsarError, match="hides the built-in piano"):
        run('track piano = piano()\ntrack lead = piano()\n')


def render_note(note, vel=0.8, **opts):
    extra = "".join(f", {k}: {v}" for k, v in opts.items())
    song, _ = run(f'tempo 60\ntrack p = piano("grand"{extra})\nsection a 1 {{ play p "{note}".vel({vel}) }}\narrange a\n')
    return Renderer(compile_song(song)).voice_audio("p")[0]


def f0(x, lo, hi):
    x = x[2400:2400 + 48000]
    F = np.abs(np.fft.rfft(x * np.hanning(len(x)), 8 * len(x)))
    f = np.fft.rfftfreq(8 * len(x), 1 / 48000)
    band = (f > lo) & (f < hi)
    return f[band][F[band].argmax()]


def test_pitch_layers_and_dampers(kit):
    assert abs(f0(render_note("a4", 0.5), 400, 480) - 440) < 1
    b4 = 440 * 2 ** (2 / 12) * 2 ** (-10 / 1200)  # loud layer, re-pitched up 2 semitones, tuned -10 cents
    assert abs(f0(render_note("b4", 0.9), 450, 550) - b4) < 1
    soft, loud = np.abs(render_note("a4", 0.5)).max(), np.abs(render_note("a4", 0.95)).max()
    assert loud > 2 * soft  # the forte layer, plus velocity tracking
    held = render_note("a4", 0.5)  # a 4-beat note (4s at 60 BPM): the sample has decayed but still sounds
    short = render_note("[a4 ~ ~ ~]", 0.5, hammer=0)  # 1s, then the dampers stop it within 0.2s
    win = slice(int(1.5 * 48000), int(1.9 * 48000))
    assert np.abs(short[win]).max() < 0.01 * np.abs(held[win]).max()
    # with key-up sounds on, the release recording plays from the note-off
    noisy = render_note("[a4 ~ ~ ~]", 0.5)
    assert abs(f0(noisy[48000 - 2400:], 900, 1100) - 1000) < 2
