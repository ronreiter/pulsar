from fractions import Fraction as F

import pytest

from pulsar.errors import PulsarError
from pulsar.pattern import Ctx, mini

D = Ctx(kind="drums")
M = Ctx(kind="melodic", octave=4)


def hits(s, cycles=1, ctx=D):
    out = []
    for e in mini(s).onsets(0, cycles, ctx):
        out.append((e.b, e.e - e.b, e.v.get("drum") or tuple(e.v.get("notes", ()))))
    return out


def test_sequence_and_rest():
    assert hits("bd ~ sd ~") == [(F(0), F(1, 4), "bd"), (F(1, 2), F(1, 4), "sd")]


def test_subdivision_and_fast():
    assert [h[0] for h in hits("bd [sd sd]")] == [0, F(1, 2), F(3, 4)]
    assert len(hits("hh*8")) == 8


def test_stack():
    assert sorted(h[2] for h in hits("bd, hh hh")) == ["bd", "hh", "hh"]


def test_alternation_keeps_its_own_cycle():
    assert [h[2] for h in hits("<bd sd cp>", 4)] == ["bd", "sd", "cp", "bd"]
    # nested alternation inside a subsequence advances once per cycle
    assert [h[2] for h in hits("hh <bd sd>", 2)] == ["hh", "bd", "hh", "sd"]


def test_euclid():
    assert [h[0] * 8 for h in hits("bd(3,8)")] == [0, 3, 6]


def test_elongation_and_weight():
    assert hits("bd _ _ sd")[0][1] == F(3, 4)
    assert hits("bd@3 sd")[0][1] == F(3, 4)


def test_replicate():
    assert len(hits("bd!4")) == 4
    assert len(hits("bd ! ! sd")) == 4


def test_slow_spans_cycles():
    h = hits("bd/2", 4)
    assert [x[0] for x in h] == [0, 2]


def test_degrade_is_deterministic():
    a = hits("hh*16?", 4)
    b = hits("hh*16?", 4)
    assert a == b and 0 < len(a) < 64


def test_notes_chords_degrees():
    assert hits("a3", ctx=M)[0][2] == (57,)
    assert hits("Am7", ctx=Ctx(kind="melodic", octave=3))[0][2] == (57, 60, 64, 67)
    # degrees in A minor at octave 4: 0 -> a4, 2 -> c5
    assert [h[2][0] for h in hits("0 2", ctx=M)] == [69, 72]
    # roman numerals are diatonic: iv in A minor is D minor
    assert hits("iv", ctx=M)[0][2] == (74, 77, 81)


def test_bad_token_reports_location():
    with pytest.raises(PulsarError) as e:
        mini("a1 zz", (3, 10)).onsets(0, 1, M)
    assert e.value.loc == (3, 14)


def test_unclosed_bracket():
    with pytest.raises(PulsarError):
        mini("bd [sd")


def test_arp_and_transpose():
    p = mini("Am").arp("up", F(1, 8))
    notes = [e.v["notes"][0] for e in p.onsets(0, 1, Ctx(kind="melodic", octave=3))]
    assert notes[:4] == [57, 60, 64, 57] and len(notes) == 8
    assert mini("a3").transpose(12).onsets(0, 1, M)[0].v["notes"] == [69]


def test_every_and_rev():
    p = mini("bd sd").every(2, lambda q: q.rev())
    seq = [e.v["drum"] for e in p.onsets(0, 2, D)]
    assert seq == ["sd", "bd", "bd", "sd"]


def test_struct_samples_values():
    p = mini("<Am F>").struct(mini("x ~ x x"))
    evs = p.onsets(0, 2, Ctx(kind="melodic", octave=3))
    assert len(evs) == 6 and evs[0].e - evs[0].b == F(1, 4)


def test_capitalised_digit_is_a_chord_lowercase_is_a_note():
    c = Ctx(kind="melodic", octave=3)
    assert hits("E6", ctx=c)[0][2] == (52, 56, 59, 61)
    assert hits("e6", ctx=c)[0][2] == (88,)
    assert hits("G7", ctx=c)[0][2] == (55, 59, 62, 65)
