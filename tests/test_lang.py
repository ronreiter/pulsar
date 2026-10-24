import pytest

from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.song import compile_song

BASE = '''
tempo 120
key "A minor"
track kit = drums("909")
track bass = synth("bass")
'''


def score(src):
    song, out = run(BASE + src)
    return compile_song(song), out


def test_section_play_and_arrange():
    sc, _ = score('''
section a 2 { play kit "bd*4" }
section b 4 extends a { play bass "a1 ~ a1 c2" }
arrange a, b * 2
''')
    assert sc.total_bars == 10
    assert len(sc.notes["kit"]) == 4 * 10
    assert len(sc.notes["bass"]) == 3 * 8


def test_functions_loops_and_conditionals():
    sc, out = score('''
fn line(n) = "a1 c2".transpose(n)
fn fills(count) {
  let out = []
  for i in 0..count { out = out + ["bd*" + "2"] }
  return out
}
section a 4 {
  for i in 0..4 {
    if i % 2 == 0 { play bass line(i) at i } else { play kit "sd" at i }
  }
  print(len(fills(3)))
}
''')
    assert out == ["3"]
    assert [n.pitch for n in sc.notes["bass"]] == [33, 36, 35, 38]
    assert len(sc.notes["kit"]) == 2


def test_play_from_to_bars():
    sc, _ = score('section a 8 { play kit "bd" from 2 to 5 }')
    assert [n.bar for n in sc.notes["kit"]] == [2, 3, 4]


def test_mute_in_extended_section():
    sc, _ = score('''
section a 1 { play kit "bd"; play bass "a1" }
section b extends a { mute bass }
''')
    assert len(sc.notes["bass"]) == 1 and len(sc.notes["kit"]) == 2


def test_automation_targets():
    song, _ = run(BASE + '''
track lead = synth("lead") >> delay(1/8) >> reverb()
section a 4 {
  play lead "a4"
  automate lead.cutoff 0.2 -> 0.8
  automate lead.delay.feedback lfo(period: 2, min: 0.1, max: 0.5)
  automate lead.gain "-6 -3 0 0"
  automate master.glue 0.2 -> 0.6
}
''')
    autos = song.sections["a"].autos
    assert [a.path for a in autos] == [["synth", "cutoff"], ["fx", 0, "feedback"], ["mixer", "gain"], ["glue"]]


@pytest.mark.parametrize("src,needle", [
    ('track x = synth("acdi")', "did you mean 'acid'"),
    ('track x = synth("bass", cutof: 0.2)', "did you mean 'cutoff'"),
    ('section a 4 { play bas "a1" }', "did you mean 'bass'"),
    ('play bass "a1"', "only works inside a section"),
    ('section a 4 { automate bass.cutof 0.1 -> 0.2 }', "did you mean 'cutoff'"),
    ('section a 4 { play bass "a1".fsat(2) }', "did you mean 'fast'"),
    ('section a 4 { play bass "a1 [c2" }', "missing closing"),
    ('let x = 1 +', "unexpected"),
    ('track x = synth("bass", cutoff: 7)', "out of range"),
    ('track x = drums("909", bd_decya: 1)', "bd_decay"),
])
def test_helpful_errors(src, needle):
    with pytest.raises(PulsarError) as e:
        score(src)
    text = e.value.format(BASE + src)
    assert needle in text, text


def test_error_has_location():
    with pytest.raises(PulsarError) as e:
        score('section a 4 {\n  play bass "a1 qq"\n}')
    assert e.value.loc is not None and e.value.loc[0] == BASE.count("\n") + 2


def test_multiline_chain_and_continuation():
    song, _ = run(BASE + '''
track pad = synth("pad", reverb: 0.4)
  >> chorus(0.3)
  >> eq(low: -3)
let p = "Am7 F"
  .slow(2)
  .arp("up", 1/8)
''')
    assert [e.name for e in song.tracks["pad"].inst.inserts] == ["chorus", "eq"]


def test_keyword_prefixes_are_names():
    song, _ = run(BASE + 'track keys = synth("keys")\nlet tempos = 3\nlet seedy = 1\n')
    assert "keys" in song.tracks


def test_play_in_extended_section_replaces_inherited_track():
    sc, _ = score('''
section a 1 { play kit "bd*4"; play bass "a1" }
section b extends a { play kit "bd*2"; play kit "~ sd" }
''')
    bar1 = [n.drum for n in sc.notes["kit"] if n.bar >= 1]
    assert sorted(bar1) == ["bd", "bd", "sd"]
    assert len(sc.notes["bass"]) == 2  # untouched tracks keep playing


def test_gain_is_relative_to_preset_default():
    song, _ = run(BASE + 'track p = synth("pad", gain: -2)\np.gain = -3\n')
    tr = song.tracks["p"]
    assert tr.inst.base_gain == -11 and tr.mixer["gain"] == -3


def test_voicing_smooth_voice_leads():
    sc, _ = score('track p = synth("pad")\nsection a 4 { play p "<Am F C G>".voicing("smooth") }')
    chords = {}
    for n in sc.notes["p"]:
        chords.setdefault(n.bar, []).append(int(n.pitch))
    spans = [max(c) - min(c) for c in chords.values()]
    assert all(s <= 9 for s in spans)
    tops = [max(c) for c in chords.values()]
    assert max(tops) - min(tops) <= 3


def test_second_effect_of_same_kind():
    song, _ = run(BASE + '''
track p = synth("pad") >> hp(100) >> hp(300)
section a 2 { play p "Am"; automate p.hp_2.freq 300 -> 600 }
''')
    assert song.sections["a"].autos[0].path == ["fx", 1, "freq"]


def test_sampler_slices_pitches_and_renders(tmp_path):
    import numpy as np
    import soundfile as sf
    from pulsar.render import render
    sr = 48000
    t = np.arange(sr) / sr
    sf.write(tmp_path / "tone.wav", 0.5 * np.sin(2 * np.pi * 440 * t), sr)
    song_file = tmp_path / "s.pulse"
    song_file.write_text('''
tempo 120
track s = sampler("tone.wav", root: "a4", slices: 4, gate: true)
track p = sampler("tone.wav", root: "a4")
section a 1 { play s "0 1 ~ 3"; play p "a5" }
''')
    song, _ = run(song_file.read_text(), str(song_file))
    sc = compile_song(song)
    assert [n.ctrl.get("slice") for n in sc.notes["s"]] == [0, 1, 3]
    assert sc.notes["p"][0].pitch == 81
    res = render(sc, jobs=1)
    assert np.isfinite(res.mix).all() and np.abs(res.mix).max() > 0.1


def test_sampler_missing_file():
    with pytest.raises(PulsarError) as e:
        score('track v = sampler("nope.wav")')
    assert "not found" in e.value.msg


def test_voice_lyrics_follow_notes():
    from pulsar import tts
    song, _ = run(BASE + '''
track v = voice("af_heart", lyrics: "one two / three")
section a 1 { play v "a4 c5 e5 ~" }
''')
    sc = compile_song(song)
    assert [n.pitch for n in sc.notes["v"]] == [69, 72, 76]
    assert tts.lyric_units("one two / three") == ["one", "two", "three"]
    assert tts.lyric_lines("one two / three") == ["one two", "three"]


@pytest.mark.skipif(not __import__("pathlib").Path.home().joinpath(".cache/pulsar/models/kokoro/kokoro-v1.0.onnx").exists(),
                    reason="voice model not downloaded")
def test_sung_word_hits_its_pitch():
    import numpy as np
    import pyworld as pw
    from pulsar import tts
    y, lead = tts.sing(tts.speak("light", "af_heart"), 72, 0.8)
    f0, t = pw.harvest(y.astype(np.float64), tts.TTS_SR, f0_floor=100, f0_ceil=900, frame_period=5)
    mid = f0[(t > lead + 0.1) & (t < 0.6)]
    assert abs(np.median(mid[mid > 0]) - 523.25) < 10


def test_section_name_cannot_hide_a_variable():
    with pytest.raises(PulsarError) as e:
        score('let groove = "bd*4"\nsection groove 4 { play kit groove }')
    assert "would hide" in e.value.msg


def test_warns_when_a_sparse_play_replaces_an_inherited_groove():
    from pulsar.analyze import score_warnings
    sc, _ = score('''
section a 4 { play kit "bd*4, hh*8" }
section b 4 extends a { play kit "cr" at 0 }
''')
    assert any("replaces what it inherited" in w for w in score_warnings(sc))
