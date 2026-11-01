"""The sampled orchestra: mapping VSCO-2 file names to keys, layers and takes, and playing them (offline)."""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pulsar import orchestra
from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.render import Renderer
from pulsar.song import compile_song


def tone(path: Path, freq: float, amp: float, seconds: float = 2.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 44100
    t = np.arange(int(seconds * sr)) / sr
    x = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    x[-int(0.3 * sr):] *= np.linspace(1, 0, int(0.3 * sr))  # a release at the end, like the real samples
    sf.write(path, np.stack([x, x]).T, sr)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestra, "HOME", tmp_path)
    d = tmp_path / "Strings/Violin Section/susVib"
    for note, f in [("A3", 220.0), ("A4", 440.0)]:
        tone(d / f"VlnEns_susVib_{note}_v1.wav", f, 0.1)
        tone(d / f"VlnEns_susVib_{note}_v2.wav", f, 0.4)
    k = tmp_path / "VSCO 1 Percussion/drums/bass"
    tone(k / "bdrum_p_1.wav", 60, 0.1, 1.0)
    tone(k / "bdrum_fff_1.wav", 60, 0.6, 1.0)
    tone(k / "bdrum_fff_2.wav", 62, 0.6, 1.0)
    tone(tmp_path / "Percussion/Snare2-HitSN_v1_rr1_Sum.wav", 200, 0.3, 0.5)

    def install(name, log=print):
        if name == "percussion":
            return sorted(tmp_path.rglob("*.wav"))
        return sorted(d.glob("*.wav"))
    monkeypatch.setattr(orchestra, "install", install)
    return tmp_path


def test_file_names_map_to_keys_layers_and_takes(lib):
    rs = orchestra.regions("violins", orchestra.install("violins"))
    a3 = [r for r in rs if r.center == 57]
    assert {(r.lokey, r.hikey) for r in a3} == {(0, 63)} and {(r.lovel, r.hivel) for r in a3} == {(1, 64), (65, 127)}
    assert all(r.loop == "auto" for r in rs)  # sustains loop when a note outlasts them
    kit = orchestra.kit_regions(orchestra.install("percussion"))
    assert set(kit) == {"bd", "snare"}
    loud = [r for r in kit["bd"] if r.hivel == 127]
    assert len(loud) == 2 and {r.rr for r in loud} == {1, 2}  # two takes of the fff layer


def test_parameters_are_checked():
    with pytest.raises(PulsarError, match="no orchestra instrument"):
        run('track v = orchestra("kazoo")\n')
    with pytest.raises(PulsarError, match="has no parameter"):
        run('track v = orchestra("violins", taiko_gain: 2)\n')
    run('track p = orchestra("percussion", taiko_gain: 2)\n')


def render(src, track):
    return Renderer(compile_song(run(src)[0])).voice_audio(track)[0]


def test_layers_takes_loops_and_kit(lib):
    sr = 48000
    soft = render('tempo 60\ntrack v = orchestra("violins")\nsection a 1 { play v "a4".vel(0.3) }\narrange a\n', "v")
    loud = render('tempo 60\ntrack v = orchestra("violins")\nsection a 1 { play v "a4".vel(0.9) }\narrange a\n', "v")
    assert np.abs(loud).max() > 2.5 * np.abs(soft).max()  # the forte layer, not just a louder copy
    # a 12 s note from 2 s recordings: looped, without dropping out
    held = render('tempo 60\ntrack v = orchestra("violins")\nsection a 3 { play v "a4".legato(1) }\narrange a\n', "v")
    win = sr // 4
    env = [np.sqrt((held[i:i + win] ** 2).mean()) for i in range(sr, 11 * sr, win)]
    assert min(env) > 0.5 * max(env)
    # kit: aliases work (snare -> sd -> snare) and repeated hits alternate takes
    hits = render('tempo 60\ntrack p = orchestra("percussion")\nsection a 1 { play p "bd bd snare ~".vel(1) }\narrange a\n', "p")
    f = lambda seg: np.fft.rfftfreq(len(seg), 1 / sr)[np.abs(np.fft.rfft(seg)).argmax()]
    assert {round(f(hits[0:int(0.9 * sr)])), round(f(hits[sr:int(1.9 * sr)]))} == {60, 62}
    assert np.abs(hits[2 * sr:int(2.4 * sr)]).max() > 0.01
    with pytest.raises(PulsarError, match="no voice"):
        render('tempo 60\ntrack p = orchestra("percussion")\nsection a 1 { play p "cowbell" }\narrange a\n', "p")


def test_to_ends_notes():
    sc = compile_song(run('tempo 60\ntrack v = synth("pad")\nsection a 4 { play v "a4".slow(4) to 2.5 }\narrange a\n')[0])
    assert sc.notes["v"][0].dur == pytest.approx(10.0)  # 4-bar note cut at bar 2.5 (bars are 4 s)
