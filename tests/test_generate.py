"""Stable Audio generation: references, checks, caching and use in songs (offline: the model is simulated)."""
import json
import subprocess
import types

import numpy as np
import pytest
import soundfile as sf

from pulsar import generate, library
from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.render import Renderer
from pulsar.song import compile_song


def test_references_and_checks():
    r = generate.ref("glass shattering: close", "sfx", 2, 7)
    assert r == "gen:sfx:2:7:glass shattering: close" and library.is_ref(r)
    assert generate.parse(r) == ("sfx", 2.0, 7, "glass shattering: close")  # colons in the prompt survive
    with pytest.raises(PulsarError, match="no vocals") as e:
        generate.check("vocal", 3)
    assert "voice(" in e.value.hint
    with pytest.raises(PulsarError, match="between"):
        generate.check("sfx", 500)
    with pytest.raises(PulsarError, match="must be one of"):
        generate.check("huge", 3)


@pytest.fixture
def fake_model(tmp_path, monkeypatch):
    """Stand in for the Stable Audio environment: writes a short tone for each request."""
    monkeypatch.setattr(generate, "OUT", tmp_path / "generated")
    monkeypatch.setattr(generate, "setup", lambda log=print: tmp_path / "python")
    calls = []

    def fake_run(cmd, capture_output=True, text=True):
        args = json.loads(cmd[-1])
        calls.append(args)
        sr = 44100
        t = np.arange(int(args["seconds"] * sr)) / sr
        sf.write(args["out"], np.stack([0.3 * np.sin(2 * np.pi * 600 * t)] * 2).T, sr)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_generate_once_then_cached(fake_model):
    a = generate.generate("door creak", "sfx", 1.5, 3, log=lambda m: None)
    b = generate.generate("door creak", "sfx", 1.5, 3, log=lambda m: None)
    assert a == b and a.exists() and len(fake_model) == 1  # the second request comes from the cache
    assert fake_model[0]["model"] == "small-sfx" and fake_model[0]["seed"] == 3
    generate.generate("door creak", "sfx", 1.5, 4, log=lambda m: None)  # another seed is another sound
    assert len(fake_model) == 2


def test_gated_weights_explain_themselves(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "OUT", tmp_path)
    monkeypatch.setattr(generate, "setup", lambda log=print: tmp_path / "python")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="", stderr="huggingface_hub.errors.GatedRepoError: 401 Client Error"))
    with pytest.raises(PulsarError, match="gated") as e:
        generate.generate("thunder", "sfx", 2, 0, log=lambda m: None)
    assert "huggingface.co/stabilityai/stable-audio-3-small-sfx" in e.value.hint and "auth login" in e.value.hint


def test_a_song_plays_a_generated_sound(fake_model):
    song, _ = run('tempo 120\ntrack fx = sampler(generate("laser zap", seconds: 1))\n'
                  'section a 1 { play fx "x ~ ~ ~" }\narrange a\n')
    r = Renderer(compile_song(song))
    assert np.abs(r.voice_audio("fx")).max() > 0.01
    res = r.render()
    assert any("Stable Audio 3" in c["credit"] and c["title"] == "laser zap" for c in res.info["credits"])
