"""A sampled orchestra: the Versilian Studios Chamber Orchestra 2 Community Edition (VSCO-2 CE, CC0).

    orchestra("violins")        sustained violin section (vibrato)
    orchestra("celli_spic")     cello section, spiccato -- the short, driving ostinato bowing
    orchestra("horns")          French horn, sustained
    orchestra("percussion")     a kit: bass drum, taiko, timpani, gong, cymbal swells, rolls...

Only the folders an instrument needs are downloaded (from github.com/sgossner/VSCO-2-CE) into
~/.cache/pulsar/instruments/vsco, on the first render that uses it. The library ships samples
without a mapping, so the key ranges, velocity layers and round-robin takes are read from the
file names (VlnEns_susVib_A3_v2.wav: A3, second dynamic layer).
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .errors import PulsarError
from .piano import ROOT, Region, _key

REPO = "sgossner/VSCO-2-CE"
RAW = f"https://raw.githubusercontent.com/{REPO}/master/"
TREE = f"https://api.github.com/repos/{REPO}/git/trees/master?recursive=1"
HOME = ROOT / "vsco"
UA = {"User-Agent": "pulsar-music/0.1 (https://github.com/ronreiter/pulsar)"}
CREDIT = {"title": "Versilian Studios Chamber Orchestra 2 Community Edition", "creator": "Versilian Studios",
          "license": "CC0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
          "url": "https://github.com/sgossner/VSCO-2-CE"}

# name: (folders, sustained?, release seconds, octave, description)
PITCHED = {
    "violins": (["Strings/Violin Section/susVib"], True, 0.45, 4, "violin section, sustained with vibrato"),
    "violins_spic": (["Strings/Violin Section/Spic"], False, 0.25, 4, "violin section, spiccato (short, bouncing)"),
    "violins_trem": (["Strings/Violin Section/Trem"], True, 0.4, 4, "violin section, tremolo"),
    "violins_pizz": (["Strings/Violin Section/Pizz"], False, 0.3, 4, "violin section, pizzicato"),
    "violas": (["Strings/Viola Section/susvib"], True, 0.45, 3, "viola section, sustained"),
    "violas_spic": (["Strings/Viola Section/spic"], False, 0.25, 3, "viola section, spiccato"),
    "violas_trem": (["Strings/Viola Section/trem"], True, 0.4, 3, "viola section, tremolo"),
    "celli": (["Strings/Cello Section/susvib"], True, 0.5, 3, "cello section, sustained"),
    "celli_spic": (["Strings/Cello Section/spic"], False, 0.25, 2, "cello section, spiccato"),
    "celli_trem": (["Strings/Cello Section/trem"], True, 0.4, 2, "cello section, tremolo"),
    "celli_pizz": (["Strings/Cello Section/pizzT"], False, 0.3, 2, "cello section, pizzicato"),
    "basses": (["Strings/Solo Contrabass/SusVib"], True, 0.5, 2, "contrabass, sustained"),
    "basses_spic": (["Strings/Solo Contrabass/Spic"], False, 0.25, 1, "contrabass, spiccato"),
    "basses_pizz": (["Strings/Solo Contrabass/Pizz"], False, 0.4, 1, "contrabass, pizzicato"),
    "harp": (["Strings/Harp"], False, 1.5, 4, "concert harp"),
    "violin": (["Strings/Solo Violin/Arco Vib"], True, 0.35, 4, "solo violin, bowed with vibrato"),
    "violin_pizz": (["Strings/Solo Violin/Pizz"], False, 0.3, 4, "solo violin, pizzicato"),
    "glockenspiel": (["Percussion/Glock"], False, 2.5, 5, "glockenspiel"),
    "horns": (["Brass/F Horn/sus"], True, 0.4, 3, "French horn, sustained"),
    "horns_stac": (["Brass/F Horn/stac"], False, 0.25, 3, "French horn, staccato"),
    "trombones": (["Brass/Tenor Trombone/sus"], True, 0.35, 2, "tenor trombone, sustained"),
    "trombones_stac": (["Brass/Tenor Trombone/stac"], False, 0.25, 2, "tenor trombone, staccato"),
    "tuba": (["Brass/Tuba/sus"], True, 0.4, 1, "tuba, sustained"),
    "tuba_stac": (["Brass/Tuba/stac"], False, 0.25, 1, "tuba, staccato"),
    "trumpets": (["Brass/Trumpet/sus"], True, 0.35, 4, "trumpet, sustained"),
    "trumpets_stac": (["Brass/Trumpet/stac"], False, 0.2, 4, "trumpet, staccato"),
    "flute": (["Woodwinds/Flute/susvib"], True, 0.35, 5, "flute, sustained with vibrato"),
    "oboe": (["Woodwinds/Oboe/Vib"], True, 0.3, 4, "oboe, sustained"),
    "clarinet": (["Woodwinds/Clarinet/susLong"], True, 0.3, 4, "clarinet, sustained"),
    "bassoon": (["Woodwinds/Bassoon/sus"], True, 0.3, 2, "bassoon, sustained"),
}

# percussion voice: (file-name regex over repo paths, description)
KIT = {
    "bd": (r"^VSCO 1 Percussion/drums/bass/bdrum_(ppp|pp|p|mp|f|ff|fff)_\d+\.wav$", "concert bass drum"),
    "bdm": (r"^VSCO 1 Percussion/drums/bass/bdrum_muted_\w+\.wav$", "muted bass drum (tight, punchy)"),
    "bdroll": (r"^VSCO 1 Percussion/drums/bass/bdrum_roll_long1\.wav$", "bass drum roll"),
    "taiko": (r"^VSCO 1 Percussion/drums/other/ethnic/giant/mallet/.*\.wav$", "giant ethnic drum, mallet (taiko-like)"),
    "taikoh": (r"^VSCO 1 Percussion/drums/other/ethnic/giant/hand/EthnicLargeHand_hit_\w+\.wav$",
               "giant ethnic drum, hand"),
    "taikos": (r"^VSCO 1 Percussion/drums/other/ethnic/giant/sticks/.*\.wav$", "giant ethnic drum, sticks"),
    "tenor": (r"^VSCO 1 Percussion/drums/tenor/tenor_lower/.*\.wav$", "tenor drum"),
    "timp": (r"^Percussion/Timpani/Timpani2_Hit_.*\.wav$", "timpani, low"),
    "timph": (r"^Percussion/Timpani/Timpani4_Hit_.*\.wav$", "timpani, high"),
    "timproll": (r"^Percussion/Timpani/Rolls/Timpani2_Roll_.*\.wav$", "timpani roll"),
    "gong": (r"^Percussion/gongHit_\w+\.wav$", "tam-tam / gong"),
    "crash": (r"^Percussion/cymbal-crash1_\w+\.wav$", "crash cymbals"),
    "swell": (r"^Percussion/susCymb1-cresc-Long_v1\.wav$", "suspended cymbal crescendo, long (ends on the next downbeat)"),
    "swellshort": (r"^Percussion/susCymb1-cresc-Short_v1\.wav$", "suspended cymbal crescendo, short"),
    "sus": (r"^Percussion/susCymb1-hit_\w+\.wav$", "suspended cymbal hit"),
    "snare": (r"^Percussion/Snare2-HitSN_.*\.wav$", "snare drum"),
    "snareroll": (r"^Percussion/Snare2-rollSN_.*\.wav$", "snare roll"),
    "anvil": (r"^Percussion/Anvil_Hit1_.*\.wav$", "anvil"),
    "doum": (r"^Percussion/Tumba-HitN_.*\.wav$", "low hand drum, open (a darbuka's doum)"),
    "tek": (r"^Percussion/Quinto-HitN_.*\.wav$", "high hand drum, open (a darbuka's tek)"),
    "ka": (r"^Percussion/Quinto-Tap1_.*\.wav$", "high hand drum, light tap"),
}

# The pattern language normalises some drum names (snare -> sd, crash -> cr); map them back.
ALIASES = {"sd": "snare", "cr": "crash"}

DYN = {"ppp": 1, "pp": 2, "p": 3, "mp": 4, "mf": 5, "f": 6, "ff": 7, "fff": 8}
NOTE_RE = re.compile(r"(?:^|[_\-])([A-G]#?)(-?\d)(?=[_\-.]|$)")
VEL_RE = re.compile(r"_v(\d+)", re.I)
DYN_RE = re.compile(r"_(ppp|pp|p|mp|mf|f|ff|fff)(?=[_.\d])")
RR_RE = re.compile(r"_rr?(\d+)|_(\d+)(?=(?:_\w+)?\.wav$)", re.I)


def names() -> list[str]:
    return sorted(PITCHED) + ["percussion"]


def describe(name: str) -> str:
    return PITCHED[name][4] if name in PITCHED else "orchestral percussion kit: " + ", ".join(KIT)


def _tree() -> list[dict]:
    cache = HOME / "tree.json"
    if cache.exists():
        return json.loads(cache.read_text())
    try:
        req = urllib.request.Request(TREE, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            files = [x for x in json.loads(r.read())["tree"] if x["type"] == "blob"]
    except OSError as e:
        raise PulsarError(f"could not list the orchestra samples on GitHub ({e})") from None
    HOME.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(files))
    return files


def _files(name: str) -> list[dict]:
    files = [x for x in _tree() if x["path"].lower().endswith(".wav")]
    if name == "percussion":
        pats = [re.compile(p) for p, _ in KIT.values()]
        return [x for x in files if any(p.match(x["path"]) for p in pats)]
    folders = PITCHED[name][0]
    return [x for x in files if x["path"].rsplit("/", 1)[0] in folders]


def _fetch(item: dict) -> None:
    dest = HOME / item["path"]
    if dest.exists() and dest.stat().st_size == item.get("size", dest.stat().st_size):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    url = RAW + urllib.parse.quote(item["path"])
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            os.replace(tmp, dest)
            return
        except OSError as e:
            if attempt == 2:
                tmp.unlink(missing_ok=True)
                raise PulsarError(f"could not download {item['path']} ({e})") from None


def install(name: str, log=print) -> list[Path]:
    """The instrument's sample files, downloading the missing ones."""
    if name not in PITCHED and name != "percussion":
        raise PulsarError(f"no orchestra instrument named {name!r}", hint="pulsar presets lists them")
    items = _files(name)
    missing = [x for x in items if not (HOME / x["path"]).exists()]
    if missing:
        mb = sum(x.get("size", 0) for x in missing) / 1e6
        log(f"  downloading {name} samples ({len(missing)} files, {mb:.0f} MB, once)...")
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(_fetch, missing))
    return [HOME / x["path"] for x in items]


def _rr(fname: str) -> int:
    m = RR_RE.search(fname)
    return int(m.group(1) or m.group(2)) if m else 1


def _layer(fname: str) -> int:
    m = VEL_RE.search(fname)
    if m:
        return int(m.group(1))
    m = DYN_RE.search(fname)
    return DYN[m.group(1)] if m else 1


def _velocity_ranges(layers: list[int]) -> dict[int, tuple[int, int]]:
    """Spread the recorded dynamic layers evenly over MIDI velocity 1-127."""
    layers = sorted(set(layers))
    n = len(layers)
    return {lv: (1 + round(i * 127 / n), round((i + 1) * 127 / n)) for i, lv in enumerate(layers)}


def regions(name: str, files: list[Path], release: float | None = None) -> list[Region]:
    """Key / velocity / round-robin regions for a pitched instrument, read from the file names."""
    sustained, rel = PITCHED[name][1], release if release is not None else PITCHED[name][2]
    by_note: dict[int, list[tuple[int, int, Path]]] = defaultdict(list)
    for f in files:
        m = NOTE_RE.search(f.name)
        if not m:
            continue
        by_note[_key(m.group(1).lower() + m.group(2))].append((_layer(f.name), _rr(f.name), f))
    centers = sorted(by_note)
    out = []
    for i, c in enumerate(centers):
        lo = 0 if i == 0 else (centers[i - 1] + c) // 2 + 1
        hi = 127 if i == len(centers) - 1 else (c + centers[i + 1]) // 2
        vr = _velocity_ranges([lv for lv, _, _ in by_note[c]])
        for lv, rr, f in sorted(by_note[c]):
            r = Region(f, lo, hi, c, vr[lv][0], vr[lv][1], veltrack=0.35, release=rel,
                       loop="auto" if sustained else None)
            r.rr = rr
            out.append(r)
    return out


def kit_regions(files: list[Path]) -> dict[str, list[Region]]:
    """Percussion voice -> its velocity layers and round-robin takes."""
    out = {}
    for voice, (pat, _) in KIT.items():
        rx = re.compile(pat)
        mine = [f for f in files if rx.match(f.relative_to(HOME).as_posix())]
        if not mine:
            continue
        vr = _velocity_ranges([_layer(f.name) for f in mine])
        rs = []
        for f in sorted(mine):
            r = Region(f, 0, 127, 60, *vr[_layer(f.name)], veltrack=0.3, release=4.0, keytrack=0.0)
            r.rr = _rr(f.name)
            rs.append(r)
        out[voice] = rs
    return out
