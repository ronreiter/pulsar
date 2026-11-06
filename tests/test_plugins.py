"""Plugin instruments: names, presets, errors and automation offline; real rendering when Surge XT is installed."""
import numpy as np
import pytest

from pulsar import plugins
from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.render import Renderer
from pulsar.song import compile_song


def test_names():
    assert plugins.norm("Filter 1 Cutoff") == "filter_1_cutoff"
    assert plugins.norm("A Osc 1 Pitch (semis)") == "a_osc_1_pitch_semis"
    assert plugins.key("Surge XT") == "surge" and plugins.key("Vitalium") == "vital" and plugins.key("serum2") == "serum"


def test_plugin_arguments_are_checked():
    with pytest.raises(PulsarError, match="no plugin named"):
        run('track a = plugin("massive")\n')
    with pytest.raises(PulsarError, match="0-1"):
        run('track a = plugin("surge", filter_1_cutoff: 3)\n')
    song, _ = run('track a = plugin("surge", preset: "Leads/x", filter_1_cutoff: 0.4, gain: -3, transpose: -12)\n')
    inst = song.tracks["a"].inst
    assert inst.kind == "plugin" and inst.sample["params"] == {"filter_1_cutoff": 0.4}
    assert inst.sample["transpose"] == -12 and song.tracks["a"].mixer["gain"] == -3


def test_plugin_parameters_are_automatable():
    song, _ = run('tempo 120\ntrack a = plugin("surge")\nsection s 2 {\n  play a "a4"\n'
                  '  automate a.filter_1_cutoff 0.2 -> 0.8\n  automate a.gain -6\n}\narrange s\n')
    autos = compile_song(song).autos
    assert ("a", ("plugin", "filter_1_cutoff")) in autos and ("a", ("mixer", "gain")) in autos


def test_find_preset(tmp_path, monkeypatch):
    for rel in ["Leads/Acid Lead.fxp", "Leads/Saw Lead.fxp", "Basses/Fat Bass.fxp", "Pads/Glass Pad.fxp"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"CcnK")
    monkeypatch.setattr(plugins, "preset_dirs", lambda name: [tmp_path])
    assert plugins.find_preset("surge", "Leads/Acid Lead").name == "Acid Lead.fxp"
    assert plugins.find_preset("surge", "acid lead").name == "Acid Lead.fxp"
    assert plugins.find_preset("surge", "fat").name == "Fat Bass.fxp"
    with pytest.raises(PulsarError, match="partly"):
        plugins.find_preset("surge", "lead")  # two match: say so instead of guessing
    with pytest.raises(PulsarError, match="no surge preset"):
        plugins.find_preset("surge", "kazoo")


def test_missing_commercial_plugin_says_where_to_get_it(monkeypatch):
    monkeypatch.setattr(plugins, "locate", lambda name: None)
    with pytest.raises(PulsarError, match="isn't installed") as e:
        plugins.ensure("serum")
    assert "xferrecords" in e.value.hint


surge = pytest.mark.skipif(plugins.locate("surge") is None, reason="Surge XT is not installed (pulsar plugins install)")


def render(src, track):
    return Renderer(compile_song(run(src)[0])).voice_audio(track)


@surge
def test_surge_plays_the_right_notes_in_time():
    sr = 48000
    a = render('tempo 60\ntrack s = plugin("surge")\nsection x 2 { play s "[a4 ~ ~ ~] [~ ~ c5 ~]".slow(2) }\narrange x\n', "s")
    m = a.mean(0)
    on = lambda t: np.sqrt((m[int(t * sr):int((t + 0.3) * sr)] ** 2).mean())
    assert on(0.1) > 0.01 and on(6.2) > 0.01 and on(3.2) < on(0.1) / 10  # a4 at 0 s, c5 at 6 s, quiet between
    seg = m[int(0.2 * sr):int(0.9 * sr)]
    f = np.fft.rfftfreq(len(seg), 1 / sr)[np.abs(np.fft.rfft(seg * np.hanning(len(seg)))).argmax()]
    assert abs(f - 440) < 6


@surge
def test_surge_preset_and_automation_change_the_sound():
    base = 'tempo 60\ntrack s = plugin("surge"{opts})\nsection x 2 {{ play s "a3".slow(2){auto} }}\narrange x\n'
    plain = render(base.format(opts="", auto=""), "s")
    names = plugins.presets("surge")
    assert len(names) > 500
    pre = render(base.format(opts=', preset: "' + str(names[0]) + '"', auto=""), "s")
    assert not np.allclose(plain[:, :48000], pre[:, :48000], atol=1e-3)

    def bright(a):
        F = np.abs(np.fft.rfft(a.mean(0))) ** 2
        f = np.fft.rfftfreq(a.shape[1], 1 / 48000)
        return F[f > 1500].sum() / F.sum()
    # the default patch has its filter switched off; the acid bass preset uses it
    acid = 'tempo 120\ntrack s = plugin("surge", preset: "Cybersoda/Basses/Acid Bassline 1"{p})\nsection x 2 {{ play s "a2*8"{a} }}\narrange x\n'
    closed = render(acid.format(p="", a="\n  automate s.a_filter_1_cutoff 0.1"), "s")
    opened = render(acid.format(p="", a="\n  automate s.a_filter_1_cutoff 0.9"), "s")
    assert bright(opened) > 3 * bright(closed)  # automation reaches the plugin
    fixed = render(acid.format(p=", a_filter_1_cutoff: 0.9", a=""), "s")
    assert bright(fixed) > 3 * bright(closed)  # and so does a plain setting
