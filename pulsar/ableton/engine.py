"""Render an imported Ableton Live set: instruments, clips, device chains, automation and the mix graph.

Notes come from the Pulsar song (so they can be edited there); everything else -- which instrument
and devices each track has, their settings and automation, groups, sends, returns and sidechains --
comes from the set as read by pulsar.ableton.als.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from ..errors import PulsarError
from . import devices as D

SR = D.SR


class Automation:
    """Every automation envelope of the set, by automation-target id, as per-sample arrays."""

    def __init__(self, envelopes: list[dict], tempo: float, n: int):
        self.tempo, self.n = tempo, n
        self.by_id = {}
        for e in envelopes:
            if e["target"] and e["points"]:
                self.by_id[e["target"]] = e["points"]

    def curve(self, target: str | None, default: float):
        """A per-sample array if `target` is automated, else `default`."""
        pts = self.by_id.get(target) if target else None
        if not pts:
            return default
        pts = sorted(pts)
        t = np.array([max(0.0, p[0]) * 60.0 / self.tempo for p in pts])
        v = np.array([p[1] for p in pts], dtype=np.float64)
        x = np.arange(self.n) / SR
        return np.interp(x, t, v).astype(np.float32)


def _val(dev, path, default=0.0):
    p = dev["params"].get(path)
    return p["value"] if p is not None else default


class Renderer:
    def __init__(self, live: dict, notes: dict[str, list], seconds: float, log=print, trim: dict | None = None):
        """`notes`: Ableton track name -> [(time s, dur s, midi key, velocity 0-1)] from the Pulsar song."""
        self.live = live
        self.tempo = live["tempo"]
        self.notes = notes
        self.n = int(seconds * SR)
        self.log = log
        envs = [e for t in live["tracks"] + live["returns"] + [live["master"]] for e in t["envelopes"]]
        self.auto = Automation(envs, self.tempo, self.n)
        self.post: dict[str, np.ndarray] = {}  # track id -> post-effects audio (for sidechains)
        self.trim = trim or {}  # track name -> extra fader dB (calibration against a reference)
        self._fx = None

    # -- the graph --------------------------------------------------------------------------

    def render(self) -> np.ndarray:
        tracks = self.live["tracks"]
        by_id = {t["id"]: t for t in tracks}
        children: dict[str, list] = {}
        for t in tracks:
            children.setdefault(t.get("group", "-1"), []).append(t)
        return_ids = [r for r in self.live["returns"]]
        sends = [np.zeros((2, self.n), np.float32) for _ in return_ids]

        # sidechain sources first: whatever a compressor listens to must be rendered before it
        order = self._order(tracks)
        out_of: dict[str, np.ndarray] = {}
        for t in order:
            if t["kind"] == "group":
                continue
            audio = self.track_audio(t)
            out_of[t["id"]] = self.mix_track(t, audio, sends)

        def group_sum(gid: str) -> np.ndarray:
            acc = np.zeros((2, self.n), np.float32)
            for c in children.get(gid, []):
                if c["kind"] == "group":
                    g = self.chain(c, group_sum(c["id"]))
                    self.post[c["id"]] = g
                    acc += self.mix_track(c, g, sends)
                elif c["id"] in out_of and not self._routes_to_track(c):
                    acc += out_of[c["id"]]
            return acc

        master_in = group_sum("-1")
        # returns: their chains, then wherever they're routed (here: an audio track that sums them)
        ret_out = np.zeros((2, self.n), np.float32)
        for i, r in enumerate(self.live["returns"]):
            y = self.chain(r, sends[i])
            ret_out += self.mix_track(r, y, None)
        for t in tracks:  # tracks fed by the returns ("Sends" bus tracks with TrackIn routing)
            if t["kind"] == "audio" and not t["audio_clips"] and t["devices"]:
                y = self.chain(t, ret_out)
                master_in += self.mix_track(t, y, None)
                ret_out = None
                break
        if ret_out is not None:
            master_in += ret_out
        m = self.chain(self.live["master"], master_in)
        return self.fader(self.live["master"], m)

    def _routes_to_track(self, t) -> bool:
        return False

    def _order(self, tracks):
        """Render order: sidechain sources (tracks a compressor listens to) before the rest."""
        sources = set()
        for t in tracks + self.live["returns"]:
            for d in self._walk(t["devices"]):
                if d["type"] == "Compressor2" and d.get("sidechain_source"):
                    sources.add(d["sidechain_source"])
        first = [t for t in tracks if t["id"] in sources]
        return first + [t for t in tracks if t["id"] not in sources]

    def _walk(self, devs):
        for d in devs:
            yield d
            for b in d.get("branches", []):
                yield from self._walk(b["devices"])

    # -- mixer ---------------------------------------------------------------------------------

    def fader(self, t, audio):
        mix = t["mixer"]
        vol = self.auto.curve(mix.get("volume_target"), None)
        g = D.lin(mix["volume_db"]) if vol is None else vol  # automated volume is stored linear
        g = g * D.lin(self.trim.get(t.get("name", ""), 0.0))
        pan = self.auto.curve(mix.get("pan_target"), mix["pan"])
        pan = np.asarray(pan, dtype=np.float32)
        lg = np.minimum(1, 1 - pan) * g
        rg = np.minimum(1, 1 + pan) * g
        return np.stack([audio[0] * lg, audio[1] * rg]).astype(np.float32)

    def mix_track(self, t, audio, sends):
        if not t["mixer"].get("active", True):
            return np.zeros_like(audio)
        out = self.fader(t, audio)
        if sends is not None:
            for s in t["mixer"]["sends"]:
                if s["index"] < len(sends) and s["active"]:
                    lvl = self.auto.curve(s.get("target"), None)
                    g = D.lin(s["db"]) if lvl is None else lvl
                    if np.max(g) > 1e-4:
                        sends[s["index"]] += out * g
        return out

    # -- instruments and clips ----------------------------------------------------------------

    def track_audio(self, t) -> np.ndarray:
        if t["kind"] == "audio":
            x = self.audio_clips(t)
        else:
            x = self.instrument(t["devices"], self.notes.get(t["name"], []), t)
        self.post[t["id"]] = None
        y = self.chain(t, x)
        self.post[t["id"]] = y
        return y

    def audio_clips(self, t) -> np.ndarray:
        import soundfile as sf
        from math import gcd

        from scipy.signal import resample_poly
        out = np.zeros((2, self.n), np.float32)
        spb = 60.0 / self.tempo
        for c in t["audio_clips"]:
            if c["disabled"] or not c["sample"] or not Path(c["sample"]).exists():
                continue
            x, sr = sf.read(c["sample"], always_2d=True, dtype="float32")
            x = x.T if x.shape[1] <= 2 else x.T[:2]
            if x.shape[0] == 1:
                x = np.repeat(x, 2, axis=0)
            if sr != SR:
                g = gcd(SR, sr)
                x = resample_poly(x, SR // g, sr // g, axis=1).astype(np.float32)
            # where in the sample the clip starts: warped clips count in beats, unwarped in seconds
            unit = spb if c["warped"] else 1.0
            offset = (c["loop_start"] + c["start_rel"]) * unit
            length = (c["end"] - c["start"]) * spb
            s0 = int(round(c["start"] * spb * SR))
            seg_n = int(round(length * SR))
            if c["looping"] and c["loop_end"] > c["loop_start"]:
                a, b = int(c["loop_start"] * unit * SR), int(c["loop_end"] * unit * SR)
                body = x[:, a:b]
                first = x[:, int(offset * SR):b]
                reps = int(math.ceil((seg_n - first.shape[1]) / max(body.shape[1], 1))) + 1
                seg = np.concatenate([first] + [body] * max(reps, 0), axis=1)[:, :seg_n]
            else:
                seg = x[:, int(offset * SR):int(offset * SR) + seg_n]
            seg = seg * D.lin(c["gain_db"])
            for k, fl in (("fade_in", 1), ("fade_out", -1)):
                f = int(c[k] * spb * SR) if c["warped"] else int(c[k] * SR)
                f = min(f, seg.shape[1])
                if f > 1:
                    ramp = np.linspace(0, 1, f, dtype=np.float32)
                    if fl > 0:
                        seg[:, :f] *= ramp
                    else:
                        seg[:, -f:] *= ramp[::-1]
            # short de-click at clip edges, like Live's
            e = min(96, seg.shape[1] // 2)
            if e > 1:
                seg[:, :e] *= np.linspace(0, 1, e)
                seg[:, -e:] *= np.linspace(1, 0, e)
            e0 = min(self.n, s0 + seg.shape[1])
            if e0 > s0:
                out[:, s0:e0] += seg[:, :e0 - s0]
        return out

    def instrument(self, devs, notes, t, macros=None) -> np.ndarray:
        """The first instrument in a chain (plugin, Simpler, or an instrument rack) plays the notes."""
        out = np.zeros((2, self.n), np.float32)
        for i, d in enumerate(devs):
            if d["type"] == "PluginDevice":
                return self.plugin(d, notes)
            if d["type"] == "OriginalSimpler":
                return self.simpler(d, notes)
            if d["type"] == "InstrumentGroupDevice":
                for b in d.get("branches", []):
                    if not b["active"]:
                        continue
                    lo, hi = b["key_range"] or (0, 127)
                    sub = [nt for nt in notes if lo <= nt[2] <= hi]
                    y = self.instrument(b["devices"], sub, t, self._macros(d, macros))
                    y = self.chain({"devices": self._after_instrument(b["devices"]), "id": None}, y,
                                   macros=self._macros(d, macros))
                    g = D.lin(b["volume_db"])
                    out += np.stack([y[0] * g * min(1, 1 - b["pan"]), y[1] * g * min(1, 1 + b["pan"])])
                return out
        return out

    def _after_instrument(self, devs):
        for i, d in enumerate(devs):
            if d["type"] in ("PluginDevice", "OriginalSimpler", "InstrumentGroupDevice"):
                return devs[i + 1:]
        return devs

    def plugin(self, d, notes) -> np.ndarray:
        import dawdreamer as daw

        from .. import plugins
        p = d["plugin"]
        path = plugins.locate(p["name"].lower().replace(" ", "")) or plugins.locate(p["name"])
        if path is None:
            raise PulsarError(f"this set needs the plugin {p['name']!r}, which isn't installed")
        eng = daw.RenderEngine(SR, 512)
        proc = eng.make_plugin_processor(f"pl{id(d)}", str(path))
        state = plugin_state_file(p)
        if state:
            proc.load_state(str(state))
        for prm in p["parameters"]:
            curve = self.auto.curve(prm.get("target"), None)
            if curve is not None and 0 <= prm["index"] < proc.get_plugin_parameter_size():
                curve = normalized(proc, prm["index"], curve)
                lead = int(0.25 * SR)
                proc.set_automation(prm["index"], np.concatenate([np.full(lead, curve[0], np.float32), curve]))
        pre = 0.25
        for (t0, dur, key, vel) in notes:
            proc.add_midi_note(int(key), int(min(127, max(1, round(vel * 127)))), pre + t0, max(0.005, dur))
        eng.load_graph([(proc, [])])
        eng.render(pre + self.n / SR + 2.0)
        a = eng.get_audio()[:2, int(pre * SR):int(pre * SR) + self.n].astype(np.float32)
        out = np.zeros((2, self.n), np.float32)
        out[:, :a.shape[1]] = a
        return out

    def simpler(self, d, notes) -> np.ndarray:
        import soundfile as sf
        from fractions import Fraction

        from scipy.signal import resample_poly
        s = d["simpler"]
        out = np.zeros((2, self.n), np.float32)
        if not s["sample"] or not Path(s["sample"]).exists():
            return out
        x, sr = sf.read(s["sample"], always_2d=True, dtype="float32")
        x = x.T[:2] if x.shape[1] >= 2 else np.repeat(x.T, 2, axis=0)
        p = d["params"]
        start = float(_val(d, "Player/SampleStart", _val(d, "SampleStart", 0)) or 0)
        length = float(_val(d, "Player/SampleLength", _val(d, "SampleLength", 1)) or 1)
        a0 = int(start * x.shape[1]) if start <= 1 else int(start)
        a1 = a0 + int(length * x.shape[1]) if length <= 1 else a0 + int(length)
        x = x[:, a0:max(a0 + 1, a1)]
        env = {k: float(_val(d, f"VolumeAndPan/Envelope/{k}", dflt)) for k, dflt in
               (("AttackTime", 0.1), ("DecayTime", 600), ("SustainLevel", 1.0), ("ReleaseTime", 50))}
        vol = float(_val(d, "VolumeAndPan/Volume", 0.0))
        vel_scale = float(_val(d, "VolumeAndPan/VolumeVelScale", 0.35))
        transpose = float(_val(d, "Pitch/TransposeKey", 0)) + float(_val(d, "Pitch/TransposeFine", 0)) / 100
        root = s["root_key"]
        cache = {}
        for (t0, dur, key, vel) in notes:
            semis = key - root + transpose + s["detune"] / 100
            ratio = Fraction(sr / SR * 2 ** (semis / 12)).limit_denominator(1000)
            y = cache.get(ratio)
            if y is None:
                y = x if ratio == 1 else resample_poly(x, ratio.denominator, ratio.numerator, axis=1).astype(np.float32)
                cache[ratio] = y
            n = y.shape[1]
            tt = np.arange(n) / SR
            att, dec, sus, rel = env["AttackTime"] / 1000, env["DecayTime"] / 1000, env["SustainLevel"], env["ReleaseTime"] / 1000
            e = np.where(tt < att, tt / max(att, 1e-4), sus + (1 - sus) * np.exp(-(tt - att) / max(dec / 5, 1e-4)))
            off = int(dur * SR)
            if off < n:
                lvl = e[off]
                e[off:] = lvl * np.exp(-(tt[off:] - dur) / max(rel / 5, 1e-4))
            g = D.lin(vol) * s["volume"] * (1 - vel_scale + vel_scale * vel)
            s0 = int(round(t0 * SR))
            e0 = min(self.n, s0 + n)
            if e0 > s0:
                out[:, s0:e0] += (y[:, :e0 - s0] * e[:e0 - s0] * g).astype(np.float32)
        return out

    # -- device chains --------------------------------------------------------------------------

    @property
    def fx(self):
        if self._fx is None:
            from ..dsp.engine import Engine
            self._fx = Engine(SR)
        return self._fx

    def _macros(self, rack, outer=None) -> list:
        """Each macro of a rack (0-127): automated, driven by a macro of the enclosing rack, or fixed."""
        out = []
        for m in rack.get("macros", []):
            if f"MacroControls.{m['index']}" in rack["params"]:
                out.append(self.value(rack, f"MacroControls.{m['index']}", m["value"], outer))
            else:
                out.append(self.auto.curve(m.get("target"), m["value"]))
        return out

    def value(self, d, path, default=0.0, macros=None):
        """A device parameter: automated, driven by a macro of the enclosing rack, or fixed."""
        p = d["params"].get(path)
        if p is None:
            return default
        v = p["value"]
        if isinstance(v, bool):
            v = 1.0 if v else 0.0
        if p.get("target") and p["target"] in self.auto.by_id:
            return self.auto.curve(p["target"], v)
        m = p.get("macro")
        if m and macros and m["index"] < len(macros):
            frac = np.asarray(macros[m["index"]], dtype=np.float32) / 127.0
            if m["min"] is None or m["max"] is None:
                # an on/off switch follows its macro: on in the macro's upper half
                return (frac >= 0.5).astype(np.float32) if frac.ndim else float(frac >= 0.5)
            lo, hi = m["min"], m["max"]
            # Live sweeps gains, thresholds, frequencies and times in their own (logarithmic) scale
            if lo > 0 and hi > 0 and LOG_PARAM.search(path.rsplit("/", 1)[-1]):
                return lo * (hi / lo) ** frac
            return lo + (hi - lo) * frac
        return v

    def chain(self, t, x, macros=None) -> np.ndarray:
        devs = t["devices"] if "devices" in t else []
        for d in devs:
            if d["type"] in ("PluginDevice", "OriginalSimpler", "InstrumentGroupDevice"):
                continue  # instruments made the input already
            on = self.auto.curve(d.get("on_target"), 1.0 if d["on"] else 0.0)
            if not np.any(np.asarray(on) > 0.5):
                continue
            y = self.device(d, x, t, macros)
            if is_partial(on):
                o = np.asarray(on, dtype=np.float32) > 0.5
                y = np.where(o, y, x)
            x = y
        return x

    def device(self, d, x, track, macros) -> np.ndarray:
        kind = d["type"]
        v = lambda path, default=0.0: self.value(d, path, default, macros)
        if kind == "AudioEffectGroupDevice":
            ms = self._macros(d, macros)
            acc = np.zeros_like(x)
            active = [b for b in d.get("branches", []) if b["active"]]
            if any(b["solo"] for b in active):
                active = [b for b in active if b["solo"]]
            if not active:
                return x
            for b in active:
                y = self.chain({"devices": b["devices"], "id": None}, x, macros=ms)
                g = D.lin(b["volume_db"])
                acc += np.stack([y[0] * g * min(1, 1 - b["pan"]), y[1] * g * min(1, 1 + b["pan"])])
            return acc
        if kind == "Eq8":
            bands = []
            params, auto = {}, {}
            scale = v("Scale", 1.0)
            for i in range(8):
                pre = f"Bands.{i}/ParameterA/"
                if not self.const(v(pre + "IsOn", False)):
                    continue
                j = len(bands)
                bands.append({"mode": int(self.const(v(pre + "Mode", 3)))})
                for key, name in (("Freq", f"f{j}"), ("Q", f"q{j}")):
                    self._set(params, auto, name, v(pre + key, 1000 if key == "Freq" else 0.71))
                self._set(params, auto, f"g{j}", np.asarray(v(pre + "Gain", 0.0)) * np.asarray(scale))
            if not bands:
                return x * D.lin(self.const(v("GlobalGain", 0.0)))
            self._set(params, auto, "out", v("GlobalGain", 0.0))
            return self.fx.process(D.eq8_code(bands), x, params, auto)
        if kind == "StereoGain":
            params, auto = {}, {}
            g = np.asarray(v("Gain", 1.0)) * D.lin(np.asarray(v("LegacyGain", 0.0)))
            self._set(params, auto, "gain", g)
            self._set(params, auto, "width", v("StereoWidth", 1.0))
            for src, dst in (("Mono", "mono"), ("BassMono", "bassmono"), ("BassMonoFrequency", "bassf"),
                             ("Balance", "bal"), ("PhaseInvertL", "invl"), ("PhaseInvertR", "invr"),
                             ("ChannelMode", "chmode"), ("Mute", "mute")):
                self._set(params, auto, dst, v(src, {"BassMonoFrequency": 120, "ChannelMode": 1}.get(src, 0)))
            return self.fx.process(D.UTILITY, x, params, auto)
        if kind == "Compressor2":
            return self.compressor(d, x, v)
        if kind == "Saturator":
            params, auto = {}, {}
            self._set(params, auto, "type", v("Type", 0))
            self._set(params, auto, "drive", v("PreDrive", 0.0))
            self._set(params, auto, "out", v("PostDrive", 0.0))
            self._set(params, auto, "mix", v("DryWet", 1.0))
            return self.fx.process(D.SATURATOR, x, params, auto)
        if kind == "Delay":
            return self.delay(x, v)
        if kind == "Echo":
            return self.echo(x, v)
        if kind == "Reverb":
            return self.reverb(x, v)
        if kind == "AutoFilter":
            params, auto = {}, {}
            self._set(params, auto, "ftype", v("FilterType", v("LegacyFilterType", 0)))
            self._set(params, auto, "cut", v("Cutoff", 100))
            self._set(params, auto, "res", v("Resonance", 0.1))
            self._set(params, auto, "slope24", v("Slope", False))
            self._set(params, auto, "lfoamt", v("Lfo/LfoAmount", v("LfoAmount", 0.0)))
            self._set(params, auto, "lforate", v("Lfo/Frequency", v("Frequency", 1.0)))
            self._set(params, auto, "drive", v("Drive", 0.0))
            self._set(params, auto, "mix", v("DryWet", 1.0))
            return self.fx.process(D.AUTOFILTER, x, params, auto)
        if kind == "AutoPan":
            params, auto = {}, {}
            rate = v("Lfo/Frequency", v("Frequency", 1.0))
            if self.const(v("Lfo/RateType", v("RateType", 0))) == 1:
                rate = self.tempo / 60 / BEAT_RATES.get(int(self.const(v("Lfo/BeatRate", v("BeatRate", 4)))), 1.0)
            self._set(params, auto, "amt", v("Lfo/LfoAmount", v("LfoAmount", 0.0)))
            self._set(params, auto, "rate", rate)
            self._set(params, auto, "phase", v("Lfo/Phase", v("Phase", 180)))
            return self.fx.process(D.AUTOPAN, x, params, auto)
        if kind == "Chorus":
            params, auto = {}, {}
            for src, dst in (("DelayTime1", "d1"), ("DelayTime2", "d2"), ("ModAmount", "amt"), ("ModFreq", "rate"),
                             ("HighPassFreq", "hpf"), ("DryWet", "mix"), ("Feedback", "fb")):
                self._set(params, auto, dst, v(src, 0))
            return self.fx.process(D.CHORUS, x, params, auto)
        if kind == "Limiter":
            params, auto = {}, {}
            self._set(params, auto, "gain", v("Gain", 0.0))
            self._set(params, auto, "ceil", v("Ceiling", -0.3))
            self._set(params, auto, "rel", v("Release", 300))
            return self.fx.process(D.LIMITER, x, params, auto, tail=0)
        if kind == "MultibandDynamics":
            return self.multiband(x, v)
        if kind == "Redux2":
            params, auto = {}, {}
            self._set(params, auto, "rate", v("SampleRate", 44100))
            self._set(params, auto, "bits", v("BitDepth", 16))
            self._set(params, auto, "mix", v("DryWet", 1.0))
            return self.fx.process(D.REDUX, x, params, auto)
        if kind == "Erosion":
            params, auto = {}, {}
            for src, dst in (("Freq", "freq"), ("Amplitude", "amp"), ("BandQ", "bq"), ("Mode", "mode")):
                self._set(params, auto, dst, v(src, 0))
            return self.fx.process(D.EROSION, x, params, auto)
        if kind == "Vinyl":
            return D.vinyl(x, {k: self.const(p["value"]) if not isinstance(p["value"], bool) else p["value"]
                               for k, p in d["params"].items()})
        if kind == "Shifter":
            hz = self.const(v("ModBasedShifting_FShift_Coarse", 0.0)) + self.const(v("ModBasedShifting_Fine", 0.0))
            mode = self.const(v("Mode", v("ShifterMode", 1)))
            return D.shifter(x, hz if mode != 0 else 0.0, self.const(v("DryWet", 1.0)))
        # unknown devices (and Max for Live modulators, which make no sound) pass audio through
        return x

    # -- device details -----------------------------------------------------------------------

    @staticmethod
    def const(val):
        return D.const(val) if not isinstance(val, bool) else (1.0 if val else 0.0)

    @staticmethod
    def _set(params, auto, name, val):
        if isinstance(val, bool):
            val = 1.0 if val else 0.0
        if D.is_array(np.asarray(val)) and float(np.ptp(np.asarray(val))) > 1e-9:
            auto[name] = np.asarray(val, dtype=np.float32)
            params[name] = float(np.asarray(val)[0])
        else:
            params[name] = D.const(np.asarray(val, dtype=np.float64))

    def compressor(self, d, x, v):
        params, auto = {}, {}
        thr = v("Threshold", 1.0)
        self._set(params, auto, "thr", D.db(np.asarray(thr)) if not np.isscalar(thr) else float(D.db(thr)))
        ratio = self.const(v("Ratio", 4))
        params["ratio"] = min(ratio, 1000.0)
        self._set(params, auto, "knee", v("Knee", 0.0))
        self._set(params, auto, "att", v("Attack", 1.0))
        self._set(params, auto, "rel", v("Release", 50))
        self._set(params, auto, "makeup", v("Gain", 0.0))
        self._set(params, auto, "mix", v("DryWet", 1.0))
        params["rms"] = 1.0 if self.const(v("Model", 0)) == 1 else 0.0
        sc = np.zeros_like(x)
        src = d.get("sidechain_source")
        if src and self.post.get(src) is not None:
            sc = self.post[src]
            params["ext"] = 1.0
        if d.get("sidechain_eq_on"):
            params["scmode"] = float(SC_EQ_MODES.get(int(self.const(v("SideChainEq/Mode", 5))), 3))
            self._set(params, auto, "scf", v("SideChainEq/Freq", 100))
            self._set(params, auto, "scg", v("SideChainEq/Gain", 0.0))
            self._set(params, auto, "scq", v("SideChainEq/Q", 0.71))
        return self.process4(D.COMPRESSOR, x, sc, params, auto)

    def process4(self, code, x, sc, params, auto):
        """A four-input DSP (stereo + stereo sidechain)."""
        import dawdreamer as daw
        eng = daw.RenderEngine(SR, 512)
        proc = eng.make_faust_processor("c4")
        proc.set_dsp_string(code)
        if not proc.compile():
            raise PulsarError("internal DSP compile error (compressor)")
        names = {p["name"].split("/")[-1]: p["name"] for p in proc.get_parameters_description()}
        for k, val in params.items():
            if k in names:
                proc.set_parameter(names[k], float(val))
        for k, arr in auto.items():
            if k in names:
                proc.set_automation(names[k], np.ascontiguousarray(arr, dtype=np.float32))
        a = eng.make_playback_processor("a", np.ascontiguousarray(x, dtype=np.float32))
        b = eng.make_playback_processor("b", np.ascontiguousarray(sc, dtype=np.float32))
        eng.load_graph([(a, []), (b, []), (proc, ["a", "b"])])
        eng.render(x.shape[1] / SR)
        y = eng.get_audio().astype(np.float32)[:, :x.shape[1]]
        if y.shape[1] < x.shape[1]:
            y = np.concatenate([y, np.zeros((2, x.shape[1] - y.shape[1]), np.float32)], axis=1)
        return y

    def delay(self, x, v):
        params, auto = {}, {}
        times = []
        for side in ("L", "R"):
            if self.const(v(f"DelayLine_Sync{side}", False)) > 0.5:
                six = DELAY_SIXTEENTHS_AT(int(self.const(v(f"DelayLine_SyncedSixteenth{side}", 2))))
                off = self.const(v(f"DelayLine_Offset{side}", 0.0))
                times.append(D.sync_seconds(six, self.tempo) * (1 + off))
            else:
                times.append(v(f"DelayLine_Time{side}", 0.25))
        if self.const(v("DelayLine_Link", True)) > 0.5:
            times[1] = times[0]
        self._set(params, auto, "tl", times[0])
        self._set(params, auto, "tr", times[1])
        self._set(params, auto, "fb", v("Feedback", 0.0))
        self._set(params, auto, "mix", v("DryWet", 1.0))
        self._set(params, auto, "pingpong", v("DelayLine_PingPong", False))
        self._set(params, auto, "filt", v("Filter_On", False))
        self._set(params, auto, "ff", v("Filter_Frequency", 1000))
        self._set(params, auto, "bw", v("Filter_Bandwidth", 8))
        return self.fx.process(D.DELAY, x, params, auto)

    def echo(self, x, v):
        params, auto = {}, {}
        times = []
        for side in ("L", "R"):
            if self.const(v(f"Delay_Sync{side}", True)) > 0.5:
                div = D.ECHO_DIVISIONS.get(int(self.const(v(f"Delay_SyncedDivision{side}", -3))), 1 / 8)
                mult = max(1, self.const(v(f"Delay_SyncedSixteenth{side}", 1)))
                times.append(div * 4 * mult * 60 / self.tempo)
            else:
                times.append(v(f"Delay_Time{side}", 0.25))
        if self.const(v("Delay_TimeLink", True)) > 0.5:
            times[1] = times[0]
        self._set(params, auto, "tl", times[0])
        self._set(params, auto, "tr", times[1])
        self._set(params, auto, "fb", v("Feedback", 0.5))
        self._set(params, auto, "mix", v("DryWet", 0.5))
        params["pingpong"] = 1.0 if self.const(v("ChannelMode", 0)) == 1 else 0.0
        params["filt"] = 1.0 if self.const(v("Filter_On", True)) > 0.5 else 0.0
        lo = self.const(v("Filter_HighPassFrequency", 100))
        hi = self.const(v("Filter_LowPassFrequency", 8000))
        params["ff"] = math.sqrt(lo * hi)
        params["bw"] = max(0.5, math.log2(max(hi / lo, 1.01)))
        y = self.fx.process(D.DELAY, x, params, auto)
        out = np.asarray(v("OutputGain", 1.0))
        return y * (out if np.isscalar(out) or out.ndim == 0 else out[None, :])

    def reverb(self, x, v):
        params, auto = {}, {}
        self._set(params, auto, "pre", v("PreDelay", 10))
        self._set(params, auto, "t60", np.asarray(v("DecayTime", 1200)) / 1000)
        params["damp"] = self.const(v("ShelfHiFreq", 6000)) if self.const(v("ShelfHighOn", False)) else 18000.0
        bf, bw = self.const(v("BandFreq", 1000)), self.const(v("BandWidth", 6))
        params["lowcut"] = bf / 2 ** (bw / 2) if self.const(v("BandLowOn", False)) else 20.0
        params["highcut"] = min(20000.0, bf * 2 ** (bw / 2)) if self.const(v("BandHighOn", False)) else 20000.0
        self._set(params, auto, "wet", v("MixDirect", 0.3))
        refl, diff = self.const(v("MixReflect", 1.0)), self.const(v("MixDiffuse", 1.0))
        params["level"] = (refl + diff) / 2
        params["width"] = min(1.0, self.const(v("StereoSeparation", 100)) / 100)
        return self.fx.process(D.REVERB, x, params, auto)

    def multiband(self, x, v):
        params, auto = {}, {}
        self._set(params, auto, "lowmid", v("SplitLowMid", 120))
        self._set(params, auto, "midhigh", v("SplitMidHigh", 2500))
        self._set(params, auto, "amount", v("GlobalAmount", 1.0))
        self._set(params, auto, "tscale", v("GlobalTime", 1.0))
        self._set(params, auto, "outg", v("OutputGain", 0.0))
        params["rms"] = 0.0 if self.const(v("EnvelopeIsPeak", False)) > 0.5 else 1.0
        for i, b in enumerate(("Low", "Mid", "High")):
            self._set(params, auto, f"in{i}", v(f"InputGain{b}", 0.0))
            self._set(params, auto, f"athr{i}", v(f"AboveThreshold{b}", 0.0))
            # Live stores the slope change: -0.985 above the threshold keeps 1.5% of the overshoot
            self._set(params, auto, f"aslope{i}", v(f"AboveRatio{b}", 0.0))
            self._set(params, auto, f"bthr{i}", v(f"BelowThreshold{b}", -80.0))
            self._set(params, auto, f"bslope{i}", v(f"BelowRatio{b}", 0.0))
            self._set(params, auto, f"att{i}", v(f"Attack{b}", 10))
            self._set(params, auto, f"rel{i}", v(f"Release{b}", 100))
            self._set(params, auto, f"gain{i}", v(f"Gain{b}", 0.0))
            self._set(params, auto, f"on{i}", v(f"Active{b}", True))
        return self.fx.process(D.MULTIBAND, x, params, auto)


import re as _re

LOG_PARAM = _re.compile(r"(Gain|Volume|Threshold|Freq|Frequency|Cutoff|Time|Attack|Release|Decay|Ceiling)$")


def normalized(proc, index: int, curve: np.ndarray) -> np.ndarray:
    """Plugin automation as the host needs it (0-1). Live can store a VST3 parameter in its display
    units (a mod wheel 0-100): read the display values at the ends of the range and rescale."""
    curve = np.asarray(curve, dtype=np.float32)
    if float(np.max(curve)) <= 1.0 and float(np.min(curve)) >= 0.0:
        return curve
    def shown(x):
        proc.set_parameter(index, x)
        m = _re.search(r"-?\d+(\.\d+)?", proc.get_parameter_text(index) or "")
        return float(m.group(0)) if m else None
    keep = proc.get_parameter(index)
    lo, hi = shown(0.0), shown(1.0)
    proc.set_parameter(index, keep)
    if lo is None or hi is None or hi == lo:
        lo, hi = 0.0, 127.0 if float(np.max(curve)) > 100 else 100.0
    return np.clip((curve - lo) / (hi - lo), 0, 1).astype(np.float32)


def is_partial(on) -> bool:
    return D.is_array(np.asarray(on)) and float(np.ptp(np.asarray(on))) > 0


def DELAY_SIXTEENTHS_AT(i: int) -> float:
    return D.DELAY_SIXTEENTHS[max(0, min(len(D.DELAY_SIXTEENTHS) - 1, i))]


# Live's synced LFO rates (Auto Pan / Auto Filter BeatRate index -> length in beats)
BEAT_RATES = {i: b for i, b in enumerate([1 / 16, 1 / 12, 1 / 8, 1 / 6, 3 / 16, 1 / 4, 5 / 16, 1 / 3, 3 / 8, 1 / 2,
                                          3 / 4, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64])}
# Compressor sidechain EQ: Live mode -> our band mode (LP, BP, HP, notch, peak, low shelf, high shelf)
SC_EQ_MODES = {0: 6, 1: 3, 2: 1, 3: 4, 4: 3, 5: 2, 6: 5}


def plugin_state_file(p: dict) -> Path | None:
    """The plugin's saved VST3 state from the set, written as a state file DawDreamer can load."""
    import hashlib
    import struct

    from .. import plugins
    proc, ctrl = p.get("processor_state"), p.get("controller_state")
    if not proc:
        return None
    pb, cb = bytes.fromhex(proc), bytes.fromhex(ctrl or "")
    out = plugins.ROOT / "states" / (hashlib.sha1(pb + cb).hexdigest()[:16] + ".vst3state")
    if not out.exists():
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n\n<VST3PluginState><IComponent>' + plugins.juce_b64encode(pb)
               + "</IComponent>" + (f"<IEditController>{plugins.juce_b64encode(cb)}</IEditController>" if cb else "")
               + "</VST3PluginState>").encode() + b"\0"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"VC2!" + struct.pack("<I", len(xml) - 1) + xml)
    return out
