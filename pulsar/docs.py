"""The language reference plus tables generated from the code, so they never drift."""
from __future__ import annotations

from pathlib import Path

from . import presets
from .dsp import effects as FX
from .dsp import faust as FZ


def appendix() -> str:
    out = ["", "## Synth presets", "",
           "`fader` is the preset's built-in level; a track's `gain:` is added to it.", "",
           "| preset | engine | octave | fader dB | sound |", "|---|---|---|---|---|"]
    for name, p in presets.SYNTHS.items():
        out.append(f"| `{name}` | {p['engine']} | {p.get('octave', 4)} | {p.get('gain', -8):g} | {p['desc']} |")
    out += ["", "## Drum kits", "", "| kit | sound |", "|---|---|"]
    for name, k in presets.KITS.items():
        out.append(f"| `{name}` | {k['desc']} |")
    out += ["", "## Engine parameters", "",
            "Pass any of these to `synth(\"preset\", ...)` of the matching engine (name default [min, max]).", ""]
    for engine, table in (("analog", FZ.ANALOG_PARAMS), ("fm", FZ.FM_PARAMS), ("pluck", FZ.PLUCK_PARAMS)):
        out.append(f"**{engine}**: " + ", ".join(f"`{k}` {d:g} [{lo:g}, {hi:g}]" for k, (d, lo, hi) in table.items()))
        out.append("")
    out.append("Analog notes: `cutoff` is 0-1 on a log scale (0.5 = 630 Hz); `env_amount` is in octaves; times are "
               "seconds; `rise` (semitones), `sweep` (octaves), `swell` and `fade` follow the note length.")
    out += ["", "## Drum voice parameters", "", "Use as `drums(\"909\", voice_param: value)`; every voice also takes "
            "`voice_gain` in dB.", ""]
    for v, table in FZ.DRUM_PARAMS.items():
        out.append(f"* `{v}`: " + ", ".join(f"{k} {d:g}" for k, (d, lo, hi) in table.items()))
    return "\n".join(out) + "\n"


def reference() -> str:
    return (Path(__file__).parent / "LANGUAGE.md").read_text() + appendix()
