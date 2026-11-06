"""Plugin instruments: VST3 / AU synthesizers played by Pulsar, rendered offline.

    plugin("surge", preset: "Leads/Acid Lead")   Surge XT -- open source (GPL-3), installed for you
    plugin("vital", preset: "...")                Vital / Vitalium, if installed
    plugin("serum")                               Xfer Serum 2, if installed (commercial)
    plugin("/path/to/Anything.vst3")              any other VST3 (or AU component on macOS)

Surge XT is downloaded on first use into ~/.cache/pulsar/plugins (no installer, no admin
rights): the plugin, and its factory patches and wavetables. `pulsar plugins install` fetches
it ahead of time; `pulsar plugins` lists what's available.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import struct
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from .errors import PulsarError, suggest

ROOT = Path(os.environ.get("PULSAR_CACHE", Path.home() / ".cache" / "pulsar")) / "plugins"
UA = {"User-Agent": "pulsar-music/0.1 (https://github.com/ronreiter/pulsar)"}

SURGE_VERSION = "1.3.4"
SURGE_BASE = f"https://github.com/surge-synthesizer/releases-xt/releases/download/{SURGE_VERSION}/"
SURGE_FILES = {
    "Darwin": f"surge-xt-macos-{SURGE_VERSION}-pluginsonly.zip",
    "Linux": f"surge-xt-linux-{SURGE_VERSION}-pluginsonly.tar.gz",
    "Windows": f"surge-xt-win64-{SURGE_VERSION}-pluginsonly.zip",
}
SURGE_CONTENT = f"surge-xt-portable-content-{SURGE_VERSION}.tar.gz"

# name: (title, bundle names to look for, auto-installable, license, home page)
KNOWN = {
    "surge": ("Surge XT", ["Surge XT.vst3"], True, "GPL-3.0", "https://surge-synthesizer.github.io"),
    "vital": ("Vital", ["Vital.vst3", "Vitalium.vst3"], False, "GPL-3.0 (Vitalium) / free (Vital)",
              "https://vital.audio"),
    "serum": ("Serum 2", ["Serum2.vst3", "Serum.vst3"], False, "commercial", "https://xferrecords.com"),
}
ALIASES = {"surge xt": "surge", "surgext": "surge", "vitalium": "vital", "serum2": "serum", "serum 2": "serum"}


def system_dirs() -> list[Path]:
    home = Path.home()
    sysname = platform.system()
    if sysname == "Darwin":
        return [home / "Library/Audio/Plug-Ins/VST3", Path("/Library/Audio/Plug-Ins/VST3")]
    if sysname == "Windows":
        return [Path(os.environ.get("COMMONPROGRAMFILES", r"C:\Program Files\Common Files")) / "VST3"]
    return [home / ".vst3", Path("/usr/lib/vst3"), Path("/usr/local/lib/vst3")]


def norm(text: str) -> str:
    """'Filter 1 Cutoff' -> 'filter_1_cutoff' (how plugin parameters are written in Pulsar)."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def key(name: str) -> str:
    n = name.strip().lower()
    return ALIASES.get(n, n)


def locate(name: str) -> Path | None:
    """Where a plugin is installed (Pulsar's own copy first), or None."""
    p = Path(name).expanduser()
    if p.suffix in (".vst3", ".component", ".so", ".dll") and p.exists():
        return p
    k = key(name)
    if k not in KNOWN:
        return None
    for d in [ROOT / k] + system_dirs():
        for bundle in KNOWN[k][1]:
            if (d / bundle).exists():
                return d / bundle
    return None


def available() -> list[tuple[str, str, Path | None]]:
    return [(k, info[0], locate(k)) for k, info in KNOWN.items()]


# -- installing Surge XT -----------------------------------------------------------------

def _download(url: str, dest: Path, log):
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"  downloading {url.rsplit('/', 1)[-1]} (once)...")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise PulsarError(f"could not download {url} ({e})") from None
    os.replace(tmp, dest)


def _extract(archive: Path, into: Path):
    into.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        if platform.system() == "Darwin" and shutil.which("ditto"):
            # ditto keeps the bundles' symlinks and code signatures intact
            subprocess.run(["ditto", "-x", "-k", str(archive), str(into)], check=True)
        else:
            with zipfile.ZipFile(archive) as z:
                z.extractall(into)
    else:
        with tarfile.open(archive) as t:
            t.extractall(into, filter="tar") if hasattr(tarfile, "data_filter") else t.extractall(into)


def surge_data() -> Path:
    """Where Surge XT's factory patches and wavetables live for Pulsar's copy."""
    return ROOT / "surge" / "SurgeXTData"


def install_surge(log=print) -> Path:
    """Download Surge XT (plugin + factory content) into Pulsar's cache, if it isn't there yet."""
    dest = ROOT / "surge"
    vst3 = dest / "Surge XT.vst3"
    sysname = platform.system()
    if sysname not in SURGE_FILES:
        raise PulsarError(f"Pulsar can't install Surge XT on {sysname} automatically",
                          hint="install it from https://surge-synthesizer.github.io")
    if not vst3.exists():
        arch = ROOT / "dl" / SURGE_FILES[sysname]
        _download(SURGE_BASE + SURGE_FILES[sysname], arch, log)
        tmp = ROOT / "dl" / "surge-unpack"
        shutil.rmtree(tmp, ignore_errors=True)
        _extract(arch, tmp)
        found = next(iter(sorted(tmp.rglob("Surge XT.vst3"))), None)
        if found is None:
            raise PulsarError(f"no 'Surge XT.vst3' inside {arch.name}")
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(found), str(vst3))
        shutil.rmtree(tmp, ignore_errors=True)
        if sysname == "Darwin":  # downloaded bundles are quarantined; Pulsar's copy is only loaded by Pulsar
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(vst3)], capture_output=True)
    if not surge_data().exists():
        arch = ROOT / "dl" / SURGE_CONTENT
        _download(SURGE_BASE + SURGE_CONTENT, arch, log)
        tmp = ROOT / "dl" / "surge-content"
        shutil.rmtree(tmp, ignore_errors=True)
        _extract(arch, tmp)
        found = next((p for p in sorted(tmp.rglob("*")) if p.is_dir() and (p / "patches_factory").exists()), None)
        if found is None:
            raise PulsarError(f"no factory patches inside {arch.name}")
        shutil.move(str(found), str(surge_data()))
        shutil.rmtree(tmp, ignore_errors=True)
    link_surge_data()
    return vst3


def link_surge_data():
    """Surge XT looks for its content in the user's documents/app-support folders; point it at ours."""
    target = surge_data()
    if not target.exists():
        return
    sysname = platform.system()
    if sysname == "Darwin":
        place = Path.home() / "Library/Application Support/Surge XT"
    elif sysname == "Linux":
        place = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "surge-xt"
    else:
        place = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Surge XT"
    if place.exists() or place.is_symlink():
        return  # a real Surge XT installation (or our link) is already there
    place.parent.mkdir(parents=True, exist_ok=True)
    try:
        place.symlink_to(target, target_is_directory=True)
    except OSError:
        pass


def ensure(name: str, log=print) -> Path:
    """The plugin's path, installing it first when Pulsar can."""
    found = locate(name)
    if found is not None:
        if key(name) == "surge":
            link_surge_data()
        return found
    k = key(name)
    if k == "surge":
        return install_surge(log)
    if k in KNOWN:
        title, _, _, lic, url = KNOWN[k]
        raise PulsarError(f"{title} isn't installed", hint=f"install it from {url} ({lic}), or use plugin(\"surge\")")
    raise PulsarError(f"no plugin named {name!r}",
                      hint=suggest(k, list(KNOWN)) or "use surge, vital, serum, or a path to a .vst3")


# -- presets -------------------------------------------------------------------------------

def preset_dirs(name: str) -> list[Path]:
    k = key(name)
    home = Path.home()
    if k == "surge":
        roots = [surge_data(), home / "Library/Application Support/Surge XT", Path("/Library/Application Support/Surge XT"),
                 home / ".local/share/surge-xt", Path("/usr/share/surge-xt")]
        user = [home / "Documents/Surge XT/Patches", home / "Documents/Surge XT"]
        return [d for r in roots for d in (r / "patches_factory", r / "patches_3rdparty")] + user
    if k == "vital":
        return [home / "Music/Vital", home / "Documents/Vital", home / ".local/share/vital"]
    if k == "serum":
        return [home / "Documents/Xfer/Serum 2 Presets", home / "Documents/Xfer/Serum Presets"]
    return []


PRESET_EXT = (".fxp", ".vstpreset", ".vital")


def presets(name: str) -> list[Path]:
    """Every preset file, once (the same folder can be reachable through a link)."""
    out, seen = [], set()
    for d in preset_dirs(name):
        if d.exists():
            for p in sorted(d.rglob("*")):
                if p.suffix.lower() in PRESET_EXT and p.resolve() not in seen:
                    seen.add(p.resolve())
                    out.append(p)
    return out


def find_preset(name: str, query: str) -> Path:
    """A preset by path, or by name: 'Leads/Acid Lead', 'acid lead' (case and punctuation don't matter)."""
    p = Path(query).expanduser()
    if p.exists():
        return p
    allp = presets(name)
    if not allp:
        raise PulsarError(f"no presets found for {name}", hint="give a preset file path instead")
    want = norm(query)
    def rel(x: Path) -> str:
        for d in preset_dirs(name):
            if d.exists() and str(x).startswith(str(d)):
                return norm(str(x.relative_to(d).with_suffix("")))
        return norm(x.stem)
    # exact: the whole name, or a trailing folder/name path ("Leads/Acid Lead" inside a bigger tree)
    exact = [x for x in allp if rel(x) == want or norm(x.stem) == want
             or ("_" in want and rel(x).endswith("_" + want) and "/" in query)]
    if exact:
        return sorted(exact, key=lambda x: len(str(x)))[0]
    part = [x for x in allp if want in rel(x)]
    if len(part) == 1:
        return part[0]
    names = [x.stem for x in allp]
    raise PulsarError(f"no {name} preset matches {query!r}" + (f" ({len(part)} partly do)" if part else ""),
                      hint=suggest(query, names) or "pulsar plugins presets " + name)


# -- preset files -> plugin state ----------------------------------------------------------
#
# VST3 hosts can't load a VST2-style .fxp preset (Surge's patch format) directly. JUCE-built plugins
# (Surge XT, Vital) store their VST3 state as "VC2!" + size + XML holding the plugin's own chunk,
# base64-encoded in JUCE's alphabet; that chunk is exactly what the .fxp carries. So a preset
# becomes a state file Pulsar can load.

JUCE_B64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"
JUCE_TRAILER = b"\0" * 16 + b"JUCEPrivateData"


def juce_b64encode(data: bytes) -> str:
    """JUCE MemoryBlock::toBase64Encoding: '<size>.' then 6 bits per character, least significant first."""
    bits = int.from_bytes(data, "little")
    n = (len(data) * 8 + 5) // 6
    return f"{len(data)}." + "".join(JUCE_B64[(bits >> (6 * i)) & 63] for i in range(n))


def juce_b64decode(text: str) -> bytes:
    size, _, chars = text.strip().partition(".")
    bits = 0
    for i, ch in enumerate(chars):
        bits |= JUCE_B64.index(ch) << (6 * i)
    return (bits & ((1 << (8 * int(size))) - 1)).to_bytes(int(size), "little")


def fxp_chunk(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] != b"CcnK" or data[8:12] != b"FPCh":
        raise PulsarError(f"{path.name} isn't an .fxp program-chunk preset")
    size = struct.unpack(">I", data[56:60])[0]
    return data[60:60 + size]


def state_from_fxp(path: Path) -> Path:
    """A VST3 state file (cached) that loads this .fxp preset into a JUCE-based plugin."""
    chunk = fxp_chunk(path)
    out = ROOT / "states" / (hashlib.sha1(chunk).hexdigest()[:16] + ".state")
    if not out.exists():
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n\n<VST3PluginState><IComponent>'
               + juce_b64encode(chunk + JUCE_TRAILER) + "</IComponent></VST3PluginState>").encode() + b"\0"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(b"VC2!" + struct.pack("<I", len(xml) - 1) + xml)
        os.replace(tmp, out)
    return out
