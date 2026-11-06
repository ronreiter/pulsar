"""Read an Ableton Live set (.als): tracks, routing, mixer, devices, clips, notes and automation.

The .als is gzipped XML. This module turns it into plain Python data (dicts and lists) that the
importer (pulsar.ableton.importer) translates into a Pulsar song. Times are in beats unless a key
says otherwise; levels are in dB.
"""
from __future__ import annotations

import gzip
import math
import xml.etree.ElementTree as ET
from pathlib import Path

INITIAL = -63072000.0  # Ableton's time for an envelope's value before the song starts


def _v(el, path, default=None, cast=str):
    if el is None:
        return default
    x = el.find(path) if path else el
    if x is None or x.get("Value") is None:
        return default
    val = x.get("Value")
    if cast is bool:
        return val == "true"
    try:
        return cast(val)
    except ValueError:
        return default


def lin_db(x: float) -> float:
    return -math.inf if x <= 0 else 20 * math.log10(x)


def _params(dev) -> dict:
    """Every automatable parameter of a device: {path: {"value": v, "target": automation id}}."""
    out = {}

    def walk(el, prefix):
        for c in el:
            if c.tag in ("Branches", "DeviceChain", "Devices", "PluginDesc", "MxPatchRef", "SampleParts",
                         "MultiSampleMap", "AutomationEnvelopes"):
                continue
            path = f"{prefix}{c.tag}"
            m = c.find("Manual")
            if m is not None and m.get("Value") is not None:
                at = c.find("AutomationTarget")
                raw = m.get("Value")
                val = raw == "true" if raw in ("true", "false") else _num(raw)
                out[path] = {"value": val, "target": at.get("Id") if at is not None else None}
                # Mapped to a macro of the enclosing rack: Live marks it as MIDI channel 16, controller = macro.
                km = c.find("KeyMidi")
                if km is not None and _v(km, "Channel") == "16" and _v(km, "NoteOrController", -1, int) >= 0:
                    rng = c.find("MidiControllerRange")
                    lo, hi = _v(rng, "Min", None, float), _v(rng, "Max", None, float)
                    out[path]["macro"] = {"index": _v(km, "NoteOrController", 0, int),
                                          "min": lo, "max": hi}
            elif len(c):
                walk(c, path + "/")
    walk(dev, "")
    return out


def _num(s):
    try:
        f = float(s)
        return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f
    except ValueError:
        return s


def _sample_path(fileref, project_dir: Path) -> str | None:
    if fileref is None:
        return None
    rel = _v(fileref, "RelativePath")
    path = _v(fileref, "Path")
    for cand in ([project_dir / rel] if rel else []) + ([Path(path)] if path else []):
        if cand.exists():
            return str(cand)
    # samples moved with the project: find by name under it
    name = Path(rel or path or "").name
    if name:
        hits = list(project_dir.rglob(name))
        if hits:
            return str(hits[0])
    return rel or path


def _plugin(dev) -> dict:
    vst3 = dev.find("PluginDesc/Vst3PluginInfo")
    vst2 = dev.find("PluginDesc/VstPluginInfo")
    au = dev.find("PluginDesc/AuPluginInfo")
    info = vst3 if vst3 is not None else vst2 if vst2 is not None else au
    name = _v(info, "Name") or _v(info, "PlugName") or "?"
    out = {"format": "vst3" if vst3 is not None else "vst2" if vst2 is not None else "au", "name": name}
    if vst3 is not None:
        vp = vst3.find("Preset/Vst3Preset")
        if vp is not None:
            out["processor_state"] = "".join((vp.findtext("ProcessorState") or "").split())
            out["controller_state"] = "".join((vp.findtext("ControllerState") or "").split())
    # automatable plugin parameters: Ableton's index -> automation target
    plist = []
    for p in dev.findall("ParameterList/PluginFloatParameter"):
        plist.append({"index": _v(p, "ParameterId", -1, int), "name": _v(p, "ParameterName", ""),
                      "value": _v(p, "ParameterValue/Manual", 0.0, float),
                      "target": (p.find("ParameterValue/AutomationTarget").get("Id")
                                 if p.find("ParameterValue/AutomationTarget") is not None else None)})
    out["parameters"] = plist
    return out


def _simpler(dev, project_dir: Path) -> dict:
    """An Original Simpler: its sample, key range, playback region and envelope."""
    part = dev.find(".//MultiSamplePart")
    sample = None
    if part is not None:
        sample = _sample_path(part.find("SampleRef/FileRef"), project_dir)
    return {
        "sample": sample,
        "root_key": _v(part, "RootKey", 60, int) if part is not None else 60,
        "key_range": (_v(part, "KeyRange/Min", 0, int), _v(part, "KeyRange/Max", 127, int)) if part is not None else (0, 127),
        "volume": _v(part, "Volume", 1.0, float) if part is not None else 1.0,
        "panorama": _v(part, "Panorama", 0.0, float) if part is not None else 0.0,
        "sample_start": _v(part, "SampleStart", 0, float) if part is not None else 0,
        "sample_end": _v(part, "SampleEnd", None, float) if part is not None else None,
        "detune": _v(part, "Detune", 0, float) if part is not None else 0,
        "tune_scale": _v(part, "TuneScale", 100, float) if part is not None else 100,
    }


def _device(dev, project_dir: Path) -> dict:
    d = {"type": dev.tag, "on": _v(dev, "On/Manual", True, bool), "name": _v(dev, "UserName", "") or ""}
    d["params"] = _params(dev)
    on_target = dev.find("On/AutomationTarget")
    d["on_target"] = on_target.get("Id") if on_target is not None else None
    if dev.tag == "PluginDevice":
        d["plugin"] = _plugin(dev)
    elif dev.tag == "OriginalSimpler":
        d["simpler"] = _simpler(dev, project_dir)
    elif dev.tag == "Compressor2":
        sc = dev.find("SideChain")
        if sc is not None and _v(sc, "OnOff/Manual", False, bool):
            tgt = _v(sc, "RoutedInput/Routable/Target", "") or ""
            # "AudioIn/Track.128/PostFxOut" -> listen to track 128 after its effects
            parts = tgt.split("/")
            src = next((x.split(".", 1)[1] for x in parts if x.startswith("Track.")), None)
            d["sidechain_source"] = src
        d["sidechain_eq_on"] = _v(dev, "SideChainEq/On/Manual", False, bool)
    elif dev.tag == "MxDeviceAudioEffect":
        ref = dev.find(".//FileRef")
        d["patch"] = Path(_v(ref, "RelativePath") or _v(ref, "Path") or "").name
    branches = dev.find("Branches")
    if branches is not None:
        d["branches"] = []
        for br in branches:
            mixer = br.find("MixerDevice")
            chain = (br.find("DeviceChain/AudioToAudioDeviceChain") or br.find("DeviceChain/MidiToAudioDeviceChain")
                     or br.find("DeviceChain/MidiToMidiDeviceChain") or br.find("DeviceChain"))
            devs = chain.find("Devices") if chain is not None else None
            zone = br.find("ZoneSettings/KeyRange")
            d["branches"].append({
                "name": _v(br, "Name", ""),
                "volume_db": lin_db(_v(mixer, "Volume/Manual", 1.0, float)),
                "pan": _v(mixer, "Panorama/Manual", 0.0, float),
                "active": _v(mixer, "Speaker/Manual", True, bool) and _v(mixer, "On/Manual", True, bool),
                "solo": _v(br, "IsSoloed", False, bool),
                "key_range": (_v(zone, "Min", 0, int), _v(zone, "Max", 127, int)) if zone is not None else None,
                "volume_target": (mixer.find("Volume/AutomationTarget").get("Id")
                                  if mixer is not None and mixer.find("Volume/AutomationTarget") is not None else None),
                "devices": [_device(x, project_dir) for x in (devs if devs is not None else [])],
            })
        macros = []
        for i in range(16):
            m = dev.find(f"MacroControls.{i}")
            if m is None:
                break
            macros.append({"index": i, "value": _v(m, "Manual", 0.0, float),
                           "target": m.find("AutomationTarget").get("Id") if m.find("AutomationTarget") is not None else None})
        d["macros"] = macros
    return d


def _clips_midi(track) -> list:
    clips = []
    for c in track.findall(".//ArrangerAutomation/Events/MidiClip"):
        start, end = _v(c, "CurrentStart", 0.0, float), _v(c, "CurrentEnd", 0.0, float)
        loop = c.find("Loop")
        lstart, lend = _v(loop, "LoopStart", 0.0, float), _v(loop, "LoopEnd", 0.0, float)
        rel = _v(loop, "StartRelative", 0.0, float)
        looping = _v(loop, "LoopOn", False, bool)
        notes = []
        for kt in c.findall("Notes/KeyTracks/KeyTrack"):
            key = _v(kt, "MidiKey", 60, int)
            for n in kt.findall("Notes/MidiNoteEvent"):
                if n.get("IsEnabled", "true") != "true":
                    continue
                notes.append({"key": key, "time": float(n.get("Time")), "dur": float(n.get("Duration")),
                              "vel": float(n.get("Velocity")) / 127.0})
        clips.append({"start": start, "end": end, "loop_start": lstart, "loop_end": lend, "start_rel": rel,
                      "looping": looping, "disabled": _v(c, "Disabled", False, bool), "notes": notes,
                      "envelopes": _clip_envelopes(c)})
    return clips


def _clip_envelopes(c) -> list:
    out = []
    for env in c.findall("Envelopes/Envelopes/ClipEnvelope"):
        pid = _v(env, "EnvelopeTarget/PointeeId")
        pts = [(float(e.get("Time")), float(e.get("Value"))) for e in env.iter("FloatEvent")]
        out.append({"target": pid, "points": pts})
    return out


def _clips_audio(track, project_dir: Path) -> list:
    clips = []
    for c in track.findall(".//ArrangerAutomation/Events/AudioClip"):
        loop = c.find("Loop")
        warps = [(float(w.get("SecTime")), float(w.get("BeatTime"))) for w in c.findall("WarpMarkers/WarpMarker")]
        fades = c.find("Fades")
        clips.append({
            "start": _v(c, "CurrentStart", 0.0, float), "end": _v(c, "CurrentEnd", 0.0, float),
            "loop_start": _v(loop, "LoopStart", 0.0, float), "loop_end": _v(loop, "LoopEnd", 0.0, float),
            "start_rel": _v(loop, "StartRelative", 0.0, float), "looping": _v(loop, "LoopOn", False, bool),
            "sample": _sample_path(c.find("SampleRef/FileRef"), project_dir),
            "warped": _v(c, "IsWarped", True, bool), "warp_markers": warps,
            "warp_mode": _v(c, "WarpMode", 0, int),
            "pitch": _v(c, "PitchCoarse", 0.0, float) + _v(c, "PitchFine", 0.0, float) / 100.0,
            "gain_db": lin_db(_v(c, "SampleVolume", 1.0, float)),
            "disabled": _v(c, "Disabled", False, bool),
            "fade_in": _v(fades, "FadeInLength", 0.0, float), "fade_out": _v(fades, "FadeOutLength", 0.0, float),
            "envelopes": _clip_envelopes(c),
        })
    return clips


def _envelopes(track) -> list:
    out = []
    for env in track.findall("AutomationEnvelopes/Envelopes/AutomationEnvelope"):
        pid = _v(env, "EnvelopeTarget/PointeeId")
        pts = []
        for e in env.iter():
            if e.tag in ("FloatEvent", "BoolEvent", "EnumEvent"):
                raw = e.get("Value")
                val = (1.0 if raw == "true" else 0.0) if raw in ("true", "false") else float(raw)
                pts.append((float(e.get("Time")), val))
        out.append({"target": pid, "points": pts})
    return out


def _mixer(track) -> dict:
    mix = track.find("DeviceChain/Mixer")
    sends = []
    for i, s in enumerate(mix.findall("Sends/TrackSendHolder")):
        at = s.find("Send/AutomationTarget")
        sends.append({"index": i, "db": lin_db(_v(s, "Send/Manual", 0.0, float)),
                      "active": _v(s, "Active", True, bool), "target": at.get("Id") if at is not None else None})
    vt = mix.find("Volume/AutomationTarget")
    pt = mix.find("Pan/AutomationTarget")
    return {"volume_db": lin_db(_v(mix, "Volume/Manual", 1.0, float)), "pan": _v(mix, "Pan/Manual", 0.0, float),
            "active": _v(mix, "Speaker/Manual", True, bool), "sends": sends,
            "volume_target": vt.get("Id") if vt is not None else None, "pan_target": pt.get("Id") if pt is not None else None}


def _devices_of(track, project_dir) -> list:
    devs = track.find("DeviceChain/DeviceChain/Devices")
    return [_device(d, project_dir) for d in (devs if devs is not None else [])]


def read(path: str | Path) -> dict:
    """The whole set as plain data."""
    path = Path(path)
    data = path.read_bytes()
    root = ET.fromstring(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
    ls = root.find("LiveSet")
    pdir = path.parent
    master = ls.find("MainTrack") if ls.find("MainTrack") is not None else ls.find("MasterTrack")
    tempo = _v(master, "DeviceChain/Mixer/Tempo/Manual", 120.0, float)
    tracks = []
    for t in ls.find("Tracks"):
        kind = {"AudioTrack": "audio", "MidiTrack": "midi", "GroupTrack": "group"}.get(t.tag, t.tag)
        tracks.append({
            "id": t.get("Id"), "kind": kind, "name": _v(t, "Name/EffectiveName", ""),
            "group": _v(t, "TrackGroupId", "-1"), "mixer": _mixer(t), "devices": _devices_of(t, pdir),
            "midi_clips": _clips_midi(t) if kind == "midi" else [],
            "audio_clips": _clips_audio(t, pdir) if kind == "audio" else [],
            "envelopes": _envelopes(t),
        })
    returns = [{"id": t.get("Id"), "kind": "return", "name": _v(t, "Name/EffectiveName", ""), "mixer": _mixer(t),
                "devices": _devices_of(t, pdir), "envelopes": _envelopes(t)}
               for t in (ls.find("ReturnTracks") if ls.find("ReturnTracks") is not None else [])]
    locators = sorted((_v(l, "Time", 0.0, float), _v(l, "Name", "")) for l in ls.findall("Locators/Locators/Locator"))
    ends = [c["end"] for t in tracks for c in t["midi_clips"] + t["audio_clips"] if not c["disabled"]]
    return {"tempo": tempo, "tracks": tracks, "returns": returns,
            "master": {"kind": "master", "name": "Master", "mixer": _mixer(master), "devices": _devices_of(master, pdir),
                       "envelopes": _envelopes(master)},
            "locators": locators, "length_beats": max(ends) if ends else 0.0, "path": str(path)}


def arrangement_notes(clips: list, tempo: float) -> list:
    """Every note a track's arrangement clips play: [(time s, dur s, key, velocity 0-1)], loops unrolled."""
    spb = 60.0 / tempo
    out = []
    for c in clips:
        if c["disabled"]:
            continue
        content0 = c["loop_start"] + c["start_rel"]  # clip time that sounds at the clip's start
        length = c["end"] - c["start"]
        loop = c["loop_end"] - c["loop_start"] if c["looping"] else None
        for n in c["notes"]:
            if loop:
                if not (c["loop_start"] <= n["time"] < c["loop_end"]) and n["time"] >= c["loop_start"]:
                    continue
                k0 = 0
                while True:
                    at = n["time"] - content0 + k0 * loop
                    if at >= length:
                        break
                    if at >= 0 and (k0 == 0 or n["time"] >= c["loop_start"]):
                        dur = min(n["dur"], length - at)
                        out.append(((c["start"] + at) * spb, dur * spb, n["key"], n["vel"]))
                    k0 += 1
            else:
                at = n["time"] - content0
                if 0 <= at < length:
                    out.append(((c["start"] + at) * spb, min(n["dur"], length - at) * spb, n["key"], n["vel"]))
    return sorted(out)
