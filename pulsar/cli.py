"""pulsar: render, check and inspect Pulsar songs."""
from __future__ import annotations

import argparse
import json
import shutil
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
        if inst.kind == "drums" or (inst.kind == "orchestra" and inst.sample.get("kit")):
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


def cmd_plugins(args):
    from . import plugins
    from .errors import PulsarError

    try:
        if args.action in (None, "list"):
            print("plugin instruments  (track lead = plugin(\"surge\", preset: \"...\"))")
            for k, title, path in plugins.available():
                auto = plugins.KNOWN[k][2]
                where = str(path) if path else ("not installed -- downloads on first use" if auto else
                                                f"not installed ({plugins.KNOWN[k][4]})")
                print(f"  {k:8} {title:10} {where}")
            return 0
        name = args.name or "surge"
        if args.action == "install":
            print(plugins.ensure(name, lambda m: print(m, file=sys.stderr)))
            return 0
        if args.action == "presets":
            plugins.ensure(name, lambda m: print(m, file=sys.stderr))
            q = plugins.norm(args.query or "")
            shown = 0
            for pth in plugins.presets(name):
                rel = next((str(pth.relative_to(d).with_suffix("")) for d in plugins.preset_dirs(name)
                            if d.exists() and str(pth).startswith(str(d))), pth.stem)
                if q in plugins.norm(rel):
                    print(f"  {rel}")
                    shown += 1
            print(f"{shown} presets", file=sys.stderr)
            return 0
        if args.action == "params":
            import dawdreamer as daw
            path = plugins.ensure(name, lambda m: print(m, file=sys.stderr))
            eng = daw.RenderEngine(48000, 512)  # keep the engine alive: it owns the plugin
            proc = eng.make_plugin_processor("p", str(path))
            q = plugins.norm(args.query or "")
            for d in proc.get_parameters_description():
                n = plugins.norm(d["name"])
                if q in n:
                    print(f"  {n:40} {proc.get_parameter_text(d['index'])}")
            return 0
    except PulsarError as e:
        print(f"error: {e.msg}" + (f"\n  hint: {e.hint}" if e.hint else ""), file=sys.stderr)
        return 1
    return 0


def cmd_library(args):
    """Search the open sound library, fetch a sound, or print the code that uses it."""
    from dataclasses import asdict

    from . import library
    from .errors import PulsarError

    try:
        if args.action == "search":
            items = library.search(args.query or "", args.source, args.limit, args.license)
            if args.json:
                print(json.dumps([dict(asdict(i), **library.snippet(i)) for i in items]))
                return 0
            for i in items:
                dur = f"{i.duration:5.1f}s" if i.duration else "      "
                print(f"{i.source:9} {i.license:12} {dur}  {i.title}")
                print(f"{'':9} {i.ref}")
            return 0
        if args.action == "fetch":
            path, it = library.fetch(args.query, lambda m: print(m, file=sys.stderr))
            print(json.dumps({"path": str(path), **asdict(it)}) if args.json else path)
            return 0
        if args.action == "snippet":
            it = library.item(args.query)
            sn = library.snippet(it)
            print(json.dumps(sn) if args.json else sn["track"] + "\n" + sn["play"])
            return 0
    except PulsarError as e:
        if args.json:
            print(json.dumps({"error": e.msg, "hint": e.hint}))
        print(f"error: {e.msg}", file=sys.stderr)
        return 1
    return 0


def cmd_import_ableton(args):
    """Write a Pulsar song from an Ableton Live set (notes in clips; devices and mix from the set)."""
    from .ableton.importer import write_pulse
    from .errors import PulsarError

    out = Path(args.output or Path(args.set).with_suffix(".pulse").name)
    try:
        write_pulse(args.set, out, args.calibration)
    except (PulsarError, OSError, KeyError) as e:
        print(f"error: {getattr(e, 'msg', e)}", file=sys.stderr)
        return 1
    print(f"wrote {out} -- render it with: pulsar render {out} --mp3", file=sys.stderr)
    return 0


def cmd_generate(args):
    """Generate a sound effect or instrumental texture locally with Stable Audio 3."""
    from . import generate
    from .errors import PulsarError

    log = lambda m: print(m, file=sys.stderr)
    try:
        if args.setup:
            generate.setup(log)
            print(f"Stable Audio 3 is installed. The weights are gated: accept the license at {generate.PAGES[args.model]}"
                  f" and log in once with: {generate.ENV / 'bin' / 'hf'} auth login", file=sys.stderr)
            return 0
        if not args.prompt:
            print("error: describe the sound, e.g. pulsar generate \"rain on a tin roof\"", file=sys.stderr)
            return 1
        path = generate.generate(args.prompt, args.model, args.seconds, args.seed, log)
        if args.output:
            shutil.copyfile(path, args.output)
            path = Path(args.output)
        print(path)
        if args.play:
            subprocess.run(["afplay", str(path)] if sys.platform == "darwin" else ["ffplay", "-autoexit", "-nodisp", str(path)])
    except PulsarError as e:
        print(f"error: {e.msg}" + (f"\n  hint: {e.hint}" if e.hint else ""), file=sys.stderr)
        return 1
    return 0


def cmd_setup(args):
    """Check the installation and fetch the instruments, so the first render doesn't wait."""
    import shutil as _sh

    from . import orchestra, piano, plugins
    ok = True
    print(f"pulsar {_version()} on Python {sys.version.split()[0]}")
    for tool, why in (("ffmpeg", "MP3 export and stock sounds"),):
        found = _sh.which(tool)
        print(f"  {'ok ' if found else 'missing'} {tool:8} {found or 'needed for ' + why + ' (macOS: brew install ffmpeg)'}")
        ok &= bool(found)
    log = lambda m: print(m)
    if args.minimal:
        return 0 if ok else 1
    print("instruments (downloaded once into ~/.cache/pulsar):")
    plugins.ensure("surge", log)
    print("  ok  Surge XT")
    piano.install("upright", log)
    print("  ok  upright piano")
    if args.all:
        piano.install("grand", log)
        print("  ok  grand piano")
        for name in orchestra.names():
            orchestra.install(name, log)
        print("  ok  orchestra")
    print("ready" if ok else "ready, but see the missing tools above")
    return 0 if ok else 1


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("pulsar-music")
    except Exception:  # noqa: BLE001
        return "dev"


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
    from . import orchestra, piano
    print("\nsampled pianos  (piano(\"name\", pedal: 0.4))")
    for name, info in piano.PIANOS.items():
        if name != "accordion":
            print(f"  {name:12} {info['title']} ({info['license']}, {info['size']} once)")
    print("\nsampled accordion  (accordion(gain: -2, dynamics: 2); a Hohner button accordion, CC0, 5 MB once)")
    print("\nsampled orchestra  (orchestra(\"name\", gain: -2); VSCO-2 CE, CC0, downloads per instrument)")
    for name, (_, _, _, octv, desc) in orchestra.PITCHED.items():
        print(f"  {name:15} oct {octv}  {desc}")
    print("  percussion      kit: " + " ".join(orchestra.KIT) + "  (voice_gain: dB)")
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
    pg = sub.add_parser("plugins", help="plugin synths (Surge XT, Vital, Serum): list, install, presets, params")
    pg.add_argument("action", nargs="?", choices=["list", "install", "presets", "params"])
    pg.add_argument("name", nargs="?", help="surge (default), vital, serum, or a .vst3 path")
    pg.add_argument("query", nargs="?", help="filter presets / parameters by this text")
    pg.set_defaults(fn=cmd_plugins)
    lb = sub.add_parser("library", help="open sound library (VCSL, Wikimedia Commons, Freesound): search, fetch, snippet")
    lb.add_argument("action", choices=["search", "fetch", "snippet"])
    lb.add_argument("query", nargs="?", help="search words, or a reference (vcsl:..., commons:File:..., freesound:123)")
    lb.add_argument("--source", choices=["vcsl", "commons", "freesound"])
    lb.add_argument("--license", choices=["cc0", "open", "by-sa"], default="open")
    lb.add_argument("--limit", type=int, default=30)
    lb.add_argument("--json", action="store_true")
    lb.set_defaults(fn=cmd_library)
    ia = sub.add_parser("import-ableton", help="turn an Ableton Live set (.als) into a Pulsar song")
    ia.add_argument("set")
    ia.add_argument("-o", "--output")
    ia.add_argument("--calibration", help="a JSON of per-track dB corrections to apply when rendering")
    ia.set_defaults(fn=cmd_import_ableton)
    gn = sub.add_parser("generate", help="make a sound effect or texture locally with Stable Audio 3")
    gn.add_argument("prompt", nargs="?")
    gn.add_argument("--model", choices=["sfx", "music", "medium"], default="sfx")
    gn.add_argument("--seconds", type=float, default=4.0)
    gn.add_argument("--seed", type=int, default=0)
    gn.add_argument("-o", "--output")
    gn.add_argument("--play", action="store_true")
    gn.add_argument("--setup", action="store_true", help="install the generator (once) and show how to log in")
    gn.set_defaults(fn=cmd_generate)
    st = sub.add_parser("setup", help="check the installation and download the instruments ahead of time")
    st.add_argument("--all", action="store_true", help="also the grand piano (410 MB) and the whole orchestra (~1 GB)")
    st.add_argument("--minimal", action="store_true", help="only check the tools; download nothing")
    st.set_defaults(fn=cmd_setup)
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
