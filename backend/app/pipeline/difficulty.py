"""Difficulty shaping. Post-processing pass that thins or thickens notes.

Targets (notes per beat):
  easy:   ~1.0
  normal: ~1.5
  hard:   ~2.5 (cap)

Approach: if density is over target, drop the lowest-strength-equivalent notes
(here we proxy strength with isolation, since onset strength is dropped at
lane_assign time). If under target, leave it alone; we don't fabricate hits.
"""

from __future__ import annotations

import math

from app.pipeline.lane_assign import RawNote

_DENSITY_TARGETS = {
    "easy": 1.0,
    "normal": 1.5,
    "hard": 2.5,
}


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
        return notes  # Without a beat reference we can't shape sensibly.

    avg_beat_period = (beats[-1] - beats[0]) / max(len(beats) - 1, 1)
    if avg_beat_period <= 0:
        return notes

    duration = notes[-1].t - notes[0].t
    beat_count_in_span = max(duration / avg_beat_period, 1.0)
    current_density = len(notes) / beat_count_in_span

    if current_density <= target * 1.05:
        return notes

    keep_ratio = target / current_density
    keep_every = max(1, int(round(1 / max(keep_ratio, 1e-3))))
    # Stride-keep: keep every Nth note. Cheap, deterministic, preserves structure.
    return [n for i, n in enumerate(notes) if i % keep_every == 0]


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
