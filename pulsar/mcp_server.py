"""MCP server: lets an AI agent read the reference, compose, render and analyze.

Run with `pulsar mcp` (stdio). Rendered files go to $PULSAR_OUT (default
./pulsar-out).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP as Server

from .errors import PulsarError

OUT = Path(os.environ.get("PULSAR_OUT", "pulsar-out")).resolve()

server = Server(
    "pulsar",
    instructions=(
        "Pulsar renders electronic music from a small text language. Read `reference` once, write a song, "
        "call `check` until it compiles, then `render`. Use the mix report (loudness, spectrum, energy arc, "
        "suggestions) to revise and render again."
    ),
)


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-") or "song"


@server.tool()
def reference() -> str:
    """The complete Pulsar language reference, including every preset, kit, effect and parameter."""
    from .docs import reference as ref
    return ref()


@server.tool()
def check(source: str) -> str:
    """Compile a song without rendering. Returns the structure (sections, tracks, note ranges) or the errors."""
    from .analyze import describe_score
    from .interp import run
    from .song import compile_song

    try:
        song, output = run(source)
        score = compile_song(song)
    except PulsarError as e:
        return e.format(source)
    return "\n".join(output + [describe_score(score)])


@server.tool()
def render(source: str, name: str = "song", stems: bool = False, format: str = "wav") -> str:
    """Render a song to a mastered 48 kHz file: format "wav" (24-bit) or "mp3" (320 kbps).
    Returns the file path and a mix report with suggestions."""
    import soundfile as sf

    from .analyze import analyze, format_report
    from .interp import run
    from .render import render as do_render
    from .song import compile_song

    try:
        song, output = run(source)
        score = compile_song(song)
    except PulsarError as e:
        return e.format(source)
    OUT.mkdir(parents=True, exist_ok=True)
    base = _slug(name)
    (OUT / f"{base}.pulse").write_text(source)
    try:
        res = do_render(score, stems=stems, rebuild=(source, "<song>", None))
    except PulsarError as e:
        return e.format(source)
    from .export import FORMATS, write_audio
    if format not in FORMATS:
        return f"format must be one of {', '.join(FORMATS)}"
    path = write_audio(OUT / f"{base}.{format}", res.mix, res.sr, name)
    if stems:
        d = OUT / f"{base}-stems"
        d.mkdir(exist_ok=True)
        for k, a in res.stems.items():
            sf.write(d / f"{k}.wav", a.T, res.sr, subtype="PCM_24")
    report = format_report(analyze(res.mix, res.sr, score, res))
    return "\n".join(output + [f"wrote {path} ({res.info['seconds']}s, rendered in {res.info['render_seconds']}s)",
                               report])


@server.tool()
def analyze_file(path: str) -> str:
    """Analyze any WAV file: loudness, true peak, spectrum balance, stereo image."""
    import soundfile as sf

    from .analyze import analyze, format_report

    audio, sr = sf.read(path, always_2d=True)
    return format_report(analyze(audio.T.astype("float32"), sr))


@server.tool()
def play(path: str) -> str:
    """Play a rendered WAV on this computer's speakers (blocks until it finishes)."""
    p = Path(path)
    if not p.exists():
        return f"no such file: {path}"
    cmd = ["afplay", str(p)] if sys.platform == "darwin" else ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet", str(p)]
    subprocess.run(cmd)
    return f"played {p}"


def main():
    server.run("stdio")
    return 0


if __name__ == "__main__":
    main()
