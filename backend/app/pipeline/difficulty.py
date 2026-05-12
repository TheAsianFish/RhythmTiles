"""Difficulty shaping.

Difficulty is a SELECTIVITY knob, not a fixed density target. Each note
gets a "musical importance" score from its onset strength, the local
energy bucket, and whether it's on a beat or off-beat. Each difficulty
keeps the top X% of notes by score. The exact note count therefore
depends on the song's actual onset density: a sparse ballad produces
fewer notes than a dense EDM track at the same difficulty.

After selectivity, a sanity cap enforces a maximum sustained density
(8 notes/sec over a 1-second window) so unplayable bursts can't happen
no matter what the upstream pipeline produced. Bursts trim the lowest-
importance notes first.

The `_DENSITY_TARGETS` map is preserved for the public `target_density`
helper that other modules still use (it's a sanity hint, not a thinning
rule any more).
"""

from __future__ import annotations

from app.pipeline.lane_assign import RawNote

_DENSITY_TARGETS = {
    "easy": 1.0,
    "normal": 1.5,
    "hard": 2.5,
    "expert": 3.8,
}

# Target notes-per-second BAND per difficulty. The keep ratio is derived
# per song: ratio = (target_mid * duration) / total_candidates, clamped to
# [MIN_RATIO, MAX_RATIO]. This makes a difficulty feel similarly hard
# across genres: a sparse ballad gets a higher ratio (keep more of what's
# there), a dense EDM song gets a lower ratio (don't spam the player).
# Fixed ratios trained on one song don't generalize; calibration does.
#
# Bands picked to feel right for typical pop/rock at ~120-180 BPM. Tune
# with playtest data, or replace this dict with an ML-learned function
# of (song features -> target rate) once we have replay ratings.
# Normal and Hard raised so vocal syllables and on-beat instrument hits
# survive selectivity instead of being thinned to a sparse skeleton. Expert
# nudged up slightly so it stays above Hard's new ceiling. Bumped again to
# preserve more single-note coverage of vocal syllables and melodic ramps on
# Normal/Hard; Expert gets a modest lift so the tier ordering still holds.
TARGET_NOTES_PER_SEC = {
    "easy":   (0.7, 1.5),
    "normal": (3.0, 4.5),
    "hard":   (5.0, 6.8),
    "expert": (5.8, 7.6),
}

# Absolute bounds on the derived ratio so degenerate inputs (tiny or huge
# candidate sets) don't push selection past sanity.
_MIN_RATIO = 0.05
_MAX_RATIO = 0.98

# Energy-bucket bonus to keep_score: low energy verse notes get a small
# discount, chorus notes get a small boost. Subtle so it complements
# strength rather than overriding it.
_ENERGY_MULT = (0.90, 1.00, 1.15)

# Sanity cap: no more than this many notes inside any sliding window of
# SANITY_WINDOW_S seconds. Drops the lowest-strength notes in over-dense
# windows. 8 notes/sec is roughly osu!mania "Insane" peak density across
# four lanes (2 notes/sec per hand) and the upper bound of sustained play
# even for skilled players.
SANITY_NOTES_PER_WINDOW = 8
SANITY_WINDOW_S = 1.0


def shape_difficulty(
    *,
    notes: list[RawNote],
    difficulty: str,
    beats: list[float],
    energy_buckets: list[int] | None = None,
    song_duration_s: float | None = None,
) -> list[RawNote]:
    """Filter `notes` by per-note importance and a per-song calibrated keep ratio.

    The returned list is time-sorted and respects chord pairs (if one chord
    partner is kept, both are). After selection the sanity cap runs to clamp
    humanly-impossible bursts.

    The keep ratio is no longer a fixed per-difficulty constant. It's derived
    each call from the song's actual onset density: the difficulty supplies a
    target notes-per-second BAND (TARGET_NOTES_PER_SEC), and we pick the
    ratio that lands the chart in that band given the candidate count and
    song duration. So Easy on a sparse ballad keeps a higher fraction than
    Easy on a dense EDM track, but both end up at ~1 note/sec player rate.

    `song_duration_s` overrides what we'd otherwise infer from `beats`. Pass
    the audio duration when available for the most accurate calibration.
    """
    if not notes:
        return notes

    duration_s = _infer_duration(
        notes=notes, beats=beats, song_duration_s=song_duration_s,
    )
    keep_ratio = _calibrated_ratio(
        difficulty=difficulty,
        n_candidates=len(notes),
        duration_s=duration_s,
    )
    chord_groups = _chord_groups(notes)

    # Score each note. Chord partners (multiple notes at the same t with
    # different lanes) are intentional musical events from the lane
    # assigner; they always score above the keep cutoff so a chord stack
    # never gets half-kept or fully dropped by selectivity.
    scores = [_score_note(notes, i, energy_buckets) for i in range(len(notes))]
    for group in chord_groups.values():
        if len(group) <= 1:
            continue
        for i in group:
            scores[i] = float("inf")

    # Pick top keep_ratio by score. Ties keep stable order (earlier note
    # wins) so chord partners with identical scores both survive.
    n_keep = max(1, int(round(len(notes) * keep_ratio)))
    indexed = sorted(range(len(notes)), key=lambda i: (-scores[i], i))
    selected = set(indexed[:n_keep])
    # Promote chord partners of selected notes so groups stay whole.
    for i in list(selected):
        for j in chord_groups.get(notes[i].t, ()):
            selected.add(j)

    kept = [notes[i] for i in sorted(selected)]

    # Final pass: keep notes in beats-aware time order and apply the
    # sanity cap so no 1-second window has more than SANITY_NOTES_PER_WINDOW.
    return _enforce_sanity_cap(kept)


def _score_note(
    notes: list[RawNote],
    idx: int,
    energy_buckets: list[int] | None,
) -> float:
    """Importance score for note[idx]. Higher = more likely to keep.

    Inputs:
      - n.strength (0-1 normalised by the onset detector)
      - local energy bucket (verse=low, chorus=high)
    """
    n = notes[idx]
    base = float(n.strength)
    if energy_buckets and idx < len(energy_buckets):
        bucket = energy_buckets[idx]
        if 0 <= bucket < len(_ENERGY_MULT):
            base *= _ENERGY_MULT[bucket]
    return base


def _chord_groups(notes: list[RawNote]) -> dict[float, list[int]]:
    """Map onset time -> indices of notes that share that time (chord partners)."""
    out: dict[float, list[int]] = {}
    for i, n in enumerate(notes):
        out.setdefault(n.t, []).append(i)
    return out


def _enforce_sanity_cap(notes: list[RawNote]) -> list[RawNote]:
    """Drop the weakest notes inside any 1-second window over the cap.

    Sliding right edge i: while count of kept notes in [t_i - 1s, t_i] exceeds
    SANITY_NOTES_PER_WINDOW, mark the lowest-strength note in that window as
    dropped. Continues until the window is under cap.
    """
    if not notes:
        return notes
    n = len(notes)
    dropped = [False] * n
    times = [x.t for x in notes]
    strengths = [x.strength for x in notes]

    window_start = 0
    for window_end in range(n):
        # Move window_start so window_end's time minus window_start's time <= SANITY_WINDOW_S.
        while times[window_end] - times[window_start] > SANITY_WINDOW_S:
            window_start += 1
        # Drop weakest until under cap.
        while True:
            alive = [i for i in range(window_start, window_end + 1) if not dropped[i]]
            if len(alive) <= SANITY_NOTES_PER_WINDOW:
                break
            weakest = min(alive, key=lambda i: strengths[i])
            dropped[weakest] = True

    return [n for i, n in enumerate(notes) if not dropped[i]]


def _calibrated_ratio(
    *,
    difficulty: str,
    n_candidates: int,
    duration_s: float,
) -> float:
    """Pick the keep ratio that lands this song in the difficulty's target rate band."""
    lo, hi = TARGET_NOTES_PER_SEC.get(difficulty, TARGET_NOTES_PER_SEC["normal"])
    target_mid = 0.5 * (lo + hi)
    if n_candidates <= 0 or duration_s <= 0:
        return 0.5
    desired_total = target_mid * duration_s
    ratio = desired_total / n_candidates
    return max(_MIN_RATIO, min(_MAX_RATIO, ratio))


def _infer_duration(
    *,
    notes: list[RawNote],
    beats: list[float],
    song_duration_s: float | None,
) -> float:
    """Best-effort song duration. Prefer the caller's value, then beats, then notes."""
    if song_duration_s is not None and song_duration_s > 0:
        return float(song_duration_s)
    if beats and len(beats) >= 2:
        return float(beats[-1])
    if notes:
        return float(notes[-1].t - notes[0].t) or 1.0
    return 1.0


def target_density(difficulty: str) -> float:
    """Approximate notes-per-beat hint. No longer enforced; for callers
    that want a rough sense of what 'difficulty X' aims at."""
    return _DENSITY_TARGETS.get(difficulty, 1.5)


def approximate_density(notes: list[RawNote], beats: list[float]) -> float:
    if len(notes) < 2 or len(beats) < 2:
        return 0.0
    avg_beat_period = (beats[-1] - beats[0]) / max(len(beats) - 1, 1)
    duration = notes[-1].t - notes[0].t
    return len(notes) / max(duration / avg_beat_period, 1.0)
