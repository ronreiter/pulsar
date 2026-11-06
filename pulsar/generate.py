"""Generate sounds locally with Stable Audio 3 (Stability AI's open-weight text-to-audio models).

    pulsar generate "glass shattering on concrete" --seconds 3 -o glass.wav
    track glass = sampler(generate("glass shattering on concrete", seconds: 3))

Models: "sfx" (Stable Audio 3 Small-SFX: sound effects and foley), "music" (Small-Music:
instrumental loops and textures) -- both run on a laptop CPU -- and "medium" (1.4B, needs an
NVIDIA GPU). Stable Audio 3 makes no vocals; for singing or speech use voice().

The generator lives in its own environment (~/.cache/pulsar/stable-audio, created on first use)
so its PyTorch install never touches Pulsar's. The weights are gated on Hugging Face: accept the
license on the model page once and log in (see `pulsar generate --setup`). Results are cached by
prompt, model, length and seed, so a song renders the same sound every time.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import PulsarError

ROOT = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar"))
ENV = ROOT / "stable-audio" / "venv"
OUT = ROOT / "generated"
PACKAGE = "stable-audio-3 @ git+https://github.com/Stability-AI/stable-audio-3"
MODELS = {"sfx": "small-sfx", "music": "small-music", "medium": "medium"}
PAGES = {"sfx": "https://huggingface.co/stabilityai/stable-audio-3-small-sfx",
         "music": "https://huggingface.co/stabilityai/stable-audio-3-small-music",
         "medium": "https://huggingface.co/stabilityai/stable-audio-3-medium"}
LICENSE = "Stability AI Community License"
SCHEME = "gen:"


def ref(prompt: str, model: str = "sfx", seconds: float = 4.0, seed: int = 0) -> str:
    """The portable reference a sampler uses for a generated sound."""
    check(model, seconds)
    return f"{SCHEME}{model}:{seconds:g}:{int(seed)}:{prompt}"


def parse(r: str) -> tuple[str, float, int, str]:
    model, seconds, seed, prompt = r[len(SCHEME):].split(":", 3)
    return model, float(seconds), int(seed), prompt


def check(model: str, seconds: float):
    if model in ("vocal", "vocals", "voice", "speech"):
        raise PulsarError("Stable Audio 3 makes no vocals",
                          hint='for singing or speech use voice("af_heart", lyrics: "...")')
    if model not in MODELS:
        raise PulsarError(f"generate model must be one of {', '.join(MODELS)}")
    limit = 380 if model == "medium" else 120
    if not 0.5 <= seconds <= limit:
        raise PulsarError(f"generate seconds must be between 0.5 and {limit} for the {model} model")


def python() -> Path:
    return ENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def installed() -> bool:
    if not python().exists():
        return False
    r = subprocess.run([str(python()), "-c", "import stable_audio_3"], capture_output=True)
    return r.returncode == 0


def setup(log=print) -> Path:
    """Create the generator's environment (once): Python 3.12, PyTorch and the stable-audio-3 package."""
    if installed():
        return python()
    uv = shutil.which("uv")
    if uv is None:
        raise PulsarError("installing Stable Audio needs uv", hint="curl -LsSf https://astral.sh/uv/install.sh | sh")
    log("  installing Stable Audio 3 into ~/.cache/pulsar/stable-audio (once; PyTorch is a few GB)...")
    ENV.parent.mkdir(parents=True, exist_ok=True)
    for cmd in ([uv, "venv", "-q", "--python", "3.12", str(ENV)],
                [uv, "pip", "install", "-q", "--python", str(python()), PACKAGE]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise PulsarError(f"could not install Stable Audio: {r.stderr.strip()[-400:]}")
    return python()


RUNNER = r"""
import json, sys
import soundfile as sf
from stable_audio_3 import StableAudioModel
a = json.loads(sys.argv[1])
model = StableAudioModel.from_pretrained(a["model"])
audio = model.generate(prompt=a["prompt"], duration=a["seconds"], seed=a["seed"])  # (batch, channels, samples)
x = audio[0].detach().cpu().float().numpy()
sf.write(a["out"], x.T, int(model.model.sample_rate))
"""


def generate(prompt: str, model: str = "sfx", seconds: float = 4.0, seed: int = 0, log=print) -> Path:
    """A generated sound as a WAV file (cached)."""
    check(model, seconds)
    key = hashlib.sha1(json.dumps([prompt, model, round(seconds, 3), int(seed)]).encode()).hexdigest()[:16]
    out = OUT / f"{key}.wav"
    if out.exists():
        return out
    py = setup(log)
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"  generating {seconds:g}s with Stable Audio 3 {MODELS[model]}: {prompt!r}")
    args = {"prompt": prompt, "model": MODELS[model], "seconds": float(seconds), "seed": int(seed), "out": str(out)}
    r = subprocess.run([str(py), "-c", RUNNER, json.dumps(args)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        err = (r.stderr or r.stdout).strip()
        if any(s in err for s in ("GatedRepo", "401", "403", "gated", "Access to model", "token")):
            raise PulsarError("the Stable Audio 3 weights are gated on Hugging Face",
                              hint=f"accept the license at {PAGES[model]} (free, approved at once), then run "
                                   f"`{ENV / 'bin' / 'hf'} auth login` with a token from huggingface.co/settings/tokens")
        raise PulsarError(f"Stable Audio failed: {err[-500:]}")
    meta = out.with_suffix(".json")
    meta.write_text(json.dumps(dict(args, license=LICENSE)))
    return out


def credit(prompt: str, model: str) -> dict:
    return {"title": prompt, "creator": f"generated with Stable Audio 3 {MODELS[model]}", "license": LICENSE,
            "url": PAGES[model],
            "credit": f'"{prompt}" -- generated locally with Stable Audio 3 {MODELS[model]} ({LICENSE}, {PAGES[model]})'}
