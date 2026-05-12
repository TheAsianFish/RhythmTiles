"""Beat-grid safety net.

Onset detection occasionally misses long stretches on vocal-only choruses
where neither drums nor melody attacks fire confidently. The result is a
dead patch in the chart that maps to musical silence the player can hear
isn't actually silent. This module inserts synthetic onsets aligned to
the beat grid for any run of beats where no real onset landed nearby.

Heuristic:
  - For each beat in `beats`, mark it "empty" if no real onset is within
    `BEAT_TOLERANCE_S` of it.
  - Find runs of consecutive empty beats. Runs shorter than
    `MIN_EMPTY_RUN_BEATS` are left alone (intentional musical pauses).
  - For each beat in a run that qualifies, insert a synthetic Onset with
    medium-low strength (below the chord-quantile threshold) and an
    alternating low/high centroid so lane assignment distributes across
    all four lanes.
"""

from __future__ import annotations

from app.pipeline.onset_detect import Onset

# Real onset within this many seconds of a beat is "close enough" to count
# the beat as covered. 100ms is roughly an eighth note at 150 BPM.
BEAT_TOLERANCE_S = 0.10

# Don't fill short empty runs. Musical breaks are real and shouldn't be
# papered over with synthetic notes.
MIN_EMPTY_RUN_BEATS = 2

# Synthetic onsets get strength below the chord-quantile threshold so they
# never become chords or anchors.
SYNTH_STRENGTH = 0.40

# Alternating centroids so synthetic onsets distribute between low and high
# bands (which means across all 4 lanes via the lane assigner).
SYNTH_CENTROIDS = (500.0, 3000.0)


def fill_empty_beats(onsets: list[Onset], beats: list[float]) -> list[Onset]:
    """Return `onsets` augmented with synthetic events at long empty runs."""
    if not beats or len(beats) < 2:
        return onsets
    onset_times = sorted(o.t for o in onsets)
    has_onset = _per_beat_coverage(beats=beats, onset_times=onset_times)

    augmented = list(onsets)
    i = 0
    while i < len(has_onset):
        if has_onset[i]:
            i += 1
            continue
        # Found the start of an empty run.
        start = i
        while i < len(has_onset) and not has_onset[i]:
            i += 1
        run_len = i - start
        if run_len < MIN_EMPTY_RUN_BEATS:
            continue
        for k in range(start, i):
            augmented.append(
                Onset(
                    t=float(beats[k]),
                    strength=SYNTH_STRENGTH,
                    centroid_hz=SYNTH_CENTROIDS[k % 2],
                ),
            )

    augmented.sort(key=lambda o: o.t)
    return augmented


def _per_beat_coverage(*, beats: list[float], onset_times: list[float]) -> list[bool]:
    """For each beat, True iff some onset lands within BEAT_TOLERANCE_S."""
    has_onset: list[bool] = []
    j = 0
    for b in beats:
        # Advance j past onsets that are too far before this beat.
        while j < len(onset_times) and onset_times[j] < b - BEAT_TOLERANCE_S:
            j += 1
        within = j < len(onset_times) and onset_times[j] <= b + BEAT_TOLERANCE_S
        has_onset.append(within)
    return has_onset
