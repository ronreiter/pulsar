"""Stock ambience: license filtering, search ranking, resolution and rendering -- all offline."""
import numpy as np
import pytest
import soundfile as sf

from pulsar import stock
from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.render import Renderer
from pulsar.song import compile_song


@pytest.mark.parametrize("name,policy,ok", [
    ("CC0", "cc0", True), ("Public domain", "cc0", True), ("CC BY 4.0", "cc0", False),
    ("CC BY 4.0", "open", True), ("CC BY-SA 3.0", "open", False), ("CC BY-SA 3.0", "by-sa", True),
    ("CC BY-NC 4.0", "by-sa", False), ("CC BY-ND 2.0", "by-sa", False), ("", "open", False),
    ("GFDL", "open", False)])
def test_license_filter(name, policy, ok):
    assert stock.license_ok(name, policy) is ok


def test_score_prefers_matches_and_rejects_music():
    nature = stock._score("water stream", "File:Mountain stream.ogg", "field recording of water")
    music = stock._score("water stream", "File:Down by the stream (1911).mp3", "song by a quartet")
    assert nature > 3 > music


def fake_sound(tmp_path, sid="File:Brook.ogg", seconds=3.0):
    sr = 48000
    t = np.arange(int(seconds * sr)) / sr
    x = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    path = tmp_path / "src.wav"
    sf.write(path, np.stack([x, x]).T, sr)
    return stock.Sound("commons", sid, sid[5:], "Someone", "CC0", "", "https://example.org/brook", path.as_uri(),
                       seconds, 5.0)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(stock, "CACHE", tmp_path / "cache")
    return tmp_path


def test_resolve_downloads_once_and_remembers(cache, monkeypatch):
    calls = []
    snd = fake_sound(cache)
    monkeypatch.setattr(stock, "search", lambda *a, **k: calls.append(a) or [snd])
    path, s = stock.resolve("brook")
    assert path.exists() and s.id == snd.id
    path2, _ = stock.resolve("brook")
    assert path2 == path and len(calls) == 1  # second time comes from the index, no search


def test_resolve_nothing_found(cache, monkeypatch):
    monkeypatch.setattr(stock, "search", lambda *a, **k: [])
    with pytest.raises(PulsarError, match="no open-licensed"):
        stock.resolve("unicorns")


def test_ambience_params_are_checked():
    with pytest.raises(PulsarError, match="license must be one of"):
        run('track a = ambience("rain", license: "nc")\n')
    with pytest.raises(PulsarError, match="has no parameter"):
        run('track a = ambience("rain", loop: 1)\n')


def test_ambience_plays_continuously_with_fades(cache, monkeypatch):
    snd = fake_sound(cache, seconds=3.0)
    monkeypatch.setattr(stock, "search", lambda *a, **k: [snd])
    song, _ = run('''tempo 120
track rain = ambience("rain", fade: 0.5)
section a 4 { play rain "x" }
section b 2 { }
section c 2 { play rain "x" }
arrange a, b, c
''')
    score = compile_song(song)
    r = Renderer(score)
    audio = r.voice_audio("rain")
    sr = 48000
    env = np.abs(audio[0])
    # bars are 2s: plays at 0-8s and 12-16s, silence between, a fade-in at the start
    assert env[: int(0.05 * sr)].max() < env[int(2 * sr): int(3 * sr)].max() * 0.2
    assert env[int(9 * sr): int(11 * sr)].max() == 0
    assert env[int(13 * sr): int(15 * sr)].max() > 0
    # 8s from a 3s recording: looped, with no sample-to-sample jump bigger than the sine itself makes
    seg = audio[0, int(1 * sr): int(7 * sr)]
    assert np.abs(np.diff(seg)).max() < 0.1
