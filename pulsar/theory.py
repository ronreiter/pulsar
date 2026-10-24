"""Pitch, scale and chord vocabulary.

Everything here is pure data: names in, MIDI numbers out. MIDI 60 is C4.
"""
from __future__ import annotations

import re

PITCH_CLASSES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
ACCIDENTALS = {"": 0, "#": 1, "s": 1, "b": -1, "f": -1}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "ionian": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "aeolian": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor": [0, 2, 3, 5, 7, 9, 11],
    "minor_pentatonic": [0, 3, 5, 7, 10],
    "major_pentatonic": [0, 2, 4, 7, 9],
    "blues": [0, 3, 5, 6, 7, 10],
    "chromatic": list(range(12)),
    "whole_tone": [0, 2, 4, 6, 8, 10],
    "phrygian_dominant": [0, 1, 4, 5, 7, 8, 10],
    "hungarian_minor": [0, 2, 3, 6, 7, 8, 11],
}

# Intervals above the root. Longest names first so "maj7" wins over "m".
CHORDS = {
    "": [0, 4, 7], "maj": [0, 4, 7], "M": [0, 4, 7],
    "m": [0, 3, 7], "min": [0, 3, 7], "-": [0, 3, 7],
    "dim": [0, 3, 6], "o": [0, 3, 6], "aug": [0, 4, 8], "+": [0, 4, 8],
    "sus2": [0, 2, 7], "sus4": [0, 5, 7], "sus": [0, 5, 7], "5": [0, 7],
    "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9],
    "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "M7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10], "min7": [0, 3, 7, 10], "mmaj7": [0, 3, 7, 11],
    "dim7": [0, 3, 6, 9], "o7": [0, 3, 6, 9], "m7b5": [0, 3, 6, 10],
    "7sus4": [0, 5, 7, 10], "add9": [0, 4, 7, 14], "madd9": [0, 3, 7, 14],
    "9": [0, 4, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "m9": [0, 3, 7, 10, 14],
    "11": [0, 4, 7, 10, 14, 17], "m11": [0, 3, 7, 10, 14, 17],
    "13": [0, 4, 7, 10, 14, 21], "maj13": [0, 4, 7, 11, 14, 21],
}

ROMAN = {"i": 0, "ii": 1, "iii": 2, "iv": 3, "v": 4, "vi": 5, "vii": 6}

NOTE_RE = re.compile(r"^([a-gA-G])(#|s|b|f)?(-?\d+)$")
CHORD_RE = re.compile(r"^([A-G])(#|b)?(.*)$")
ROMAN_RE = re.compile(r"^(vii|vi|v|iv|iii|ii|i|VII|VI|V|IV|III|II|I)(7|9)?$")


def note_number(name: str) -> int | None:
    """'c4' -> 60, 'eb3' -> 51, 'F#2' -> 42. None if it isn't a note name."""
    m = NOTE_RE.match(name)
    if not m:
        return None
    pc = PITCH_CLASSES[m.group(1).lower()] + ACCIDENTALS[m.group(2) or ""]
    return 12 * (int(m.group(3)) + 1) + pc


def pitch_class(name: str) -> int | None:
    """'A' -> 9, 'F#' -> 6, 'bb' -> 10."""
    m = re.match(r"^([a-gA-G])(#|s|b)?$", name)
    if not m:
        return None
    return (PITCH_CLASSES[m.group(1).lower()] + ACCIDENTALS[m.group(2) or ""]) % 12


def parse_key(text: str) -> tuple[int, list[int]]:
    """'A minor' / 'f# dorian' / 'C' -> (pitch class, scale intervals)."""
    parts = text.replace(":", " ").split()
    if not parts:
        raise ValueError("empty key")
    pc = pitch_class(parts[0])
    if pc is None:
        raise ValueError(f"unknown key root {parts[0]!r}; use a note name like A, F#, Bb")
    scale = "_".join(parts[1:]).lower() or "major"
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; known: {', '.join(sorted(SCALES))}")
    return pc, SCALES[scale]


def degree_to_midi(degree: int, root_pc: int, scale: list[int], octave: int) -> int:
    """0-based scale degree in a key. Degree 7 in a 7-note scale is the next octave."""
    n = len(scale)
    octs, idx = divmod(degree, n)
    return 12 * (octave + 1) + root_pc + scale[idx] + 12 * octs


def chord_symbol(text: str, octave: int) -> list[int] | None:
    """'Am7' -> [57, 60, 64, 67] at octave 3. None if not a chord symbol."""
    m = CHORD_RE.match(text)
    if not m:
        return None
    quality = m.group(3)
    if quality not in CHORDS:
        return None
    pc = (PITCH_CLASSES[m.group(1).lower()] + ACCIDENTALS[m.group(2) or ""]) % 12
    root = 12 * (octave + 1) + pc
    return [root + i for i in CHORDS[quality]]


def roman_chord(text: str, root_pc: int, scale: list[int], octave: int) -> list[int] | None:
    """Diatonic chord on a scale degree: 'iv' in A minor -> D F A. Case is ignored;
    the key decides the quality. A '7' or '9' suffix stacks more thirds."""
    m = ROMAN_RE.match(text)
    if not m:
        return None
    deg = ROMAN[m.group(1).lower()]
    size = {None: 3, "7": 4, "9": 5}[m.group(2)]
    return [degree_to_midi(deg + 2 * k, root_pc, scale, octave) for k in range(size)]


def midi_to_hz(n: float) -> float:
    return 440.0 * 2.0 ** ((n - 69) / 12.0)


def midi_name(n: int) -> str:
    names = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]
    return f"{names[n % 12]}{n // 12 - 1}"
