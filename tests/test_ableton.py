"""Importing Ableton Live sets: parsing, device emulations, the mix graph and the Pulsar round-trip (offline)."""
import gzip
import os

import numpy as np
import pytest
import soundfile as sf

from pulsar import plugins
from pulsar.ableton import als, devices as D
from pulsar.ableton.engine import Renderer
from pulsar.interp import run
from pulsar.song import compile_song

SR = 48000

SET = """<?xml version="1.0" encoding="UTF-8"?>
<Ableton MajorVersion="5" MinorVersion="11.0_436" Creator="test">
<LiveSet>
<Tracks>
 <GroupTrack Id="10">
  <Name><EffectiveName Value="Bus"/></Name><TrackGroupId Value="-1"/>
  <DeviceChain><Mixer><Volume><Manual Value="0.5011872"/></Volume><Pan><Manual Value="0"/></Pan><Speaker><Manual Value="true"/></Speaker><Sends/></Mixer>
   <DeviceChain><Devices/></DeviceChain></DeviceChain>
 </GroupTrack>
 <AudioTrack Id="11">
  <Name><EffectiveName Value="Tone"/></Name><TrackGroupId Value="10"/>
  <DeviceChain><Mixer><Volume><Manual Value="1"/></Volume><Pan><Manual Value="0"/></Pan><Speaker><Manual Value="true"/></Speaker><Sends/></Mixer>
   <DeviceChain><Devices>
    <AudioEffectGroupDevice Id="1"><On><Manual Value="true"/></On>
     <MacroControls.0><Manual Value="0"/><AutomationTarget Id="500"/></MacroControls.0>
     <Branches>
      <AudioEffectBranch><Name Value="a"/><BranchSelectorRange Min="0" Max="0"/><IsSoloed Value="false"/>
       <DeviceChain><AudioToAudioDeviceChain><Devices>
        <StereoGain Id="2"><On><Manual Value="true"/></On>
         <Gain><Manual Value="0.5"/><AutomationTarget Id="600"/></Gain>
         <PhaseInvertR><Manual Value="true"/><KeyMidi><Channel Value="16"/><NoteOrController Value="0"/></KeyMidi></PhaseInvertR>
         <StereoWidth><Manual Value="1"/></StereoWidth>
        </StereoGain>
       </Devices></AudioToAudioDeviceChain></DeviceChain>
       <MixerDevice><Volume><Manual Value="1"/></Volume><Panorama><Manual Value="0"/></Panorama><Speaker><Manual Value="true"/></Speaker><On><Manual Value="true"/></On></MixerDevice>
      </AudioEffectBranch>
     </Branches>
    </AudioEffectGroupDevice>
   </Devices></DeviceChain>
   <MainSequencer><Sample><ArrangerAutomation><Events>
    <AudioClip Id="3"><CurrentStart Value="4"/><CurrentEnd Value="8"/>
     <Loop><LoopStart Value="0"/><LoopEnd Value="4"/><StartRelative Value="0"/><LoopOn Value="false"/></Loop>
     <SampleRef><FileRef><RelativePath Value="tone.wav"/></FileRef></SampleRef>
     <IsWarped Value="true"/><WarpMode Value="0"/><SampleVolume Value="1"/><Disabled Value="false"/>
    </AudioClip>
   </Events></ArrangerAutomation></Sample></MainSequencer>
  </DeviceChain>
 </AudioTrack>
 <MidiTrack Id="12">
  <Name><EffectiveName Value="Lead"/></Name><TrackGroupId Value="-1"/>
  <DeviceChain><Mixer><Volume><Manual Value="1"/></Volume><Pan><Manual Value="0"/></Pan><Speaker><Manual Value="true"/></Speaker><Sends/></Mixer>
   <DeviceChain><Devices/></DeviceChain>
   <MainSequencer><ClipTimeable><ArrangerAutomation><Events>
    <MidiClip Id="4"><CurrentStart Value="0"/><CurrentEnd Value="8"/>
     <Loop><LoopStart Value="0"/><LoopEnd Value="2"/><StartRelative Value="0"/><LoopOn Value="true"/></Loop>
     <Notes><KeyTracks><KeyTrack><MidiKey Value="69"/><Notes>
      <MidiNoteEvent Time="0" Duration="0.5" Velocity="100" IsEnabled="true"/>
      <MidiNoteEvent Time="1" Duration="0.5" Velocity="64" IsEnabled="true"/>
     </Notes></KeyTrack></KeyTracks></Notes><Disabled Value="false"/>
    </MidiClip>
   </Events></ArrangerAutomation></ClipTimeable></MainSequencer>
  </DeviceChain>
 </MidiTrack>
</Tracks>
<ReturnTracks/>
<MainTrack><Name><EffectiveName Value="Main"/></Name>
 <DeviceChain><Mixer><Volume><Manual Value="1"/></Volume><Pan><Manual Value="0"/></Pan><Speaker><Manual Value="true"/></Speaker><Tempo><Manual Value="120"/></Tempo><Sends/></Mixer>
  <DeviceChain><Devices/></DeviceChain></DeviceChain>
</MainTrack>
<Locators><Locators>
 <Locator><Time Value="0"/><Name Value="Intro"/></Locator>
 <Locator><Time Value="4"/><Name Value="Main"/></Locator>
</Locators></Locators>
</LiveSet>
</Ableton>
"""


@pytest.fixture
def live_set(tmp_path):
    t = np.arange(SR * 3) / SR
    sf.write(tmp_path / "tone.wav", (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), SR)
    p = tmp_path / "test.als"
    p.write_bytes(gzip.compress(SET.encode()))
    return p


def test_read_set(live_set):
    s = als.read(live_set)
    assert s["tempo"] == 120 and s["length_beats"] == 8
    assert [t["name"] for t in s["tracks"]] == ["Bus", "Tone", "Lead"] and s["tracks"][1]["group"] == "10"
    assert abs(s["tracks"][0]["mixer"]["volume_db"] + 6.0) < 0.01
    clip = s["tracks"][1]["audio_clips"][0]
    assert clip["sample"].endswith("tone.wav") and clip["start"] == 4
    rack = s["tracks"][1]["devices"][0]
    gain = rack["branches"][0]["devices"][0]["params"]
    assert gain["PhaseInvertR"]["macro"] == {"index": 0, "min": None, "max": None}
    # a 2-beat loop in an 8-beat clip plays four times; notes at 0 and 1 beat of each pass
    notes = als.arrangement_notes(s["tracks"][2]["midi_clips"], 120)
    assert [round(n[0], 3) for n in notes] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    assert notes[0][2] == 69 and abs(notes[1][3] - 64 / 127) < 1e-6


def test_mix_graph(live_set):
    s = als.read(live_set)
    mix = Renderer(s, {}, 4.0, log=lambda m: None).render()
    # silence before the clip (beat 4 = 2 s), then the tone through Utility (-6 dB) and the group (-6 dB)
    assert np.abs(mix[:, :int(1.9 * SR)]).max() < 1e-4
    level = np.abs(mix[0, int(2.5 * SR):int(3.5 * SR)]).max()
    assert abs(20 * np.log10(level / 0.5) + 12.0) < 0.3
    # the phase switch is mapped to macro 1, which is at 0: off, so the channels stay in phase
    assert np.corrcoef(mix[0, int(2.5 * SR):int(3.5 * SR)], mix[1, int(2.5 * SR):int(3.5 * SR)])[0, 1] > 0.99


def test_macro_values(live_set):
    s = als.read(live_set)
    r = Renderer(s, {}, 1.0, log=lambda m: None)
    d = {"params": {"Gain": {"value": 1.0, "target": None, "macro": {"index": 0, "min": 0.1, "max": 1.0}},
                    "Mix": {"value": 1.0, "target": None, "macro": {"index": 0, "min": 0.0, "max": 1.0}},
                    "On": {"value": True, "target": None, "macro": {"index": 0, "min": None, "max": None}}}}
    half = [63.5]
    assert abs(r.value(d, "Gain", 0, half) - 0.1 * 10 ** 0.5) < 1e-3   # gains sweep in dB
    assert abs(r.value(d, "Mix", 0, half) - 0.5) < 1e-3                # plain values sweep linearly
    assert r.value(d, "On", 0, [10.0]) == 0.0 and r.value(d, "On", 0, [100.0]) == 1.0


def fx(code, x, params):
    import dawdreamer as daw
    e = daw.RenderEngine(SR, 512)
    p = e.make_faust_processor("f")
    p.set_dsp_string(code)
    assert p.compile()
    names = {d["name"].split("/")[-1]: d["name"] for d in p.get_parameters_description()}
    for k, v in params.items():
        if k in names:
            p.set_parameter(names[k], float(v))
    src = e.make_playback_processor("s", np.ascontiguousarray(x, np.float32))
    e.load_graph([(src, []), (p, ["s"])])
    e.render(x.shape[1] / SR)
    return e.get_audio()[:, :x.shape[1]]


def tone(f, a=0.25, s=1.0):
    m = a * np.sin(2 * np.pi * f * np.arange(int(s * SR)) / SR)
    return np.stack([m, m])


def level(y, a=0.25):
    return 20 * np.log10(np.abs(y[0, SR // 2:]).max() / a)


def test_devices():
    eq = D.eq8_code([{"mode": 3}])
    assert abs(level(fx(eq, tone(1000), dict(f0=1000, g0=6, q0=1))) - 6) < 0.3
    lc = D.eq8_code([{"mode": 0}])
    assert abs(level(fx(lc, tone(100), dict(f0=200, q0=0.71))) + 48) < 1.5  # 48 dB/octave
    y = fx(D.LIMITER, tone(200, 0.9), dict(gain=6, ceil=-1, rel=100))
    assert abs(20 * np.log10(np.abs(y[:, 2400:]).max()) + 1) < 0.1
    imp = np.zeros((2, SR))
    imp[:, 0] = 1
    y = fx(D.DELAY, imp, dict(tl=0.25, tr=0.25, fb=0.5, mix=1, pingpong=1))
    hits = lambda ch: [round(i / SR, 2) for i in np.nonzero(np.abs(y[ch]) > 0.05)[0]]
    assert hits(0)[:2] == [0.25, 0.75] and hits(1)[:1] == [0.5]  # echoes alternate sides
    y = fx(D.UTILITY, np.stack([tone(440)[0], -tone(440)[0]]), dict(gain=1, width=1, mono=1))
    assert np.abs(y).max() < 1e-6


def test_juce_base64_round_trip():
    for n in (0, 1, 2, 3, 4, 1000, 4097):
        d = os.urandom(n)
        assert plugins.juce_b64decode(plugins.juce_b64encode(d)) == d
    assert plugins.juce_b64encode(b"\x01\x00\x00") == "3.A..."  # 6 bits per char, least significant first


def test_import_round_trip(live_set, tmp_path):
    from pulsar.ableton.importer import write_pulse
    out = write_pulse(live_set, tmp_path / "song.pulse")
    src = out.read_text()
    assert 'ableton_set("' in src and 'track lead = ableton_track("Lead")' in src
    song, _ = run(src, str(out))
    sc = compile_song(song)
    assert sc.total_bars == 2 and [n.pitch for n in sc.notes["lead"]] == [69.0] * 8


def test_cli_render_of_an_imported_set(live_set, tmp_path):
    # the whole command, mix report included (it crashed on tracks rendered inside a set)
    import subprocess, sys
    from pulsar.ableton.importer import write_pulse
    song = write_pulse(live_set, tmp_path / "song.pulse")
    r = subprocess.run([sys.executable, "-m", "pulsar", "render", str(song), "-o", str(tmp_path / "out.wav")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    assert (tmp_path / "out.wav").exists() and "loudness" in r.stdout


def test_compact_import_plays_the_same_notes(live_set, tmp_path):
    from pulsar.ableton.importer import write_pulse
    full = write_pulse(live_set, tmp_path / "full.pulse", compact=False)
    small = write_pulse(live_set, tmp_path / "small.pulse", compact=True)
    notes = lambda p: [(round(n.time, 6), round(n.dur, 6), n.pitch, round(n.vel, 2))
                       for n in compile_song(run(p.read_text(), str(p))[0]).notes["lead"]]
    assert notes(small) == notes(full)
    src = small.read_text()
    assert "let lead_1 = " in src and src.count("play lead lead_1") == 2  # one loop, used by both sections


def test_calibration_master_eq_and_width(live_set):
    s = als.read(live_set)
    plain = Renderer(s, {}, 4.0, log=lambda m: None).render()
    cal = {"__eq__": [[40, 0, 0.7], [440, 6, 1.0], [10000, 0, 0.7]], "__width__": 1.0}
    louder = Renderer(s, {}, 4.0, log=lambda m: None, trim=cal).render()
    w = slice(int(2.5 * SR), int(3.5 * SR))
    gain = 20 * np.log10(np.abs(louder[0, w]).max() / np.abs(plain[0, w]).max())
    assert abs(gain - 6) < 0.4  # the 440 Hz tone gets the 440 Hz bell's +6 dB
    st = np.stack([plain[0], plain[0] * 0.5])
    r = Renderer(s, {}, 4.0, log=lambda m: None, trim={"__width__": 0.0})
    mono = r.match(st)
    assert np.allclose(mono[0], mono[1])  # width 0 folds to mono
