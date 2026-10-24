"""Thin wrapper over DawDreamer: compile Faust once, render many notes."""
from __future__ import annotations

import hashlib

import dawdreamer as daw
import numpy as np

from ..errors import PulsarError

INTERNAL_SR = 96000
BLOCK = 64


class Engine:
    def __init__(self, sr: int = INTERNAL_SR):
        self.sr = sr
        self._engs: dict[int, object] = {}
        self._pbs: dict[int, object] = {}
        self._defaults: dict[int, dict] = {}
        self._procs: dict[str, object] = {}
        self._names: dict[int, dict] = {}
        self._cache: dict = {}

    def processor(self, code: str):
        key = hashlib.sha1(code.encode()).hexdigest()[:16]
        proc = self._procs.get(key)
        if proc is None:
            eng = daw.RenderEngine(self.sr, BLOCK)
            proc = eng.make_faust_processor(f"p{key}")
            proc.set_dsp_string(code)
            try:
                ok = proc.compile()
            except RuntimeError as e:
                raise PulsarError(f"internal DSP compile error: {e}") from None
            if not ok:
                raise PulsarError("internal DSP compile error")
            desc = proc.get_parameters_description()
            self._names[id(proc)] = {p["name"].split("/")[-1]: p["name"] for p in desc}
            self._defaults[id(proc)] = {p["name"].split("/")[-1]: float(p["value"]) for p in desc}
            self._procs[key] = proc
            self._engs[id(proc)] = eng
        return proc

    def _apply(self, proc, params: dict):
        """Set every parameter: the given values, defaults for the rest (a
        processor keeps its parameter values between renders)."""
        names = self._names[id(proc)]
        for k, d in self._defaults[id(proc)].items():
            proc.set_parameter(names[k], float(params.get(k, d)))

    def render(self, code: str, params: dict, seconds: float, automation: dict | None = None,
               cache: bool = False, trim: bool = True) -> np.ndarray:
        """Render one voice for `seconds`; returns float32 (2, n)."""
        key = None
        if cache and not automation:
            key = (code, tuple(sorted((k, round(float(v), 5)) for k, v in params.items())), round(seconds, 4))
            hit = self._cache.get(key)
            if hit is not None:
                return hit
        proc = self.processor(code)
        names = self._names[id(proc)]
        self._apply(proc, params)
        if automation:
            for k, arr in automation.items():
                if k in names:
                    proc.set_automation(names[k], np.ascontiguousarray(arr, dtype=np.float32))
        eng = self._engs[id(proc)]
        eng.load_graph([(proc, [])])
        eng.render(seconds)
        out = eng.get_audio().astype(np.float32)
        if automation:
            for k in automation:
                if k in names:
                    proc.set_automation(names[k], np.zeros(1, np.float32) + float(params.get(k, 0)))
        if trim:
            out = trim_tail(out)
        if key is not None:
            self._cache[key] = out
        return out

    def process(self, code: str, audio: np.ndarray, params: dict | None = None,
                automation: dict | None = None, tail: float = 0.0) -> np.ndarray:
        """Run stereo `audio` through an effect DSP."""
        n = audio.shape[1] + int(tail * self.sr)
        if tail:
            audio = np.concatenate([audio, np.zeros((2, n - audio.shape[1]), np.float32)], axis=1)
        proc = self.processor(code)
        eng = self._engs[id(proc)]
        data = np.ascontiguousarray(audio, dtype=np.float32)
        pb = self._pbs.get(id(proc))
        if pb is None:
            pb = self._pbs[id(proc)] = eng.make_playback_processor("src", data)
        else:
            pb.set_data(data)
        names = self._names[id(proc)]
        self._apply(proc, params or {})
        for k, arr in (automation or {}).items():
            if k in names:
                proc.set_automation(names[k], np.ascontiguousarray(arr, dtype=np.float32))
        eng.load_graph([(pb, []), (proc, ["src"])])
        eng.render(n / self.sr)
        out = eng.get_audio().astype(np.float32)[:, :n]
        if out.shape[1] < n:  # the engine renders whole seconds-rounded lengths
            out = np.concatenate([out, np.zeros((2, n - out.shape[1]), np.float32)], axis=1)
        for k in (automation or {}):
            if k in names:
                proc.set_automation(names[k], np.zeros(1, np.float32) + float((params or {}).get(k, 0)))
        return out


def trim_tail(x: np.ndarray, thresh: float = 1e-5) -> np.ndarray:
    a = np.abs(x).max(axis=0)
    idx = np.nonzero(a > thresh)[0]
    if len(idx) == 0:
        return x[:, :1]
    end = min(x.shape[1], idx[-1] + 64)
    return x[:, :end]
