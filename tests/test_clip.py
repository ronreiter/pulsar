"""Clips and the note editor's read / write-back."""
import pytest

from pulsar.clip import read_band, write_band
from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.song import compile_song

SRC = '''tempo 120
key "D major"
track kit = drums("909")
track lead = synth("pluck")
section a 2 {
  play kit "bd*4"
  play lead "[d4, f#4] a4".vel(0.6)   // the hook
  play lead "d5" from 1
}
section b 2 extends a {
  play kit "bd sd bd sd"
}
section c 1 { play lead "a4" }
section d 2 {
  for i in [0, 1] {
    play lead "d4" at i
  }
}
arrange a, b, c, d
'''


def notes(src, track):
    sc = compile_song(run(src)[0])
    return [(round(n.time, 4), n.pitch, n.drum, round(n.vel, 3), round(n.dur, 4)) for n in sc.notes[track]]


def test_clip_literal():
    song, _ = run('tempo 60\ntrack l = synth("pluck")\nsection a 2 { play l clip(["0 a4 1 .5, 2 c5 1/3 slide", ""]) }\n'
                  'arrange a\n')
    ns = compile_song(song).notes["l"]
    assert [(n.time, n.pitch, round(n.vel, 2)) for n in ns] == [(0.0, 69, 0.5), (2.0, 72, 0.8)]
    assert abs(ns[1].dur - 1 / 3) < 1e-9 and ns[1].ctrl.get("slide")
    with pytest.raises(PulsarError, match="beat note length"):
        run('track l = synth("pluck")\nlet c = clip(["0 a4"])\n')


def test_read_band():
    band = read_band(run(SRC)[0], "a", "lead")
    assert band["editable"] and band["kind"] == "notes"
    assert [(n["beat"], n["note"], n["len"], n["vel"]) for n in band["notes"]] == [
        ("0", "d4", "2", 0.6), ("0", "f#4", "2", 0.6), ("2", "a4", "2", 0.6),  # bar 1
        ("4", "d4", "2", 0.6), ("4", "f#4", "2", 0.6), ("4", "d5", "4", 0.8), ("6", "a4", "2", 0.6)]  # bar 2


def test_write_band_edits_one_track_in_place():
    song, _ = run(SRC)
    band = read_band(song, "a", "lead")
    band["notes"][2].update(note="b4", vel=0.9)
    new = write_band(SRC, song, "a", "lead", band["notes"])
    assert 'play lead clip([' in new and '"0 f#4 2 .6, 0 d4 2 .6, 2 b4 2 .9",' in new
    assert '"[d4, f#4] a4"' not in new and 'play lead "d5" from 1' not in new  # both plays became the clip
    assert "])   // the hook" in new  # the line's comment stays
    assert 'play kit "bd*4"' in new
    after = notes(new, "lead")
    assert (1.0, 71, None, 0.9, 1.0) in after and (1.0, 69, None, 0.6, 1.0) not in after
    assert notes(new, "kit") == notes(SRC, "kit")


def test_write_band_into_an_inherited_section_and_clearing_it():
    song, _ = run(SRC)
    # b inherits lead from a: writing it adds a play to b that overrides the inherited ones
    new = write_band(SRC, song, "b", "lead", [{"beat": "0", "note": "e4", "len": "1", "vel": 0.7}])
    lead = [n for n in notes(new, "lead") if 4 <= n[0] < 8]
    assert lead == [(4.0, 64, None, 0.7, 0.5)]
    cleared = write_band(SRC, song, "b", "lead", [])
    assert [n for n in notes(cleared, "lead") if 4 <= n[0] < 8] == []
    assert notes(cleared, "lead")[:4] == notes(SRC, "lead")[:4]  # section a untouched


def test_one_line_section_and_loops():
    song, _ = run(SRC)
    new = write_band(SRC, song, "c", "lead", [{"beat": "1", "note": "g4", "len": "1", "vel": 0.5}])
    assert [n for n in notes(new, "lead") if 8 <= n[0] < 10] == [(8.5, 67, None, 0.5, 0.5)]
    with pytest.raises(PulsarError, match="loop or an if"):
        write_band(SRC, song, "d", "lead", [])
    assert read_band(song, "d", "lead")["editable"] is False


def test_a_part_that_enters_late_is_written_with_from():
    src = 'tempo 120\ntrack l = synth("pluck")\nsection a 4 {\n  play l "a4" from 2\n}\narrange a\n'
    song, _ = run(src)
    band = read_band(song, "a", "l")
    new = write_band(src, song, "a", "l", band["notes"])
    assert '"0 a4 4 .8",\n  ]) from 2' in new
    assert notes(new, "l") == notes(src, "l")
