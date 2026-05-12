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

Per-section density variation: when `energy_buckets` is provided, each note's
local min_gap is scaled by the bucket's density multiplier (0 = low energy,
larger gap; 2 = high energy, smaller gap). The result is denser playable
notes during the chorus and a breathing room during quiet verses.
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

# Density multiplier per energy bucket. Bucket 0 = low energy (verse), 1 =
# medium, 2 = high energy (chorus). 0.75 / 1.0 / 1.30 swings density by
# about a third in each direction, which is enough to feel different
# without making the chart density unpredictable.
_BUCKET_DENSITY_MULT = (0.75, 1.00, 1.30)


def shape_difficulty(
    *,
    notes: list[RawNote],
    difficulty: str,
    beats: list[float],
    energy_buckets: list[int] | None = None,
) -> list[RawNote]:
    """Thin a dense raw-note list down toward the target density.

    `energy_buckets` is an optional list parallel to `notes` (one bucket per
    note, value in {0, 1, 2}). When provided, each note's local min_gap is
    scaled by `_BUCKET_DENSITY_MULT[bucket]` so chorus sections keep more
    notes than verses.
    """
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

    base_min_gap = avg_beat_period / target

    def bucket_for(index: int) -> int:
        if not energy_buckets or index >= len(energy_buckets):
            return 1
        b = energy_buckets[index]
        return b if b in (0, 1, 2) else 1

    kept: list[RawNote] = []
    last_t = -1e9
    last_lane = -1
    for i, n in enumerate(notes):
        gap = n.t - last_t
        # Chord partner: same-t note as the previously kept one. Always keep
        # so the pair survives thinning. Lane assigner only emits chords with
        # distinct lanes so we don't need to re-check that here.
        if last_t > -1e8 and abs(gap) < 1e-6 and n.lane != last_lane:
            kept.append(n)
            continue

        # Per-note thresholds. High-energy chorus notes get tighter gaps
        # (more notes kept); low-energy verse notes get larger gaps.
        density_mult = _BUCKET_DENSITY_MULT[bucket_for(i)]
        local_min_gap = base_min_gap / density_mult
        local_tight_gap = local_min_gap * _LANE_VARIETY_FACTOR

        if gap >= local_min_gap:
            kept.append(n)
            last_t = n.t
            last_lane = n.lane
        elif n.lane != last_lane and gap >= local_tight_gap:
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
