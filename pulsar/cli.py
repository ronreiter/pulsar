"""pulsar: render, check and inspect Pulsar songs."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from .errors import PulsarError


def load(path: str, only: str | None = None, source: str | None = None):
    from .song import build_score

    src = source if source is not None else Path(path).read_text()
    song, score, output = build_score(src, path, only)
    return src, song, score, output


def cmd_check(args):
    src, song, score, output = load(args.file)
    for line in output:
        print(line)
    from .analyze import describe_score
    print(describe_score(score))
    return 0


def cmd_render(args):
    import soundfile as sf

    from .render import render

    t0 = time.time()
    src, song, score, output = load(args.file, args.only, sys.stdin.read() if args.stdin else None)
    for line in output:
        print(line)
    out = Path(args.output or Path(args.file).with_suffix(".wav"))
    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    log(f"rendering {args.file}: {len(score.sections)} sections, {score.total_bars:g} bars, "
        f"{score.seconds:.1f}s at {song.tempo:g} BPM")
    res = render(score, stems=bool(args.stems), log=log, jobs=args.jobs, rebuild=(src, args.file, args.only))
    from .export import write_audio
    title = Path(args.file).stem.replace("_", " ")
    write_audio(out, res.mix, res.sr, title)
    if args.mp3 and out.suffix.lower() != ".mp3":
        write_audio(out.with_suffix(".mp3"), res.mix, res.sr, title)
        print(f"wrote {out.with_suffix('.mp3')}", file=sys.stderr)
    if res.info.get("credits"):
        cred = out.with_name(out.stem + ".credits.txt")
        lines = [f"{Path(args.file).stem} -- sounds used", ""]
        lines += [f"{c['track']}: {c['credit']}" for c in res.info["credits"]]
        cred.write_text("\n".join(lines) + "\n")
        print(f"wrote {cred} (credit these recordings when you share the track)", file=sys.stderr)
    if args.stems:
        d = Path(args.stems)
        d.mkdir(parents=True, exist_ok=True)
        for name, a in res.stems.items():
            sf.write(d / f"{name}.wav", a.T, res.sr, subtype="PCM_24")
    from .analyze import analyze, format_report
    report = analyze(res.mix, res.sr, score, res)
    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        print(format_report(report))
    print(f"wrote {out} ({res.info['seconds']}s) in {time.time() - t0:.1f}s", file=sys.stderr)
    if args.play:
        subprocess.run(["afplay", str(out)] if sys.platform == "darwin" else ["ffplay", "-autoexit", "-nodisp", str(out)])
    return 0


def cmd_analyze(args):
    import soundfile as sf

    from .analyze import analyze, format_report

    audio, sr = sf.read(args.file, always_2d=True)
    report = analyze(audio.T.astype("float32"), sr)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


def score_json(path: str, source: str | None = None) -> dict:
    """The compiled song as plain data for editors and visualizers."""
    from .song import build_score, span_value

    src = source if source is not None else Path(path).read_text()
    try:
        song, score, output = build_score(src, path)
    except PulsarError as e:
        return {"ok": False, "error": {"message": e.msg, "line": e.loc[0] if e.loc else None,
                                       "column": e.loc[1] if e.loc else None, "hint": e.hint,
                                       "text": e.format(src, path)}}
    tracks = []
    for name, tr in song.tracks.items():
        inst = tr.inst
        if inst.kind == "drums":
            lane = "drums"
        elif inst.kind in ("sampler", "ambience"):
            lane = "sampler"
        else:
            lane = "notes"
        notes = [{"bar": round(n.bar, 5), "dur_bars": round(n.dur / song.bar_seconds, 5),
                  "pitch": n.pitch, "drum": n.drum, "vel": round(n.vel, 3), "slice": n.ctrl.get("slice"),
                  "slide": bool(n.ctrl.get("slide")), "accent": bool(n.ctrl.get("accent"))}
                 for n in score.notes.get(name, [])]
        tracks.append({"name": name, "lane": lane, "preset": inst.preset, "engine": inst.engine,
                       "effects": [e.name for e in inst.inserts], "gain": float(tr.mixer.get("gain", 0)),
                       "pan": float(tr.mixer.get("pan", 0)),
                       "sends": {b: float(tr.mixer[b]) for b in song.buses if tr.mixer.get(b)},
                       "duck": float(tr.mixer.get("duck", 0) or 0), "line": tr.loc[0] if tr.loc else None,
                       "notes": notes})
    autos = []
    step = 1 / 16
    for (track, path_), spans in score.autos.items():
        lo = min(s.start for s in spans)
        hi = max(s.end for s in spans)
        pts = []
        b = lo
        while b <= hi + 1e-9:
            v = span_value(spans, b, float("nan"))
            if v == v:
                pts.append([round(b, 4), round(v, 4)])
            b += step
        label = ".".join(str(p) for p in path_ if p not in ("synth", "mixer", "fx") and not isinstance(p, int))
        if path_ and path_[0] == "fx":
            tr = song.tracks.get(track)
            if tr is not None:
                label = f"{tr.inst.inserts[path_[1]].name}.{path_[2]}"
        autos.append({"track": track, "param": label, "points": pts})
    return {"ok": True, "tempo": song.tempo, "key": song.key_name, "bar_seconds": song.bar_seconds,
            "total_bars": score.total_bars, "seconds": score.seconds,
            "sections": [{"name": n, "bar": b, "bars": l} for n, b, l in score.sections],
            "tracks": tracks, "automation": autos, "output": output}


def cmd_score(args):
    data = score_json(args.file, sys.stdin.read() if args.stdin else None)
    print(json.dumps(data, separators=(",", ":")))
    return 0 if data["ok"] else 2


def cmd_compare(args):
    import soundfile as sf

    from .analyze import compare, profile

    profs = []
    for f in (args.mine, args.reference):
        audio, sr = sf.read(f, always_2d=True, dtype="float32")
        profs.append(profile(audio.T, sr))
    if args.json:
        print(json.dumps({"yours": profs[0], "reference": profs[1]}, indent=2))
    else:
        print("\n".join(compare(*profs)))
    return 0


def cmd_match(args):
    from .match import match

    match(args.file, args.reference, args.iterations, args.tolerance, out_wav=args.output)
    return 0


def cmd_timbre(args):
    import soundfile as sf

    from . import theory, timbre
    from .export import write_audio

    audio, sr = sf.read(args.reference, always_2d=True, dtype="float32")
    start = int(args.start * sr)
    audio = audio.T[:, start:start + int(args.seconds * sr)]
    fp = timbre.fingerprint(audio, sr)
    print("reference:", timbre.describe(fp))
    lo, _, hi = args.register.partition("-")
    reg = (theory.note_number(lo), theory.note_number(hi))
    if None in reg:
        raise PulsarError("--register takes two note names like c3-c5")
    m = timbre.PatchMatcher(fp, reg, log=print, role=args.role)
    best, d, parts = m.search(random_trials=args.trials, rounds=args.rounds)
    print(f"best distance {d:.2f} {parts} after {m.evals} renders")
    print(timbre.patch_to_pulse(best))
    if args.output:
        write_audio(args.output, m.render(best), 48000)
        print(f"wrote {args.output} (a generated test phrase played with the matched patch)")
    return 0


def cmd_voices(args):
    from . import tts
    names = tts.voices()
    groups = {"af": "American female", "am": "American male", "bf": "British female", "bm": "British male"}
    for prefix, label in groups.items():
        print(f"{label:16} " + " ".join(n for n in names if n.startswith(prefix + "_")))
    other = [n for n in names if n[:2] not in groups]
    if other:
        print(f"{'other languages':16} " + " ".join(other))
    return 0


def cmd_sounds(args):
    from . import stock
    from .errors import PulsarError

    try:
        results = stock.search(args.query, args.license)
    except PulsarError as e:
        print(f"error: {e.msg}", file=sys.stderr)
        return 1
    if not results:
        print(f"no {args.license}-licensed recordings found for {args.query!r}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([{k: v for k, v in vars(r).items()} for r in results[:args.limit]], indent=2))
        return 0
    for i, r in enumerate(results[:args.limit]):
        print(f"pick: {i:<3} {r.duration:6.0f}s  {r.license:<12} {r.title}")
        print(f"{'':9}id: {r.id!r}  by {r.creator or 'unknown'}  {r.page_url}")
    print(f'\nuse: track birds = ambience("{args.query}", pick: 0)   '
          f"(credits are written next to the render)", file=sys.stderr)
    return 0


def cmd_clip(args):
    """Read one band (a track in a section) as notes, or write edited notes back into the source."""
    from .clip import read_band, write_band
    from .errors import PulsarError
    from .interp import run

    src = sys.stdin.read() if args.stdin else Path(args.file).read_text()
    try:
        song, _ = run(src, args.file)
        if args.write:
            notes = json.loads(Path(args.write).read_text() if args.write != "-" else "[]")
            new = write_band(src, song, args.section, args.track, notes)
            run(new, args.file)  # never hand back source that doesn't compile
            print(json.dumps({"ok": True, "source": new}))
        else:
            print(json.dumps(read_band(song, args.section, args.track, args.at)))
    except PulsarError as e:
        print(json.dumps({"ok": False, "error": e.msg, "hint": e.hint}))
        return 1
    return 0


def cmd_presets(args):
    from . import presets

    if args.name:
        p = presets.SYNTHS.get(args.name) or presets.KITS.get(args.name)
        if p is None:
            print(f"no preset or kit named {args.name!r}", file=sys.stderr)
            return 1
        print(json.dumps(p, indent=2, default=str))
        return 0
    print("synth presets  (synth(\"name\", param: value, ...))")
    for name, p in presets.SYNTHS.items():
        print(f"  {name:12} {p['engine']:7} oct {p.get('octave', 4)}  {p['desc']}")
    print("\ndrum kits  (drums(\"name\", bd_decay: 0.5, hh_gain: -3, ...))")
    for name, k in presets.KITS.items():
        print(f"  {name:12} {k['desc']}")
    return 0


def cmd_params(args):
    from .dsp import effects as FX, faust as FZ

    tables = {"analog": FZ.ANALOG_PARAMS, "fm": FZ.FM_PARAMS, "pluck": FZ.PLUCK_PARAMS}
    for engine, table in tables.items():
        print(f"{engine} engine parameters (name default [min, max])")
        for k, (d, lo, hi) in table.items():
            print(f"  {k:12} {d:<8g} [{lo:g}, {hi:g}]")
    print("\ndrum voices (use as voice_param in drums(): bd_decay, hh_tune, ...; every voice also takes _gain in dB)")
    for v, table in FZ.DRUM_PARAMS.items():
        print(f"  {v:4} " + ", ".join(f"{k}={d:g}" for k, (d, lo, hi) in table.items()))
    print("\neffects")
    for name, (_, table, _) in FX.EFFECTS.items():
        print(f"  {name:8} " + ", ".join(f"{k}={d:g}" for k, (d, lo, hi) in table.items()))
    return 0


def cmd_docs(args):
    from .docs import reference
    print(reference())
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pulsar", description="Render electronic music from Pulsar (.pulse) files.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="render a song to WAV and print a mix report")
    r.add_argument("file")
    r.add_argument("-o", "--output", help="output file; .wav (24-bit) or .mp3 (320 kbps)")
    r.add_argument("--mp3", action="store_true", help="also write an MP3 next to the WAV")
    r.add_argument("--stems", metavar="DIR", help="also write per-track stems to DIR")
    r.add_argument("--play", action="store_true", help="play the result when done")
    r.add_argument("--json", action="store_true", help="print the report as JSON")
    r.add_argument("-q", "--quiet", action="store_true")
    r.add_argument("--stdin", action="store_true", help="read the source from stdin (file is used for relative paths)")
    r.add_argument("--only", metavar="SECTIONS", help="render only these sections (comma-separated), for quick checks")
    r.add_argument("-j", "--jobs", type=int, help="parallel track renders (default: one per CPU core, max 8)")
    r.set_defaults(fn=cmd_render)
    c = sub.add_parser("check", help="parse and compile without rendering; prints the song structure")
    c.add_argument("file")
    c.set_defaults(fn=cmd_check)
    a = sub.add_parser("analyze", help="analyze any WAV file")
    a.add_argument("file")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_analyze)
    sc = sub.add_parser("score", help="print the compiled song (sections, tracks, notes, automation) as JSON")
    sc.add_argument("file")
    sc.add_argument("--stdin", action="store_true", help="read the source from stdin (file is used for paths)")
    sc.add_argument("--json", action="store_true", help="(default) JSON output")
    sc.set_defaults(fn=cmd_score)
    cp = sub.add_parser("compare", help="compare a render's sonic profile to a reference track")
    cp.add_argument("mine")
    cp.add_argument("reference")
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(fn=cmd_compare)
    mt = sub.add_parser("match", help="iterate a master EQ so a song's tonal balance matches a reference")
    mt.add_argument("file")
    mt.add_argument("reference")
    mt.add_argument("-n", "--iterations", type=int, default=4)
    mt.add_argument("--tolerance", type=float, default=1.5, help="stop when every band is within this many dB")
    mt.add_argument("-o", "--output", help="output audio (.wav or .mp3)")
    mt.set_defaults(fn=cmd_match)
    tb = sub.add_parser("timbre", help="search a synth patch whose sound matches a recording (e.g. a stem)")
    tb.add_argument("reference")
    tb.add_argument("--register", default="c3-c5", help="note range for the test phrase, e.g. e1-e2 for bass")
    tb.add_argument("--role", choices=["lead", "bass"], default="lead")
    tb.add_argument("--start", type=float, default=0.0, help="seconds into the file to analyse from")
    tb.add_argument("--seconds", type=float, default=120.0)
    tb.add_argument("--trials", type=int, default=40)
    tb.add_argument("--rounds", type=int, default=4)
    tb.add_argument("-o", "--output", help="write the matched patch playing a test phrase")
    tb.set_defaults(fn=cmd_timbre)
    sub.add_parser("voices", help="list the text-to-speech voices for voice()").set_defaults(fn=cmd_voices)
    cl = sub.add_parser("clip", help="read a track's notes in one section as JSON, or write edited notes back")
    cl.add_argument("file")
    cl.add_argument("--section", required=True)
    cl.add_argument("--track", required=True)
    cl.add_argument("--at", type=float, help="the bar where this instance of the section starts")
    cl.add_argument("--write", help="a JSON file of notes [{beat, note, len, vel, flags}]: prints the new source")
    cl.add_argument("--stdin", action="store_true", help="read the source from stdin (file is used for paths)")
    cl.set_defaults(fn=cmd_clip)
    sd = sub.add_parser("sounds", help="search free stock recordings (birds, water, rain...) for ambience()")
    sd.add_argument("query")
    sd.add_argument("--license", choices=["cc0", "open", "by-sa"], default="open",
                    help="cc0: no attribution; open (default): CC0 + CC BY; by-sa: also share-alike")
    sd.add_argument("--limit", type=int, default=10)
    sd.add_argument("--json", action="store_true")
    sd.set_defaults(fn=cmd_sounds)
    p = sub.add_parser("presets", help="list synth presets and drum kits")
    p.add_argument("name", nargs="?")
    p.set_defaults(fn=cmd_presets)
    sub.add_parser("params", help="list every engine, drum and effect parameter").set_defaults(fn=cmd_params)
    sub.add_parser("docs", help="print the language reference").set_defaults(fn=cmd_docs)
    m = sub.add_parser("mcp", help="run the MCP server (stdio) so AI agents can compose and render")
    m.set_defaults(fn=lambda a: __import__("pulsar.mcp_server", fromlist=["main"]).main())
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except PulsarError as e:
        src = None
        f = getattr(args, "file", None)
        if f and Path(f).exists():
            src = Path(f).read_text()
        print(e.format(src, f or "<song>"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
