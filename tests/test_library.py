"""The open sound library: search, references, snippets, and samplers that use references (offline)."""
import json

import numpy as np
import pytest
import soundfile as sf

from pulsar import library
from pulsar.errors import PulsarError
from pulsar.interp import run
from pulsar.render import Renderer
from pulsar.song import compile_song

INDEX = [{"path": p, "size": 1000} for p in [
    "Idiophones/Struck Idiophones/Cowbells/Cowbell1_Hit_v1_rr1_Mid.wav",
    "Idiophones/Struck Idiophones/Cowbells/Cowbell1_Hit_v2_rr1_Mid.wav",
    "Idiophones/Struck Idiophones/Cowbells/Cowbell1_Hit_v3_rr1_Mid.wav",
    "Idiophones/Struck Idiophones/Anvil/Anvil_hit_1.wav",
    "Membranophones/Struck Membranophones/Legacy Snares/OldSnare/snare_f1.wav",
]]


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "CACHE", tmp_path)
    (tmp_path / "vcsl-index.json").write_text(json.dumps(INDEX))
    monkeypatch.setattr(library, "search_commons", lambda *a, **k: [])
    sr = 48000
    tone = (0.5 * np.sin(2 * np.pi * 800 * np.arange(sr // 4) / sr)).astype(np.float32)
    wav = tmp_path / "src.wav"
    sf.write(wav, tone, sr)
    monkeypatch.setattr(library, "_get", lambda url, timeout=30: wav.read_bytes())
    return tmp_path


def test_search_shows_variety_first(lib):
    hits = library.search("cowbell", source="vcsl")
    assert [h.ref.split("/")[-1] for h in hits][:3] == ["Cowbell1_Hit_v1_rr1_Mid.wav", "Cowbell1_Hit_v2_rr1_Mid.wav",
                                                       "Cowbell1_Hit_v3_rr1_Mid.wav"]
    assert all(h.license == "CC0" and h.ref.startswith("vcsl:") for h in hits)
    assert library.search("snare old", source="vcsl")[0].ref.endswith("snare_f1.wav")  # every word must match
    assert library.search("kazoo", source="vcsl") == []
    with pytest.raises(PulsarError, match="source must be"):
        library.search("x", source="splice")


def test_snippet_names_and_clashes(lib):
    it = library.search("cowbell", source="vcsl")[0]
    sn = library.snippet(it)
    assert sn["name"] == "cowbell" and sn["track"].startswith('track cowbell = sampler("vcsl:Idiophones/')
    assert "CC0" in sn["track"] and sn["play"] == 'play cowbell "x ~ ~ ~"'
    assert library.snippet(it, {"cowbell"})["name"] == "cowbell_2"


def test_a_sampler_plays_a_library_reference_and_credits_it(lib):
    ref = INDEX[3]["path"]
    song, _ = run(f'tempo 120\ntrack anvil = sampler("vcsl:{ref}")\nsection a 1 {{ play anvil "x ~ ~ ~" }}\narrange a\n')
    r = Renderer(compile_song(song))
    audio = r.voice_audio("anvil")
    assert np.abs(audio).max() > 0.01
    res = r.render()
    assert any(c["license"] == "CC0" and "Anvil" in c["title"] for c in res.info["credits"])
    path, it = library.fetch(f"vcsl:{ref}")
    assert path.exists() and it.license == "CC0"
