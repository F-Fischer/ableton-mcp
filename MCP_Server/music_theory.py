"""Pure-Python music theory helpers shared by the composition tools.

No dependency on Live or the Remote Script — everything here is testable
without Ableton open.
"""

from typing import List, Optional

SCALES = {
    "major":             [0, 2, 4, 5, 7, 9, 11],
    "minor":             [0, 2, 3, 5, 7, 8, 10],  # natural minor / aeolian
    "ionian":            [0, 2, 4, 5, 7, 9, 11],
    "dorian":            [0, 2, 3, 5, 7, 9, 10],
    "phrygian":          [0, 1, 3, 5, 7, 8, 10],
    "lydian":            [0, 2, 4, 6, 7, 9, 11],
    "mixolydian":        [0, 2, 4, 5, 7, 9, 10],
    "aeolian":           [0, 2, 3, 5, 7, 8, 10],
    "locrian":           [0, 1, 3, 5, 6, 8, 10],
    "harmonic_minor":    [0, 2, 3, 5, 7, 8, 11],
    "phrygian_dominant": [0, 1, 4, 5, 7, 8, 10],  # "español" / flamenco
}

NOTE_NAMES = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5,
    "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11,
}


def tonic_pitch_class(tonic: str) -> int:
    if tonic not in NOTE_NAMES:
        raise ValueError(f"Unknown tonic '{tonic}'. Expected one of {sorted(NOTE_NAMES)}")
    return NOTE_NAMES[tonic]


def scale_pitch_classes(tonic: str, mode: str) -> set:
    """Pitch classes (0..11) belonging to tonic/mode."""
    if mode not in SCALES:
        raise ValueError(f"Unknown mode '{mode}'. Expected one of {sorted(SCALES)}")
    root = tonic_pitch_class(tonic)
    return {(root + interval) % 12 for interval in SCALES[mode]}


def is_in_scale(pitch: int, tonic: str, mode: str) -> bool:
    return pitch % 12 in scale_pitch_classes(tonic, mode)


def snap_to_scale(pitch: int, tonic: str, mode: str, direction: str = "nearest") -> int:
    """Move a MIDI pitch to the nearest in-scale pitch.

    direction: "nearest" (default), "up", or "down".
    """
    if is_in_scale(pitch, tonic, mode):
        return pitch

    pitch_classes = scale_pitch_classes(tonic, mode)

    for distance in range(1, 12):
        candidates = []
        if direction in ("nearest", "down"):
            candidates.append(pitch - distance)
        if direction in ("nearest", "up"):
            candidates.append(pitch + distance)
        for candidate in candidates:
            if candidate % 12 in pitch_classes:
                return candidate

    return pitch


def pitch_in_octave(pitch_class: int, octave: int) -> int:
    """Place a pitch class (0..11) in a given Ableton octave (C{octave} == MIDI (octave+2)*12)."""
    return (octave + 2) * 12 + (pitch_class % 12)


def degree_to_pitch(tonic: str, mode: str, degree: int, octave: int) -> int:
    """Diatonic scale degree (1-indexed, wraps past 7) to a MIDI pitch in the given octave.

    octave follows Ableton's convention: C{octave} == MIDI pitch (octave + 2) * 12,
    i.e. octave=3 -> C3 == 60.
    """
    if mode not in SCALES:
        raise ValueError(f"Unknown mode '{mode}'. Expected one of {sorted(SCALES)}")
    intervals = SCALES[mode]
    root = tonic_pitch_class(tonic)

    degree_index = degree - 1
    octave_offset, scale_index = divmod(degree_index, len(intervals))

    base = (octave + 2) * 12 + root
    return base + intervals[scale_index] + 12 * octave_offset


def diatonic_chord(tonic: str, mode: str, degree: int, octave: int, size: int = 4) -> List[int]:
    """Stack diatonic thirds on top of `degree` to build a chord (triad/7th/9th/...).

    size=3 -> triad, size=4 -> seventh chord, size=5 -> ninth, etc.
    """
    intervals = SCALES[mode]
    step = len(intervals) // 2 if len(intervals) != 7 else 2  # diatonic third = 2 scale steps
    return [
        degree_to_pitch(tonic, mode, degree + i * step, octave)
        for i in range(size)
    ]


def voice_chord(
    pitches: List[int],
    register_low: int = 48,
    register_high: int = 72,
    voicing: str = "close",
) -> List[int]:
    """Re-voice a stack of pitches within a register, per voicing style.

    - "close": keep pitches as given, transposed by octaves into [register_low, register_high].
    - "open": spread by raising every other note an octave.
    - "drop2": drop the second-highest note down an octave (classic drop-2 voicing).
    """
    if not pitches:
        return []

    voiced = sorted(pitches)

    # Transpose the whole stack by octaves until the lowest note sits in range.
    while voiced[0] < register_low:
        voiced = [p + 12 for p in voiced]
    while voiced[0] > register_high and len(voiced) > 1:
        voiced = [p - 12 for p in voiced]

    if voicing == "open" and len(voiced) > 2:
        voiced = [p + 12 if i % 2 == 1 else p for i, p in enumerate(voiced)]
    elif voicing == "drop2" and len(voiced) >= 2:
        idx = len(voiced) - 2
        voiced[idx] -= 12

    return sorted(voiced)


def eq8_freq_to_value(hz: float) -> float:
    """Map a frequency in Hz to EQ Eight's normalized 0..1 parameter value (10 Hz - 22 kHz)."""
    import math
    return math.log10(hz / 10.0) / math.log10(2200.0)


def eq8_value_to_freq(value: float) -> float:
    """Inverse of eq8_freq_to_value."""
    return 10.0 * (2200.0 ** value)


GENRES = {
    "dnb_liquid": {
        "tempo": 174, "sig": (4, 4),
        "drums": {"kick": [0], "snare": [2], "hat": [0.5, 1.5, 2.5, 3.5]},
        "bass_style": "rolling_sub", "default_mode": "minor",
        "chord_instrument": "query:Synths#Electric",
        "bass_preset": "reese_sub", "pad_preset": "warm_pad",
    },
    "dnb_neuro": {
        "tempo": 174, "sig": (4, 4),
        "drums": {"kick": [0], "snare": [2], "hat": [0.5, 1.5, 2.5, 3.5]},
        "bass_style": "reese_growl", "default_mode": "minor",
        "bass_preset": "reese_growl",
    },
    "house": {
        "tempo": 126, "sig": (4, 4),
        "drums": {"kick": [0, 1, 2, 3], "clap": [1, 3], "hat": [0.5, 1.5, 2.5, 3.5]},
        "bass_style": "offbeat", "default_mode": "minor",
    },
    "trap": {
        "tempo": 140, "sig": (4, 4), "halftime": True,
        "drums": {"kick": [0, 1.5], "snare": [2], "hat": "rolls"},
        "bass_style": "808_glide", "default_mode": "minor",
    },
}

DEFAULT_STRUCTURE = [
    {"name": "intro", "bars": 8},
    {"name": "build", "bars": 8},
    {"name": "drop", "bars": 16},
    {"name": "break", "bars": 8},
]

TRACK_COLORS = {
    "drums": 0xCC3333,
    "bass": 0xCC8800,
    "harmony": 0x3366CC,
    "pad": 0x9933CC,
    "fx": 0x33CC99,
}

DEFAULT_PROGRESSIONS = {
    "major":             [1, 5, 6, 4],
    "minor":             [1, 6, 3, 7],
    "dorian":            [1, 4, 1, 5],
    "mixolydian":        [1, 4, 5, 1],
    "phrygian_dominant": [1, 2, 1, 7],
    "harmonic_minor":    [1, 6, 7, 1],
}
DEFAULT_PROGRESSION_FALLBACK = [1, 4, 5, 1]

DRUM_PITCHES = {
    "kick": 36,
    "snare": 38,
    "clap": 39,
    "chh": 42,
    "closed_hat": 42,
    "hat": 42,
    "ohh": 46,
    "open_hat": 46,
}

SOUND_PRESETS = {
    "reese_sub": {
        "device": "Wavetable",
        "params": {
            "Sub On": 1, "Sub Gain": 0.62, "Sub Transpose": 1,
            "Flt 1 Freq": 0.5, "Flt 1 Res": 0.30,
            "Osc 1 Detune": 0.52, "Osc 2 Detune": 0.60, "Glide": 0.12,
        },
    },
    "warm_pad": {
        "device": "Analog",
        "params": {
            "OSC1 On/Off": 1, "OSC1 Shape": 1, "OSC1 Detune": 0.42,
            "OSC2 On/Off": 1, "OSC2 Shape": 1, "OSC2 Detune": 0.60,
            "F1 Freq": 0.55, "F1 Resonance": 0.10, "F1 Freq < Env": 0.35,
            "F1 Drive": 2, "AEG1 Attack": 0.22, "AEG1 Decay": 0.6, "AEG1 Sustain": 0.80,
        },
    },
}


def validate_tonic_mode(tonic: str, mode: str) -> None:
    """Raise ValueError with a helpful message if tonic/mode aren't recognized."""
    if tonic not in NOTE_NAMES:
        raise ValueError(f"Unknown tonic '{tonic}'. Expected one of {sorted(NOTE_NAMES)}")
    if mode not in SCALES:
        raise ValueError(f"Unknown mode '{mode}'. Expected one of {sorted(SCALES)}")
