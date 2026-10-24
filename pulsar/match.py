"""Match a song's tonal balance to a reference track by iterating a master EQ.

Only the sonic profile is matched (spectral balance, width and dynamics as
numbers); the song's notes are never touched.
"""
from __future__ import annotations

import time
from pathlib import Path

from .analyze import compare, profile
from .song import Effect, build_score

BANDS = ("sub", "bass", "low_mid", "mid", "presence", "high", "air")


def match(song_path: str, reference: str, iterations: int = 4, tolerance: float = 1.5, log=print,
          out_wav: str | None = None) -> dict:
    import soundfile as sf

    from .export import write_audio
    from .render import render

    ref_audio, ref_sr = sf.read(reference, always_2d=True, dtype="float32")
    ref = profile(ref_audio.T, ref_sr)
    log(f"reference: {ref['tempo_bpm']} BPM, {ref['key']}, centroid {ref['centroid_hz']} Hz")
    source = Path(song_path).read_text()
    gains = {b: 0.0 for b in BANDS}
    history = []
    best = None
    for it in range(1, iterations + 1):
        t0 = time.time()
        song, score, _ = build_score(source, song_path)
        song.master = list(song.master) + [Effect("tone", {k: round(v, 2) for k, v in gains.items()})]
        res = render(score, jobs=None, rebuild=(source, song_path, None))
        mine = profile(res.mix, res.sr)
        diff = {b: mine["spectrum_db"][b] - ref["spectrum_db"][b] for b in BANDS}
        dist = max(abs(v) for v in diff.values())
        history.append({"iteration": it, "max_band_error_db": round(dist, 2), "gains": dict(gains),
                        "diff": {k: round(v, 1) for k, v in diff.items()}})
        log(f"iteration {it}: worst band {dist:.1f} dB off  " +
            "  ".join(f"{b} {diff[b]:+.1f}" for b in BANDS) + f"  ({time.time() - t0:.0f}s)")
        if best is None or dist < best[0]:
            best = (dist, dict(gains), res, mine)
        if dist <= tolerance:
            break
        # Move each band toward the reference; spectra are relative to the loudest
        # band, so remove the common offset to avoid drifting the overall level.
        step = {b: -0.8 * diff[b] for b in BANDS}
        mean = sum(step.values()) / len(step)
        for b in BANDS:
            gains[b] = max(-12.0, min(12.0, gains[b] + step[b] - mean))
    dist, gains, res, mine = best
    line = "master = tone(" + ", ".join(f"{b}: {gains[b]:.1f}" for b in BANDS) + ")"
    matched = Path(song_path).with_suffix(".matched.pulse")
    if "\nmaster =" in source or source.startswith("master ="):
        note = "// the song already had a master chain; this tone() runs after it\n"
        line_out = note + line.replace("master = ", "master = ") 
    else:
        line_out = line
    matched.write_text(source.rstrip() + "\n\n// tonal balance matched to " + Path(reference).name +
                       f" (worst band {dist:.1f} dB)\n" + line_out + "\n")
    wav = Path(out_wav) if out_wav else matched.with_suffix(".wav")
    write_audio(wav, res.mix, res.sr, Path(song_path).stem)
    log("\n".join(compare(mine, ref)))
    log(f"wrote {matched} and {wav}")
    return {"history": history, "gains": gains, "matched": str(matched), "wav": str(wav)}
