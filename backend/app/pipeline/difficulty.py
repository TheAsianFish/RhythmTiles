"""Difficulty shaping. Post-processing pass that thins or thickens notes.

Targets (notes per beat):
  easy:   ~1.0
  normal: ~1.5
  hard:   ~2.5 (cap)

Approach: greedy spacing-aware decimation. We pick the minimum gap between
kept notes from the target density, walk notes in order, and keep one if the
gap is satisfied. A note whose lane differs from the previous kept note can
slot in at a tighter gap so we preserve lane variety (the previous stride
implementation collapsed alternating lanes into a single-lane chain on stride
2, which is the worst-case pattern for a 4-lane game).
"""

from __future__ import annotations

import math

from app.pipeline.lane_assign import RawNote

_DENSITY_TARGETS = {
    "easy": 1.0,
    "normal": 1.5,
    "hard": 2.5,
}

# How close to min_gap a different-lane note can sneak in. 0.6 keeps the
# combined density close to target while breaking up same-lane chains.
_LANE_VARIETY_FACTOR = 0.6


def shape_difficulty(
    *,
    notes: list[RawNote],
    difficulty: str,
    beats: list[float],
) -> list[RawNote]:
    if not notes:
        return notes
    target = _DENSITY_TARGETS.get(difficulty, 1.5)
    if not beats or len(beats) < 2:
        return notes

    avg_beat_period = (beats[-1] - beats[0]) / max(len(beats) - 1, 1)
    if avg_beat_period <= 0:
        return notes

    duration = notes[-1].t - notes[0].t
    beat_count_in_span = max(duration / avg_beat_period, 1.0)
    current_density = len(notes) / beat_count_in_span

    if current_density <= target * 1.05:
        return notes

    min_gap = avg_beat_period / target  # seconds between hits at target density
    tight_gap = min_gap * _LANE_VARIETY_FACTOR

    kept: list[RawNote] = []
    last_t = -1e9
    last_lane = -1
    for n in notes:
        gap = n.t - last_t
        if gap >= min_gap:
            kept.append(n)
            last_t = n.t
            last_lane = n.lane
        elif n.lane != last_lane and gap >= tight_gap:
            kept.append(n)
            last_t = n.t
            last_lane = n.lane
    return kept


def target_density(difficulty: str) -> float:
    return _DENSITY_TARGETS.get(difficulty, 1.5)


def approximate_density(notes: list[RawNote], beats: list[float]) -> float:
    if len(notes) < 2 or len(beats) < 2:
        return 0.0
    avg_beat_period = (beats[-1] - beats[0]) / max(len(beats) - 1, 1)
    duration = notes[-1].t - notes[0].t
    return len(notes) / max(duration / avg_beat_period, 1.0)


# Re-export math so the linter is happy if the module is later extended.
_ = math
