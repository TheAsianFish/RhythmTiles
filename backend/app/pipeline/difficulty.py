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
# Bands picked to feel right for typical pop/rock at ~120-180 BPM.
#
# Calibration history:
#  - Earliest values had Normal/Hard collapsing onto a thin skeleton on
#    vocal-driven songs. Normal and Hard were raised so vocal syllables
#    survive selectivity. (commit 5f1073c)
#  - Hard 5.0-6.8 and Expert 5.8-7.6 then OVERLAPPED by 1 n/s, so the
#    per-song calibrator landed them at nearly identical ratios on most
#    songs. Playtesting confirmed tiers felt indistinguishable.
#  - Pre-v0.7.0 bands (0.7-1.5 / 2.5-3.8 / 4.6-5.9 / 6.3-7.8) felt
#    lackluster compared to actual osu!mania ranked charts where even
#    Beginner runs 1.5-2.5 NPS and Insane hits 7-10. The runway is also
#    longer in osu!mania so more notes fit on screen.
#  - v0.7.0 bumped all tiers to roughly match osu!mania difficulty
#    progression. Easy mid 1.1 -> 2.0 nps doubled the density and
#    felt too hard for auto-generated charts (no human pattern intent
#    to offset the density). v0.8.0 pulled back halfway but still
#    felt too hard, especially because the Easy bump was the largest.
#  - v0.9.0 reverts to the ORIGINAL bands times a uniform 1.05
#    across all tiers. Just a nudge - the runway bump (overlay panel
#    744->900) does most of the user-facing "feels more responsive"
#    work; density barely moves.
TARGET_NOTES_PER_SEC = {
    "easy":   (0.74, 1.58),   # mid 1.16  - original 1.1 mid x 1.05
    "normal": (2.63, 3.99),   # mid 3.31  - original 3.15 mid x 1.05
    "hard":   (4.83, 6.20),   # mid 5.51  - original 5.25 mid x 1.05
    "expert": (6.62, 8.19),   # mid 7.40  - original 7.05 mid x 1.05
}

# Absolute bounds on the derived ratio so degenerate inputs (tiny or huge
# candidate sets) don't push selection past sanity.
_MIN_RATIO = 0.05
_MAX_RATIO = 0.98

# Energy-bucket bonus to keep_score: low energy verse notes get a small
# discount, chorus notes get a small boost. The multiplier biases WHICH
# notes survive global thinning rather than HOW MANY survive per section.
# Subtle by design - off-beat phrasing notes need to survive thinning
# for the chart to feel like it has flow.
#
# History:
#  - v0.3.0 tried per-bucket thinning with a 0.65/1.30 split. Stripped
#    verses to their predictable downbeats. Reverted.
#  - v0.5.0 widened the range to 0.80/1.30. Made ML-light worse along
#    with ML-max (the RMS path also uses these). Reverted in v0.6.0.
_ENERGY_MULT = (0.90, 1.00, 1.15)

# Sanity cap: no more than this many notes inside any sliding window of
# SANITY_WINDOW_S seconds. Drops the lowest-strength notes in over-dense
# windows. Reverted to original 8 in v0.9.0 alongside the density pull-back.
# 8/s is roughly osu!mania "Insane" peak density across four lanes
# (2 notes/sec per hand) and the upper bound of sustained play.
SANITY_NOTES_PER_WINDOW = 8
SANITY_WINDOW_S = 1.0

# Window length for the time-aware thinner. Top-K-per-window is enforced
# in this window. 2s is roughly one phrase at 120 BPM; small enough to
# break "all notes clustered in chorus, dead verses" but large enough that
# legitimate quiet beats stay quiet.
SELECT_WINDOW_S = 2.0


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

    # Time-windowed selection (v0.3.0). Walks the chart in SELECT_WINDOW_S-
    # second windows and keeps the top-K notes per window where K is the
    # difficulty's target NPS times the window length. This prevents the
    # old global top-K behaviour where strong onsets in a chorus would
    # dominate the keep budget and leave verses empty.
    #
    # After windowed selection, if the total note count fell well below
    # what the difficulty band asks for (sparse song or many windows hit
    # cap), backfill the highest-scoring unselected notes to make up
    # SHORTFALL_FILL_FRACTION of the shortfall.
    selected = _select_windowed_by_score(
        notes=notes,
        scores=scores,
        difficulty=difficulty,
        duration_s=duration_s,
    )
    # Promote chord partners of selected notes so groups stay whole.
    for i in list(selected):
        for j in chord_groups.get(notes[i].t, ()):
            selected.add(j)

    # Sort by time (not by index) so the downstream sanity cap, hold
    # detector, and chord-pair logic all see the chronologically-ordered
    # list they expect. Production always passes time-sorted input but
    # the test suite shuffles to verify robustness.
    kept = sorted((notes[i] for i in selected), key=lambda n: (n.t, n.lane))

    # Final pass: keep notes in beats-aware time order and apply the
    # sanity cap so no 1-second window has more than SANITY_NOTES_PER_WINDOW.
    return _enforce_sanity_cap(kept)


def _select_windowed_by_score(
    *,
    notes: list[RawNote],
    scores: list[float],
    difficulty: str,
    duration_s: float,
) -> set[int]:
    """Window-aware top-K selector. Returns the SET of note indices to keep.

    Algorithm:
      1. Compute target_K_per_window = target_nps_mid * SELECT_WINDOW_S.
         This is roughly how many notes a window "deserves" given the
         difficulty's target density.
      2. Walk the chart in SELECT_WINDOW_S-second windows from t=0 to
         t=duration_s. For each window, sort the candidate notes in it
         by score desc, keep the top target_K_per_window.
      3. After all windows have run, count how many notes were selected.
         If the total is below the difficulty's LOWER band bound, that's
         under-fill (the song was so sparse that even keeping top-K per
         window left us under target). Backfill by adding back the
         highest-scoring unselected notes globally until we reach the
         lower bound or until we've added SHORTFALL_FILL_FRACTION of the
         shortfall.

    The output preserves "holistic timing": notes stay at their original
    times (we don't move them) and chord pairs are preserved via the
    inf-score promotion the caller already applied.
    """
    if not notes:
        return set()

    lo, hi = TARGET_NOTES_PER_SEC.get(difficulty, TARGET_NOTES_PER_SEC["normal"])
    target_mid = 0.5 * (lo + hi)
    target_per_window = max(1, int(round(target_mid * SELECT_WINDOW_S)))

    # Bucket notes by window index. Walking in time order means notes
    # at window boundaries are placed deterministically (the window
    # whose [start, end) contains t).
    n_windows = max(1, int(duration_s / SELECT_WINDOW_S) + 1)
    by_window: list[list[int]] = [[] for _ in range(n_windows)]
    for i, n in enumerate(notes):
        w = min(int(n.t / SELECT_WINDOW_S), n_windows - 1)
        if w < 0:
            w = 0
        by_window[w].append(i)

    selected: set[int] = set()
    for window in by_window:
        if not window:
            continue
        # Sort by score desc, ties broken by lower index (stable).
        window.sort(key=lambda i: (-scores[i], i))
        # Take top target_per_window. If the window has fewer candidates,
        # take what we have (sparse moment = sparse chart, deliberately).
        for idx in window[:target_per_window]:
            selected.add(idx)

    # No shortfall backfill. If a song is so sparse that the windowed
    # pass falls below the difficulty's lo NPS, the chart will simply
    # be sparse in those moments — which IS the correct behaviour
    # (the music is quiet here, the chart is quiet here). The original
    # global top-K thinner appeared to "reach target NPS" only by
    # clustering all surviving notes in the densest moments of the
    # song, which is exactly the bug we're fixing. Trading "honest
    # sparsity" for "fake density" doesn't help the player.
    return selected


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
