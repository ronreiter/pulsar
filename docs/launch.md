# Launch kit

Drafts for announcing Pulsar. Edit freely before posting.

## Show HN

**Title:** Show HN: Pulsar – a music language and synth engine built for AI to write electronic music

I wanted to see whether a language model could produce electronic music that actually sounds
good, not just MIDI. Pulsar is three things:

- A small language: TidalCycles-style mini-notation for rhythm inside a conventional syntax
  (tracks, sections that extend each other, play/automate/arrange, functions and loops), with
  chords, scale degrees and roman numerals that resolve in the song's key.
- A rendering engine on Faust + DawDreamer: per-note rendering at 96 kHz, analog-modelled
  filters (Moog ladder, 303 diode, MS-20, SEM), FM and Karplus-Strong, a sampler, and a
  real mix bus (sidechain, Zita reverb, glue, oversampled clipper, true-peak limiter).
- A mix report that replaces ears: loudness, tonal balance per band, the energy arc of each
  section, per-track levels and masking, with concrete suggestions — so the model can iterate.

The demo track on the site was written by a fresh agent given only the reference and the
CLI. There's also a native macOS app with a code editor next to an Ableton-style arrangement,
and an MCP server so Claude or other agents can compose and render in a loop.

GPL-3.0. Feedback very welcome, especially songs your models write.

## X / Bluesky thread

1/ I built Pulsar: a music language + synth engine designed so AI can write electronic music
that sounds produced, not MIDI-ish. A fresh agent wrote this deep house track from the docs
alone. 🎧 https://ronreiter.github.io/pulsar/

2/ The trick: models can't listen, so every render returns a mix report — LUFS, tonal balance,
the energy arc per section, masking between tracks — and the model fixes its own mix.

3/ Under the hood: Faust DSP at 96 kHz, Moog/303/MS-20/SEM filter models, sidechain, Zita
reverb, true-peak mastering. 34 presets calibrated to sit right by default.

4/ Plus Pulsar Studio for macOS: code on the left, an Ableton-style arrangement on the right
that animates as it plays. And an MCP server for agents. Open source (GPL-3.0):
https://github.com/ronreiter/pulsar

## Reddit (r/edmproduction, r/livecoding, r/LocalLLaMA)

**Title:** I made an open-source music language that lets LLMs write, render and mix electronic tracks

Body: the Show HN text above, plus a sentence on what you'd like feedback on (preset
quality, the language, the mix report).
