"""The sound library: openly licensed samples and sound effects, searchable and insertable into a song.

Sources (all filtered to licenses you can use in your music):
  vcsl       the Versilian Community Sample Library -- 4,000+ CC0 recordings: drums and percussion,
             struck / plucked instruments, winds, an FM synth (github.com/sgossner/VCSL)
  commons    Wikimedia Commons sound effects and field recordings (PD / CC0 / CC BY)
  freesound  Freesound.org (CC0 / CC BY), when FREESOUND_API_KEY is set

A sound is named by a portable reference -- "vcsl:Membranophones/.../Kick 1.wav",
"commons:File:Thunder.ogg", "freesound:12345" -- that a song uses directly:

    track hit = sampler("vcsl:Idiophones/Struck Idiophones/Anvil/Anvil_hit_1.wav")

It is downloaded on first use into ~/.cache/pulsar/library; renders credit it next to the output.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import PulsarError

CACHE = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar")) / "library"
UA = {"User-Agent": "pulsar-music/0.2 (https://github.com/ronreiter/pulsar)"}
VCSL_REPO = "sgossner/VCSL"
VCSL_TREE = f"https://api.github.com/repos/{VCSL_REPO}/git/trees/master?recursive=1"
VCSL_RAW = f"https://raw.githubusercontent.com/{VCSL_REPO}/master/"
SOURCES = ("vcsl", "commons", "freesound")
SCHEMES = tuple(s + ":" for s in SOURCES)


@dataclass
class Item:
    ref: str  # portable reference, e.g. "vcsl:Idiophones/.../hit.wav"
    title: str
    source: str
    license: str
    creator: str = ""
    url: str = ""
    file_url: str = ""
    duration: float | None = None
    tags: list = field(default_factory=list)
    score: float = 0.0

    def credit(self) -> str:
        return f'"{self.title}" by {self.creator or "unknown"} -- {self.license} -- {self.url}'


def is_ref(text: str) -> bool:
    return isinstance(text, str) and text.startswith(SCHEMES)


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1]


# -- VCSL ---------------------------------------------------------------------------------

def vcsl_index() -> list[dict]:
    """Every audio file in VCSL (path, size); fetched once from GitHub and cached."""
    cache = CACHE / "vcsl-index.json"
    if cache.exists():
        return json.loads(cache.read_text())
    try:
        tree = json.loads(_get(VCSL_TREE, 60))["tree"]
    except OSError as e:
        raise PulsarError(f"could not list the VCSL library on GitHub ({e})") from None
    items = [{"path": x["path"], "size": x.get("size", 0)} for x in tree
             if x["type"] == "blob" and x["path"].lower().endswith((".wav", ".flac", ".ogg"))]
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(items))
    return items


def vcsl_item(path: str) -> Item:
    parts = path.split("/")
    title = " / ".join(parts[-3:-1] + [Path(parts[-1]).stem]) if len(parts) > 2 else Path(path).stem
    return Item(f"vcsl:{path}", title, "vcsl", "CC0", "Versilian Studios (VCSL)",
                f"https://github.com/{VCSL_REPO}/blob/master/{urllib.parse.quote(path)}",
                VCSL_RAW + urllib.parse.quote(path), None, parts[:-1])


def search_vcsl(query: str, limit: int) -> list[Item]:
    words = _words(query)
    out = []
    for x in vcsl_index():
        hay = x["path"].lower()
        hits = sum(1 for w in words if w in hay)
        if words and hits < len(words):
            continue
        it = vcsl_item(x["path"])
        name = Path(x["path"]).stem.lower()
        it.score = hits * 2 + sum(1 for w in words if w in name) - len(x["path"]) / 1000
        out.append(it)
    out.sort(key=lambda i: -i.score)
    # one or two takes per instrument folder first, so a search shows variety rather than 40 takes
    seen: dict = {}
    first, rest = [], []
    for it in out:
        folder = it.ref.rsplit("/", 1)[0]
        seen[folder] = seen.get(folder, 0) + 1
        (first if seen[folder] <= 2 else rest).append(it)
    return (first + rest)[:limit]


# -- Wikimedia Commons / Freesound (shared with ambience()) --------------------------------

def search_commons(query: str, limit: int, license: str = "open") -> list[Item]:
    from . import stock
    found = stock.search_commons(query, license, 0.05, 120)
    return [Item(f"commons:{s.id}", s.title, "commons", s.license, s.creator, s.page_url, s.file_url, s.duration,
                 [], s.score) for s in found[:limit]]


def search_freesound(query: str, limit: int, license: str = "open") -> list[Item]:
    from . import stock
    if not os.environ.get("FREESOUND_API_KEY"):
        return []
    found = stock.search_freesound(query, license, 0.05, 120)
    return [Item(f"freesound:{s.id}", s.title, "freesound", s.license, s.creator, s.page_url, s.file_url,
                 s.duration, [], s.score) for s in found[:limit]]


def search(query: str, source: str | None = None, limit: int = 30, license: str = "open") -> list[Item]:
    """Matching sounds, best first. With no source, VCSL first, then Freesound and Commons."""
    if source and source not in SOURCES:
        raise PulsarError(f"source must be one of {', '.join(SOURCES)}")
    out: list[Item] = []
    errors = []
    for src, fn in (("vcsl", search_vcsl), ("freesound", search_freesound), ("commons", search_commons)):
        if source and src != source:
            continue
        try:
            out += fn(query, limit) if src == "vcsl" else fn(query, limit, license)
        except PulsarError as e:
            errors.append(e.msg)
    if not out and errors:
        raise PulsarError("; ".join(errors))
    return out[:limit] if source else _interleave(out, limit)


def _interleave(items: list[Item], limit: int) -> list[Item]:
    by: dict = {}
    for it in items:
        by.setdefault(it.source, []).append(it)
    out = []
    while len(out) < limit and any(by.values()):
        for src in SOURCES:
            if by.get(src):
                out.append(by[src].pop(0))
    return out[:limit]


# -- fetching ------------------------------------------------------------------------------

def item(ref: str) -> Item:
    """Metadata for a reference (remembered once fetched)."""
    meta = _meta_path(ref)
    if meta.exists():
        return Item(**json.loads(meta.read_text()))
    scheme, _, rest = ref.partition(":")
    if scheme == "vcsl":
        return vcsl_item(rest)
    if scheme in ("commons", "freesound"):
        from . import stock
        title = rest[5:] if rest.startswith("File:") else rest
        found = [s for s in (stock.search_commons(title, "by-sa", 0.0, 3600) if scheme == "commons"
                             else stock.search_freesound(rest, "by-sa", 0.0, 3600)) if s.id == rest]
        if not found:
            raise PulsarError(f"can't find {ref!r} in {scheme}", hint="pulsar library search finds sounds")
        s = found[0]
        return Item(ref, s.title, scheme, s.license, s.creator, s.page_url, s.file_url, s.duration)
    raise PulsarError(f"{ref!r} isn't a library reference", hint="references look like vcsl:..., commons:File:..., freesound:123")


def _meta_path(ref: str) -> Path:
    return CACHE / "files" / (hashlib.sha1(ref.encode()).hexdigest()[:16] + ".json")


def fetch(ref: str, log=lambda m: None) -> tuple[Path, Item]:
    """The sound as a local WAV (downloaded and converted once), and its metadata."""
    h = hashlib.sha1(ref.encode()).hexdigest()[:16]
    wav = CACHE / "files" / f"{h}.wav"
    meta = _meta_path(ref)
    if wav.exists() and meta.exists():
        return wav, Item(**json.loads(meta.read_text()))
    it = item(ref)
    wav.parent.mkdir(parents=True, exist_ok=True)
    log(f"  fetching {it.title} ({it.source}, {it.license})")
    raw = wav.with_suffix(".download")
    try:
        raw.write_bytes(_get(it.file_url, 120))
    except OSError as e:
        raise PulsarError(f"could not download {ref} ({e})") from None
    if raw.read_bytes()[:4] == b"RIFF" and it.file_url.lower().endswith(".wav"):
        os.replace(raw, wav)
    else:
        if not shutil.which("ffmpeg"):
            raw.unlink(missing_ok=True)
            raise PulsarError("decoding this sound needs ffmpeg (macOS: brew install ffmpeg)")
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-c:a", "pcm_s24le",
                            str(wav)], capture_output=True)
        raw.unlink(missing_ok=True)
        if r.returncode != 0:
            raise PulsarError(f"could not decode {ref}: {r.stderr.decode(errors='replace')[-200:]}")
    if it.duration is None:
        try:
            import soundfile as sf
            it.duration = round(sf.info(str(wav)).duration, 3)
        except RuntimeError:
            pass
    meta.write_text(json.dumps(asdict(it)))
    return wav, it


# -- code to insert --------------------------------------------------------------------------

NOISE = {"wav", "ogg", "flac", "mp3", "file", "hit", "hits", "sum", "mid", "main", "close", "far", "room", "near",
         "ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "struck", "plucked", "legacy", "sample", "samples", "one",
         "shot", "idiophones", "membranophones", "aerophones", "chordophones", "electrophones", "gravity", "sound"}


def track_name(it: Item, taken: set[str] = frozenset()) -> str:
    """A short, readable track name: 'Cowbell1_Hit_v2_rr1_Mid.wav' -> cowbell."""
    def meaningful(text):
        out = []
        for w in re.findall(r"[a-z]+", text.lower()):
            if w in NOISE or re.fullmatch(r"(v|rr|vl|mic)\d*|\w", w) or w in out:
                continue
            out.append(w)
        return out
    stem = Path(it.ref.split(":", 1)[1]).stem if ":" in it.ref else it.title
    words = meaningful(stem)[:2] or meaningful(it.title)[-2:]
    base = "_".join(words)[:24].strip("_") or "sound"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def snippet(it: Item, taken: set[str] = frozenset()) -> dict:
    """The lines a song needs to use this sound: a track (with its credit) and a play line."""
    name = track_name(it, set(taken))
    return {"name": name,
            "track": f'track {name} = sampler("{it.ref}")   // {it.title} -- {it.license}',
            "play": f'play {name} "x ~ ~ ~"'}
