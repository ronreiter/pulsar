"""Sampled acoustic pianos: real recordings of every few keys at several velocities.

  piano("grand")    Salamander Grand Piano V3 -- Yamaha C5, 16 velocity layers (Alexander Holm, CC BY 3.0)
  piano("upright")  Upright Piano KW -- a Kawai upright in a living room, 2 layers (FreePats, CC0)

Each set downloads once (grand ~410 MB, upright ~33 MB) into ~/.cache/pulsar/instruments and is read
through its SFZ map: the note picks the nearest recorded key and the velocity layer, and the recording
is re-pitched by the few semitones between them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .errors import PulsarError

ROOT = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar")) / "instruments"
BASE = "https://freepats.zenvoid.org/Piano"
PIANOS = {
    "grand": dict(url=f"{BASE}/SalamanderGrandPiano/SalamanderGrandPianoV3+20161209_44khz16bit.tar.xz",
                  archive="grand.tar.xz", dir="grand", size="410 MB",
                  title="Salamander Grand Piano V3", creator="Alexander Holm", license="CC BY 3.0",
                  license_url="https://creativecommons.org/licenses/by/3.0/",
                  page="https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html"),
    "upright": dict(url=f"{BASE}/UprightPianoKW/UprightPianoKW-SFZ+FLAC-20220221.7z",
                    archive="upright.7z", dir="upright", size="33 MB",
                    title="Upright Piano KW", creator="FreePats (Gonzalo and Roberto)", license="CC0",
                    license_url="https://creativecommons.org/publicdomain/zero/1.0/",
                    page="https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html"),
}

NOTE_RE = re.compile(r"^([a-gA-G])([#b]?)(-?\d+)$")


def _key(v: str) -> int:
    v = v.strip()
    if v.lstrip("-").isdigit():
        return int(v)
    m = NOTE_RE.match(v)
    if not m:
        raise ValueError(v)
    pc = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}[m[1].lower()]
    pc += {"#": 1, "b": -1, "": 0}[m[2]]
    return pc + 12 * (int(m[3]) + 1)


@dataclass
class Region:
    path: Path
    lokey: int
    hikey: int
    center: int
    lovel: int
    hivel: int
    tune: float = 0.0  # cents
    volume: float = 0.0  # dB
    veltrack: float = 1.0  # 0-1: how much velocity scales the level (on top of the velocity layers)
    release: float = 0.5  # seconds for the damper to stop the string
    trigger: str = "attack"  # or "release": sounds when the key comes up (string resonance, hammer noise)
    rt_decay: float = 0.0  # dB per second the key was held, for release sounds
    keytrack: float = 1.0  # 0 for sounds that keep their pitch (hammer noise)
    loop: tuple | None = None  # (start, end) frames, looped if a note outlasts the recording


def parse_sfz(sfz: Path) -> list[Region]:
    """The playable regions of an SFZ file: note-on and release-triggered (pedal noises are left out)."""
    text = re.sub(r"//[^\n]*", "", sfz.read_text(errors="replace"))
    tokens = re.findall(r"<(\w+)>|(\w+)=(.*?)(?=\s+\w+=|\s*<|\s*$)", text, flags=re.S)
    order = ["control", "global", "master", "group"]
    levels = {k: {} for k in order}
    regions, cur, header = [], None, None

    def flush():
        if cur is None:
            return
        o = {}
        for lv in order:
            o.update(levels[lv])
        o.update(cur)
        if "sample" not in o or o.get("trigger", "attack") not in ("attack", "release"):
            return
        try:
            if "key" in o:
                lo = hi = c = _key(o["key"])
            else:
                lo, hi = _key(o.get("lokey", "0")), _key(o.get("hikey", "127"))
                c = _key(o.get("pitch_keycenter", "60"))  # the SFZ default
        except ValueError:
            return
        if hi < 0:
            return  # pedal noises (triggered by controller 64), not keys
        sample = o["sample"].strip().replace("\\", "/")
        path = (sfz.parent / o.get("default_path", "").replace("\\", "/") / sample).resolve()
        loop = None
        if o.get("loop_mode") in ("loop_continuous", "loop_sustain") and "loop_end" in o:
            loop = (int(o.get("loop_start", 0)), int(o["loop_end"]))
        regions.append(Region(path, lo, hi, c, int(o.get("lovel", 1)), int(o.get("hivel", 127)),
                              float(o.get("tune", 0)), float(o.get("volume", 0)),
                              float(o.get("amp_veltrack", 100)) / 100, float(o.get("ampeg_release", 0.5)),
                              o.get("trigger", "attack"), float(o.get("rt_decay", 0)),
                              float(o.get("pitch_keytrack", 100)) / 100, loop))

    for h, k, v in tokens:
        if h:
            flush()
            cur = None
            header = h
            if h in levels:
                for lower in order[order.index(h):]:
                    levels[lower] = {}
            elif h == "region":
                cur = {}
            continue
        if header == "region" and cur is not None:
            cur[k] = v.strip()
        elif header in levels:
            levels[header][k] = v.strip()
    flush()
    return [r for r in regions if r.path.exists()]


def find(regions: list[Region], key: int, vel: int) -> Region | None:
    """The note-on region for a key and MIDI velocity: an exact match, else the nearest recorded key."""
    cands = [r for r in regions if r.trigger == "attack" and r.lovel <= vel <= r.hivel]
    exact = [r for r in cands if r.lokey <= key <= r.hikey]
    if exact:
        return exact[0]
    return min(cands, key=lambda r: abs(r.center - key)) if cands else None


def releases(regions: list[Region], key: int, vel: int) -> list[Region]:
    """Every sound a key makes when it comes up (string resonance, hammer and damper noise)."""
    return [r for r in regions if r.trigger == "release" and r.lokey <= key <= r.hikey and r.lovel <= vel <= r.hivel]


def _download(url: str, dest: Path):
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "pulsar-music/0.1 (https://github.com/ronreiter/pulsar)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise PulsarError(f"could not download the piano samples ({e})") from None
    os.replace(tmp, dest)


def install(name: str, log=print) -> Path:
    """The SFZ file of a piano, downloading and unpacking it the first time."""
    info = PIANOS[name]
    d = ROOT / info["dir"]
    found = sorted(d.rglob("*.sfz")) if d.exists() else []
    if not found:
        ROOT.mkdir(parents=True, exist_ok=True)
        arch = ROOT / "dl" / info["archive"]
        arch.parent.mkdir(parents=True, exist_ok=True)
        if not arch.exists():
            log(f"  downloading the {info['title']} samples ({info['size']}, once)...")
            _download(info["url"], arch)
        d.mkdir(parents=True, exist_ok=True)
        tar = shutil.which("bsdtar") or shutil.which("tar")
        r = subprocess.run([tar, "-xf", str(arch), "-C", str(d)], capture_output=True)
        if r.returncode != 0:
            raise PulsarError(f"could not unpack {arch.name}: {r.stderr.decode(errors='replace')[-200:]}",
                              hint="macOS tar (bsdtar) reads .7z and .tar.xz; on Linux install libarchive-tools")
        found = sorted(d.rglob("*.sfz"))
        if not found:
            raise PulsarError(f"no .sfz instrument inside {arch.name}")
    return choose_sfz(name, found)


def choose_sfz(name: str, found: list[Path]) -> Path:
    # Prefer the full-quality mapping of each set.
    def rank(p: Path):
        n = p.name.lower()
        return (("small" in n) * 4 + ("bright" in n) * 2 + ("pedal" in n and "nopedal" not in n),
                "retuned" not in n, n)  # the retuned Salamander map corrects each sample's tuning
    return min(found, key=rank)


def credit(name: str) -> dict:
    i = PIANOS[name]
    return {"title": i["title"], "creator": i["creator"], "license": i["license"], "license_url": i["license_url"],
            "url": i["page"],
            "credit": f'"{i["title"]}" by {i["creator"]} -- {i["license"]} ({i["license_url"]}) -- {i["page"]}'}
