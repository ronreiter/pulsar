"""Write rendered audio: 24-bit WAV, or MP3 through ffmpeg's LAME encoder."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from .errors import PulsarError

FORMATS = ("wav", "mp3")


def write_audio(path, audio: np.ndarray, sr: int, title: str | None = None, bitrate: str = "320k") -> Path:
    """Write (2, n) float audio. The format follows the file extension."""
    path = Path(path)
    fmt = path.suffix.lower().lstrip(".")
    if fmt == "wav":
        sf.write(path, audio.T, sr, subtype="PCM_24")
        return path
    if fmt != "mp3":
        raise PulsarError(f"can't write {path.name}: use a .wav or .mp3 file name")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise PulsarError("MP3 export needs ffmpeg on PATH (macOS: brew install ffmpeg)")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "f32le", "-ar", str(sr), "-ac", str(audio.shape[0]), "-i", "-",
           "-c:a", "libmp3lame", "-b:a", bitrate, "-id3v2_version", "3"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    cmd.append(str(path))
    pcm = np.ascontiguousarray(audio.T, dtype="<f4").tobytes()
    proc = subprocess.run(cmd, input=pcm, capture_output=True)
    if proc.returncode != 0:
        raise PulsarError(f"ffmpeg failed to write {path.name}: {proc.stderr.decode(errors='replace').strip()}")
    return path
