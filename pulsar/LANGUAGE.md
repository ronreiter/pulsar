# Pulsar language reference

Pulsar is a small language for writing electronic music. A song file (`.pulse`)
declares **tracks** (instruments with effects), **sections** (bars of music where
tracks **play** patterns and parameters are **automate**d) and an **arrangement**
of sections. `pulsar render song.pulse` turns it into a mastered 48 kHz WAV (or MP3 with `-o song.mp3`) and
prints a mix report you can use to improve the song without listening.

```
tempo 124
key "A minor"

track kick = drums("909")
track bass = synth("acid", duck: 0.5) >> delay(3/16, feedback: 0.3, mix: 0.15)
track pad  = synth("pad", reverb: 0.4, duck: 0.6)

section intro 8 {
  play kick "bd*4"
  play pad  "<Am7 Fmaj7 C G>"
}
section drop 16 extends intro {
  play kick "~ cp ~ cp"
  play bass "a1 a1 [~ a2] a1".slide("~ ~ x ~")
  automate bass.cutoff 0.2 -> 0.6
}
arrange intro, drop, drop
```

## Time

* One **cycle = one bar** (4 beats). Everything time-like is in bars:
  `1/4` = a beat, `1/8` = an eighth note, `1/16` = a sixteenth, `3/16` = a dotted eighth.
* Bar positions inside a section start at **0**: `from 4` means "after 4 bars"; `to 8` stops before bar 8.
* A step lasts as long as its slot. To hold one note for several bars, slow it down:
  `"a3/4"` or `"a3".slow(4)` is one 4-bar note (use this for risers, sweeps and drones).
  `play rise "a3" from 4` would instead retrigger a 1-bar note every bar.

## Sections and inheritance

`section drop2 16 extends drop { ... }` begins as a copy of `drop`. Inside it:

* the first `play` of a track **replaces** what was inherited for that track
  (later plays of the same track in the same section add more layers);
* `mute bass` removes the inherited plays and automation of `bass`;
* tracks you don't mention keep playing exactly as in `drop`.

## Statements

| statement | meaning |
|---|---|
| `tempo 128` | BPM (top level only; default 120) |
| `key "F# minor"` | key for scale degrees and roman numerals (default `"A minor"`); also allowed inside a section |
| `swing 0.1` | delay every off-beat 16th on every track, drums included (0 = straight, 0.3 = heavy shuffle); per section too; a track's `swing:` overrides it |
| `seed 7` | change every random choice in the song (deterministic) |
| `loudness -10` | master target, integrated LUFS (default -10; -14 for streaming-safe) |
| `ceiling -1` | master true-peak ceiling in dBTP (default -1) |
| `track name = instrument >> effect >> effect` | declare a track |
| `name.gain = -3` | change a track setting after declaring it |
| `bus reverb = reverb(decay: 4)` | configure (or add) a send bus |
| `master = eq(high: 1) >> comp(ratio: 2)` | extra master effects, before the built-in glue + limiter |
| `section name bars { ... }` | a block of bars; `section b 16 extends a { ... }` starts with a copy of a's plays and automation |
| `play track pattern [from X] [to Y] [at X]` | loop a pattern on a track (in a section). `to` is exclusive; `at X` plays bar X only |
| `mute track, track` | (in a section) drop the inherited plays and automation of these tracks |
| `automate track.param value [from X] [to Y]` | change a parameter over the section |
| `arrange a, b, b * 2, c` | the song order. Without it, sections play in the order written |
| `let x = 1`, `x = 2` | variables |
| `fn name(a, b = 2) { ...; return v }` / `fn name(a) = expr` | functions |
| `fn(p) => p.fast(2)` | anonymous function (for `.every()` and friends) |
| `for i in 0..4 { }`, `for x in [..] { }`, `repeat 4 { }` (`i` is the counter) | loops |
| `if c { } elif d { } else { }`, `a if c else b` | conditionals |
| `print(x)` | print while compiling |

Comments are `//` or `#`. Newlines end statements, except inside `(...)`/`[...]`
or when the next line starts with `>>` or `.` (so chains can wrap):

```
track lead = synth("lead")
  >> delay(1/8, feedback: 0.4)
  >> reverb(mix: 0.2)
let hook = "0 2 4 7"
  .fast(2)
  .vel("1 0.7")
```

Strings can be `"..."`, `'...'` or `` `...` `` (backticks may span lines).

## Patterns (mini-notation)

A string is a pattern. Steps divide the bar equally.

| syntax | meaning | example |
|---|---|---|
| `a b c` | sequence (each takes 1/3 of the bar) | `"bd sd bd sd"` |
| `~` | rest | `"bd ~ ~ bd"` |
| `[a b]` | group into one step | `"bd [sd sd]"` |
| `a*n` | repeat n times in its step | `"hh*8"` |
| `a/n` | slow down over n bars | `"Am7/2"` |
| `<a b c>` | one per bar, in turn | `"<Am F C G>"` |
| `a, b` | play at the same time | `"bd*4, [~ hh]*4"` |
| `a _ _` | hold the previous step longer | `"a2 _ _ c3"` |
| `a@3` | weight a step (takes 3 slots) | `"a2@3 c3"` |
| `a!3` or `a ! !` | repeat as separate steps | `"bd!3 sd"` |
| `a?` / `a?0.3` | random drop (50% / 30%) | `"hh*16?"` |
| `a(3,8)` / `a(3,8,2)` | euclidean rhythm (hits, steps, rotate) | `"bd(3,8)"` |
| `[a\|b\|c]` | random choice each bar | `"[bd\|cp]*4"` |

**What a step can be** (on melodic tracks):

* note names with octave, **lowercase**: `a3 c#4 eb2 f#1` (MIDI 60 = `c4`); without octave (`a c e`) the track's octave is used.
  A capitalised name is a chord: `E6` is the E-sixth chord, `e6` is the note.
* scale degrees, 0-based, in the song's key and the track's octave: `0 2 4 7 -1`
* chord symbols (capitalised root): `Am Am7 C Cmaj7 F#m7b5 Bb9 Dsus4 Eadd9 G7`
* roman numerals, diatonic to the key (case is ignored, quality comes from the key; `7`/`9` adds notes): `i iv v VI VII7`

Chord qualities: (none) `maj` `M` `m` `min` `-` `dim` `o` `aug` `+` `sus2` `sus4` `sus` `5`
`6` `m6` `7` `maj7` `M7` `m7` `min7` `mmaj7` `dim7` `o7` `m7b5` `7sus4` `add9` `madd9`
`9` `maj9` `m9` `11` `m11` `13` `maj13`. Chords are built upward from the root in the
track's octave; use `.voicing()` / `.invert()` to spread or voice-lead them.

On drum tracks steps are voice names: `bd sd cp hh oh rd cr lt mt ht rim cb sh pc`
(aliases: kick snare clap hat openhat ride crash tom cowbell shaker).

A drum track can play several voices at once with `,`:
`play kit "bd*4, ~ cp ~ cp, [~ hh]*4, ~ ~ ~ oh"`.

## Pattern methods

Any string or pattern takes these (chain them):

| method | effect |
|---|---|
| `.fast(n)` `.slow(n)` | speed up / slow down |
| `.rev()` `.palindrome()` `.iter(n)` | reverse each bar / alternate forward-back / rotate start each bar |
| `.late(t)` `.early(t)` | shift in time (bars) |
| `.ply(n)` | repeat every event n times |
| `.every(n, f)` `.lastof(n, f)` | apply function f on bar 0, n, 2n... / on the last bar of every n |
| `.sometimes(f)` `.often(f)` `.rarely(f)` | apply f to random bars (50% / 75% / 25%) |
| `.degrade(p)` | randomly drop events (probability p) |
| `.off(t, f)` | layer a shifted, transformed copy: `.off(1/8, fn(p) => p.transpose(12))` |
| `.echo(n, time, feedback)` | repeated quieter copies |
| `.struct("x ~ x x")` | re-rhythm: sample this pattern at each `x` |
| `.mask("1 1 0 1")` | silence events where the mask is 0 |
| `.stack(p, ...)` / `.then(p, ...)` | play together / one after another (one bar each) |
| `.transpose(semitones)` `.oct(n)` | shift pitch chromatically / by octaves |
| `.up(steps)` | shift pitch by scale degrees (stays in key) |
| `.octave(n)` `.key("C minor")` | override the track octave / key for this pattern |
| `.arp(mode, rate, octaves)` | arpeggiate chords. mode: up down updown downup converge random; rate e.g. `1/16` |
| `.voicing(mode)` | chord voicing: `smooth` (each chord picks the inversion nearest the middle of the track's octave, so progressions voice-lead), `open`, `drop2`, `spread` (root an octave down), `close` |
| `.invert(n)` | rotate chord notes: 1 = first inversion, -1 = top note down |
| `.vel(x)` | velocity 0-1: number or pattern `"1 0.6 0.8 0.6"` (default 0.8) |
| `.legato(x)` | note length as a fraction of its step (0.5 = staccato) |
| `.slide("~ x ~ ~")` | mono synths: glide into marked notes without retriggering (303 slides) |
| `.accent("x ~ ~ x")` | accented notes (louder, brighter filter envelope) |
| `.swing(amount)` `.humanize(ms: 6, vel: 0.08)` | groove |

Functions: `seq(a, b, ...)` (sequence in one bar), `cat(a, b, ...)` (one per bar),
`stack(a, b, ...)`, `euclid(hits, steps, rotate, "bd")`, `silence()`.

## Instruments

* `synth("preset", param: value, ...)` — a tuned preset (see the list below)
* `drums("kit", bd_decay: 0.6, hh_tune: 1.1, sd_gain: -2)` — drum machine; `voice_param` tweaks one voice, `voice_gain` sets its level in dB
* `analog(...)`, `fm(...)`, `pluck(...)` — the raw engines with default settings

Any instrument also takes **mixer settings**:

| key | meaning |
|---|---|
| `gain` | fader in dB **relative to the preset's balanced default** (0 = as designed, -3 = a bit quieter); `track.gain = x` sets it too. `automate track.gain` is a volume envelope **on top of** this fader: 0 = the track's normal level, -12 = 12 dB under it |
| `pan` | -1 left .. 1 right |
| `width` | stereo width (0 = mono; use 0 on basses) |
| `reverb`, `delay`, (or any bus name) | send amount 0-1 |
| `duck` | sidechain amount 0-1: ducks under the kick (`bd` of any drum track) |
| `duck_by` | duck under another track instead: `duck_by: "kick2"` |
| `duck_release` | seconds for the duck to recover (default: just under a beat) |
| `octave` | octave for degrees and chords (presets set a sensible one) |
| `swing`, `humanize` (ms), `legato` | per-track groove |

Shape settings for `analog` presets/engine: `wave1`, `wave2` (`saw square pulse tri sine none`),
`unison` (1-9 detuned voices), `filter` (`ladder` Moog, `diode` TB-303, `korg` MS-20,
`oberheim` SEM, `svf`, `hp`, `bp`, `none`), `mono` (true/false).

Tempo-synced LFO rates: `flfo_sync: 1/8` (filter wobble), `vib_sync`, `trem_sync`.

## Samples

`sampler("path.wav", ...)` plays an audio file (WAV, AIFF, FLAC, OGG; paths are relative
to the song file). Use it for vocals, one-shots, loops and chops.

```
track vox  = sampler("samples/vox_aah_a4.wav", root: "a4", reverb: 0.4, duck: 0.3)
track chop = sampler("samples/vox_ooh_a4.wav", root: "a4", slices: 8, gate: true)
section a 8 {
  play vox  "<a4 c5 e5 g4>"            // note names re-pitch the sample from its root
  play chop "0 ~ 3 ~ 5 2 ~ 7"          // integers pick one of the slices
}
```

| setting | meaning |
|---|---|
| `root` | the note the sample was recorded at (default `c4`); note steps re-pitch from it (pitch and speed change together, like a hardware sampler) |
| `start`, `end` | region of the file to use, as fractions 0-1 |
| `slices` | cut the region into N equal slices; integer steps pick a slice |
| `gate` | `true`: stop at the end of each step (with `release` seconds of fade); default plays the whole sample |
| `fit` | `true`: stretch each hit to exactly fill its step (changes pitch) |
| `speed`, `transpose`, `reverse` | playback rate, semitone offset, backwards |
| `attack`, `release` | fade in / fade out times in seconds |

Any other word as a step (`"x ~ x ~"`) plays the sample as recorded. Mixer settings
(`gain`, `pan`, `duck`, sends...) work as for synths. Samples are level-calibrated like
the presets; `gain` is relative to that. Only use audio you have the rights to;
`python tools/make_vox.py` writes original synthesized sung vowels (`samples/vox_aah_a4.wav`, `samples/vox_ooh_a4.wav`) to try it with.

## Effects

Chain after an instrument with `>>`. The first positional argument is shown in bold.

| effect | parameters |
|---|---|
| `reverb` | **mix**, size, decay (s), damp, predelay (s), lowcut (Hz), width |
| `delay` | **time** (note value, e.g. `3/16`; or `ms:`), feedback, mix, tone, pingpong (0/1), spread |
| `chorus` | **mix**, rate (Hz), depth |
| `drive` (`dist`) | **amount**, tone, mix, out |
| `crush` (`lofi`) | **bits**, rate (sample-rate fraction), mix |
| `filter` | **cutoff** (0-1), resonance, mode (0 lp, 1 hp, 2 bp) — automate cutoff for sweeps |
| `lp`, `hp` | **freq** (Hz), q |
| `eq` | **low** (dB), low_freq, mid, mid_freq, mid_q, high, high_freq |
| `comp` | **threshold** (dB), ratio, attack, release, makeup, knee, mix |
| `width` | **amount** (0 mono .. 2 extra wide), bass_mono (Hz) |
| `phaser` | **rate**, depth, feedback |
| `tremolo` | **rate** (Hz) or `sync:` note value, depth, stereo |
| `tone` | **bass**, sub, low_mid, mid, presence, high, air (dB): one broad band per report band |
| `gain` | **db** |
| `pan` | **pos** |

Default buses: `reverb` (a large Zita hall) and `delay` (dotted-eighth ping-pong).
Reconfigure with `bus reverb = reverb(decay: 5, size: 0.9)`.

## Automation

```
automate pad.cutoff 0.2 -> 0.7              // ramp over the whole section
automate pad.cutoff ramp(0.2, 0.7, "exp")   // curves: lin exp smooth in out
automate bass.cutoff lfo(period: 1/2, min: 0.2, max: 0.6)   // shapes: sine tri saw ramp_down square random
automate lead.gain "-12 -6 0 0"             // a value pattern: one value per step, per bar
automate lead.delay.feedback 0.2 -> 0.7 from 12 to 16
automate pad.reverb 0.2 -> 0.8              // send amounts
automate master.glue 0.3 -> 0.6             // master: glue, low, high, air (dB), gain (dB)
```

Targets:

* a synth parameter: `pad.cutoff` (synth parameters win when an effect has the same name);
* `gain`, `pan`, or a send (`reverb`, `delay`, other buses);
* an effect parameter: `track.effect.param` where `effect` is the effect's name
  (`bass.filter.cutoff`, `kick.hp.freq`); the second effect of the same kind is `hp_2`,
  the third `hp_3`. `track.param` also works when exactly one effect has that parameter.

Automation persists like a DAW lane: after a section ends, the parameter keeps its last
automated value in later sections until another `automate` changes it (even in sections
that don't extend the first). To restore a value, automate it back, e.g. `automate pad.gain 0`.

## Writing good songs (what the mix report checks)

* Start every track from a preset; tweak a few parameters rather than designing from scratch.
* Keep basses mono and below `c4`; keep pads/chords above `e2`; leads around octave 4-5.
* Use `duck:` (0.4-0.7) on bass and pads so the kick punches through.
* Arrangement creates energy: intros and breakdowns should drop the kick or bass;
  the report's **energy arc** shows loudness per section — drops should be 2-4 LU louder.
* Build tension with `riser`/`noise_sweep` tracks over the last 4-8 bars before a drop,
  filter ramps (`automate pad.cutoff 0.2 -> 0.8`), and `impact` on the drop's first bar.
* Vary loops: `.lastof(4, fn(p) => ...)` for fills, `<...>` for alternation, `.degrade()` and `?` for life.
* If the report says the limiter works hard (> 6 dB), lower the loudest/peakiest track.
* Shape a whole section's level with `automate master.gain -3` (it persists; set it back to 0 later).
* Iterate fast: `pulsar render song.pulse --only drop` renders just that section;
  `pulsar check` compiles without rendering and prints each track's note range.

Match a reference's sound (not its music): `pulsar compare mine.wav reference.wav` shows
tempo, key, loudness, dynamics, width, low-end punch and tonal balance side by side with
suggestions; `pulsar match song.pulse reference.wav` renders repeatedly, tuning a 7-band
`tone()` master EQ until the balance matches, and writes `song.matched.pulse` plus audio.

Report bands: sub 20-60 Hz, bass 60-250, low_mid 250-800, mid 800-2.5k,
presence 2.5-6k, high 6-12k, air 12-20k (a note's fundamental sits low, but bright
sounds put most energy in their harmonics, so an e-piano can read as low_mid/mid).
