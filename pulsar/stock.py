"""Free stock sounds (birds, water, rain, wind, ...) from openly licensed archives.

Sources:
  * Wikimedia Commons (default, no account needed)
  * Freesound (set FREESOUND_API_KEY; uses the high-quality previews)

Licenses are filtered before anything is used:
  "cc0"   public domain / CC0 only (no attribution needed)
  "open"  (default) also CC BY, which needs attribution -- credits are written next to every render
  "by-sa" also CC BY-SA (share-alike: anything you publish with it must use the same license)
Non-commercial (NC) and no-derivatives (ND) licenses are never used.

Downloads are cached under ~/.cache/pulsar/sounds, converted to WAV, and each query remembers the
recording it picked so a song keeps the same sound on every render.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import PulsarError

CACHE = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar")) / "sounds"
UA = {"User-Agent": "pulsar-music/0.1 (https://github.com/ronreiter/pulsar)"}
LICENSES = ("cc0", "open", "by-sa")
BAD_WORDS = ("speech", "spoken", "lecture", "interview", "inaugural", "pronunciation", "music", "song", "songs",
             "rag", "opera", "symphony", "voice", "audiobook", "podcast", "radio", "librivox", "poem", "anthem",
             "hymn", "composition", "composer", "quartet", "quartette", "orchestra", "band", "singer", "lyrics",
             "phonograph", "cylinder", "78 rpm", "gramophone")
GOOD_WORDS = ("field recording", "nature", "ambience", "ambient", "soundscape", "bird", "water", "stream", "river",
              "rain", "wind", "forest", "ocean", "sea", "waves", "creek", "brook", "fire", "insects", "night")


@dataclass
class Sound:
    source: str
    id: str
    title: str
    creator: str
    license: str
    license_url: str
    page_url: str
    file_url: str
    duration: float
    score: float = 0.0

    def credit(self) -> str:
        lic = f"{self.license} ({self.license_url})" if self.license_url else self.license
        return f'"{self.title}" by {self.creator or "unknown"} -- {lic} -- {self.page_url}'


def license_ok(name: str, policy: str) -> bool:
    n = (name or "").lower().replace("-", " ")
    if not n or "nc" in n.split() or "nd" in n.split() or "non commercial" in n or "noncommercial" in n:
        return False
    if n.startswith(("public domain", "pd", "cc0", "cc zero", "creative commons 0")):
        return True
    if policy == "cc0":
        return False
    if n.startswith(("cc by", "attribution", "creative commons attribution")):
        if "sa" in n.split() or "share" in n:
            return policy == "by-sa"
        return True
    return False


def _get(url: str, timeout: float = 25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _score(query: str, title: str, text: str) -> float:
    words = [w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2]
    t, x = title.lower(), text.lower()
    s = sum(2.0 for w in words if w in t) + sum(1.0 for w in words if w in x)
    s += sum(0.5 for g in GOOD_WORDS if g in x or g in t)
    s -= sum(4.0 for b in BAD_WORDS if re.search(rf"\b{b}\b", t) or re.search(rf"\b{b}\b", x))
    return s


def search_commons(query: str, policy: str, min_s: float, max_s: float, limit: int = 50) -> list[Sound]:
    found = _commons(query, policy, min_s, max_s, limit)
    words = [w for w in re.findall(r"\w+", query) if len(w) > 2]
    if len(found) < 6 and len(words) > 1:  # Commons matches every word; widen to any of them
        seen = {s.id for s in found}
        found += [s for s in _commons(" OR ".join(words), policy, min_s, max_s, limit) if s.id not in seen]
    # Keep what matches the query and doesn't look like music or speech.
    return sorted((s for s in found if s.score >= 1.5), key=lambda s: -s.score)


def _commons(query: str, policy: str, min_s: float, max_s: float, limit: int) -> list[Sound]:
    params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:audio",
              "gsrnamespace": "6", "gsrlimit": str(limit), "prop": "imageinfo",
              "iiprop": "url|size|mime|extmetadata|metadata"}
    try:
        data = json.loads(_get("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)))
    except OSError as e:
        raise PulsarError(f"could not reach Wikimedia Commons ({e})") from None
    out = []
    for page in (data.get("query", {}).get("pages") or {}).values():
        title = page.get("title", "")
        if title.startswith("File:En-") or "-article" in title:
            continue  # spoken Wikipedia articles
        ii = (page.get("imageinfo") or [{}])[0]
        em = ii.get("extmetadata", {})
        md = {m.get("name"): m.get("value") for m in (ii.get("metadata") or []) if isinstance(m, dict)}
        try:
            dur = float(md.get("length") or md.get("playtime_seconds") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        lic = em.get("LicenseShortName", {}).get("value", "")
        if not (min_s <= dur <= max_s) or not license_ok(lic, policy):
            continue
        artist = _strip(em.get("Artist", {}).get("value", ""))
        text = " ".join(_strip(em.get(k, {}).get("value", "")) for k in ("ImageDescription", "Categories", "ObjectName"))
        score = _score(query.replace(" OR ", " "), title, text + " " + artist)
        if re.search(r"\b(Composition|Recording|Performer|Lyricist):", artist):
            score -= 10  # credited like a music recording
        out.append(Sound("commons", title, title[5:], artist, lic, em.get("LicenseUrl", {}).get("value", ""),
                         ii.get("descriptionurl", ""), ii.get("url", ""), dur, score))
    return out


def search_freesound(query: str, policy: str, min_s: float, max_s: float, limit: int = 40) -> list[Sound]:
    key = os.environ.get("FREESOUND_API_KEY")
    if not key:
        return []
    lic_filter = '"Creative Commons 0"' if policy == "cc0" else '("Creative Commons 0" OR "Attribution")'
    params = {"query": query, "token": key, "page_size": str(limit),
              "filter": f"license:{lic_filter} duration:[{min_s} TO {max_s}]",
              "fields": "id,name,username,license,previews,duration,url,description,tags"}
    try:
        data = json.loads(_get("https://freesound.org/apiv2/search/text/?" + urllib.parse.urlencode(params)))
    except OSError as e:
        raise PulsarError(f"could not reach Freesound ({e})") from None
    out = []
    for r in data.get("results", []):
        lic_url = r.get("license", "")
        lic = "CC0" if "zero" in lic_url or "publicdomain" in lic_url else ("CC BY" if "/by/" in lic_url else lic_url)
        if not license_ok(lic, policy):
            continue
        text = (r.get("description") or "") + " " + " ".join(r.get("tags") or [])
        out.append(Sound("freesound", str(r["id"]), r.get("name", ""), r.get("username", ""), lic, lic_url,
                         r.get("url", ""), (r.get("previews") or {}).get("preview-hq-mp3", ""), float(r.get("duration", 0)),
                         _score(query, r.get("name", ""), text) + 1.0))
    return sorted(out, key=lambda s: -s.score)


def search(query: str, license: str = "open", min_s: float = 15, max_s: float = 900) -> list[Sound]:
    if license not in LICENSES:
        raise PulsarError(f"license must be one of {', '.join(LICENSES)}")
    results = search_freesound(query, license, min_s, max_s) + search_commons(query, license, min_s, max_s)
    return sorted(results, key=lambda s: -s.score)


def _index() -> dict:
    try:
        return json.loads((CACHE / "index.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_index(idx: dict):
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / f"index.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(idx, indent=1))
    os.replace(tmp, CACHE / "index.json")


def download(sound: Sound) -> Path:
    """Fetch a sound once and convert it to 48 kHz stereo WAV in the cache."""
    key = hashlib.sha1(f"{sound.source}:{sound.id}".encode()).hexdigest()[:16]
    wav = CACHE / f"{key}.wav"
    meta = CACHE / f"{key}.json"
    if wav.exists() and meta.exists():
        return wav
    if not shutil.which("ffmpeg"):
        raise PulsarError("stock sounds need ffmpeg to decode downloads (macOS: brew install ffmpeg)")
    CACHE.mkdir(parents=True, exist_ok=True)
    raw = CACHE / f"{key}.download"
    try:
        raw.write_bytes(_get(sound.file_url, timeout=120))
    except OSError as e:
        raise PulsarError(f"could not download {sound.title!r} ({e})") from None
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-ac", "2", "-ar", "48000",
                        "-t", "900", str(wav)], capture_output=True)
    raw.unlink(missing_ok=True)
    if r.returncode != 0 or not wav.exists():
        raise PulsarError(f"could not decode {sound.title!r}: {r.stderr.decode(errors='replace')[-200:]}")
    meta.write_text(json.dumps(asdict(sound), indent=1))
    return wav


def resolve(query: str, license: str = "open", pick: int = 0, sound_id: str | None = None,
            min_s: float = 15) -> tuple[Path, Sound]:
    """The recording for a query: pinned by id, or remembered from the first resolution, or the
    best-scoring match (pick=N takes the N-th best). Returns (wav path, metadata)."""
    idx = _index()
    k = f"{query}|{license}|{pick}|{sound_id or ''}"
    if k in idx:
        s = Sound(**idx[k])
        return download(s), s
    results = search(query if not sound_id else sound_id.replace("File:", ""), license, min_s=min_s)
    if sound_id:
        results = [r for r in results if r.id == sound_id or r.title == sound_id] or results
    if not results:
        raise PulsarError(f"no {license}-licensed recordings found for {query!r}",
                          hint="try a broader query, or license: \"by-sa\" / a FREESOUND_API_KEY for more results")
    s = results[min(pick, len(results) - 1)]
    path = download(s)
    idx[k] = asdict(s)
    _save_index(idx)
    return path, s
