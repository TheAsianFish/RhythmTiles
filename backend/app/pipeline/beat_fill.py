"""Beat-grid augmentation.

Two complementary augmentations after the main detect_onsets pass:

1. fill_empty_beats: insert synthetic onsets where detection failed for
   2+ consecutive beats. Stops vocal-only choruses from going dead.

2. add_subdivision_onsets: insert half-beat and (Expert-only) quarter-
   beat onsets in sections where the audio supports them (RMS energy
   is meaningful and no real onset already lands there). This is what
   gives Hard / Expert genuine density above Normal: without these
   subdivisions the thinner runs out of candidates and the top tiers
   collapse onto Normal's note count.

3. detect_crescendos: identifies rising-energy stretches (2+ beats of
   monotonically increasing smoothed RMS). add_subdivision_onsets
   forces half-beat subdivisions inside these regardless of base
   difficulty so the build-to-chorus has musical weight.

All augmentations preserve the (sorted, deduped) onset contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.pipeline.onset_detect import Onset

if TYPE_CHECKING:
    import numpy as np

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

# Subdivision-insertion thresholds. A subdivision is added only when the
# RMS at that sub-beat position exceeds this fraction of the song's median
# RMS AND clears an absolute floor, so we don't drop synthetic notes into
# actual silence.
SUBDIV_RMS_THRESHOLD = 0.40
SUBDIV_ABSOLUTE_RMS_FLOOR = 0.005

# How close (in seconds) a real onset has to be to a sub-beat position
# before we consider the slot "already covered" and skip subdivision.
SUBDIV_DEDUPE_S = 0.060

# Per-difficulty subdivision behaviour. Subdivisions feed the difficulty
# selector with more candidates; the selector then keeps a fraction of
# them based on score. Reduced aggression here so the candidate pool
# doesn't dominate and force button-mashing density.
#  - Hard: half-beats only in dense sections (current beat already has
#    a real onset).
#  - Expert: half-beats freely + quarter-beats only inside crescendos.
_SUBDIV_BY_DIFFICULTY = {
    "easy":   {"halves_everywhere": False, "halves_in_dense": False, "quarters_in_crescendo": False},
    "normal": {"halves_everywhere": False, "halves_in_dense": False, "quarters_in_crescendo": False},
    "hard":   {"halves_everywhere": False, "halves_in_dense": True,  "quarters_in_crescendo": False},
    "expert": {"halves_everywhere": True,  "halves_in_dense": True,  "quarters_in_crescendo": True},
}

# Crescendo detection. A "rising" beat-pair has RMS increase by at least this
# fraction over the previous beat; a crescendo is MIN_CRESCENDO_BEATS or
# more consecutive rising beats.
CRESCENDO_RISE_FRACTION = 0.07
MIN_CRESCENDO_BEATS = 3


def fill_empty_beats(
    onsets: list[Onset],
    beats: list[float],
    *,
    downbeats: list[float] | None = None,
) -> list[Onset]:
    """Return `onsets` augmented with synthetic events at long empty runs.

    When `downbeats` is provided (Beat This! path), any empty downbeat is
    filled regardless of run length. A bar start is a strong musical
    moment even if the inner beats are quiet; leaving it empty produces
    the "song dropped out then the chart skipped a beat" feel that the
    fill pass exists to prevent. Inner empty beats still need a run of
    MIN_EMPTY_RUN_BEATS to fill.
    """
    if not beats or len(beats) < 2:
        return onsets
    onset_times = sorted(o.t for o in onsets)
    has_onset = _per_beat_coverage(beats=beats, onset_times=onset_times)

    downbeat_beat_indices: set[int] = set()
    if downbeats:
        downbeat_beat_indices = _beats_at_downbeats(beats=beats, downbeats=downbeats)

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
        # Always fill any downbeat inside this run, even if the run is
        # shorter than MIN_EMPTY_RUN_BEATS.
        if run_len < MIN_EMPTY_RUN_BEATS:
            for k in range(start, i):
                if k in downbeat_beat_indices:
                    augmented.append(_synth_onset_at(beats[k], k))
            continue
        for k in range(start, i):
            augmented.append(_synth_onset_at(beats[k], k))

    augmented.sort(key=lambda o: o.t)
    return augmented


def _synth_onset_at(t: float, beat_index: int) -> Onset:
    """One synthetic Onset on the beat grid, alternating centroid between bands."""
    return Onset(
        t=float(t),
        strength=SYNTH_STRENGTH,
        centroid_hz=SYNTH_CENTROIDS[beat_index % 2],
    )


def _beats_at_downbeats(
    *,
    beats: list[float],
    downbeats: list[float],
    tolerance_s: float = 0.10,
) -> set[int]:
    """Return the set of beat indices whose time is closest to a downbeat.

    Beat This! reports beats and downbeats from independent heads; the
    downbeat times usually coincide with beat times but can drift by a
    few ms. Match each downbeat to its nearest beat to get the index we
    care about.
    """
    out: set[int] = set()
    if not downbeats:
        return out
    sorted_beats = beats
    j = 0
    n = len(sorted_beats)
    for db in downbeats:
        while j + 1 < n and sorted_beats[j + 1] <= db:
            j += 1
        candidate = j
        if j + 1 < n and abs(sorted_beats[j + 1] - db) < abs(sorted_beats[candidate] - db):
            candidate = j + 1
        if abs(sorted_beats[candidate] - db) <= tolerance_s:
            out.add(candidate)
    return out


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


def add_subdivision_onsets(
    onsets: list[Onset],
    beats: list[float],
    *,
    y: "np.ndarray",
    sr: int,
    difficulty: str,
    hop_length: int = 512,
) -> list[Onset]:
    """Insert half-beat / quarter-beat subdivisions where audio supports them.

    Without this pass the thinner runs out of candidates at Hard / Expert
    and those tiers collapse onto Normal's density. With it, Hard genuinely
    plays denser than Normal and Expert denser than Hard, because the
    thinner has subdivided candidates to keep when its tight min_gap
    permits.

    Crescendos (rising RMS over MIN_CRESCENDO_BEATS+ beats) get half-beat
    subdivisions regardless of difficulty, since the user's intuition that
    "build to a chorus = more notes" is musically right at any tier.
    """
    rules = _SUBDIV_BY_DIFFICULTY.get(difficulty)
    if rules is None or len(beats) < 2:
        return onsets

    import librosa  # noqa: WPS433
    import numpy as np  # noqa: WPS433

    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    if len(rms) == 0:
        return onsets
    fps = sr / hop_length
    median_rms = float(np.median(rms))
    threshold = max(median_rms * SUBDIV_RMS_THRESHOLD, SUBDIV_ABSOLUTE_RMS_FLOOR)

    onset_times = sorted(o.t for o in onsets)

    # Per-beat onset density count, for "dense section" detection.
    real_per_beat = _onsets_per_beat(beats=beats, onset_times=onset_times)
    crescendo_beats = _detect_crescendo_beats(
        beats=beats, rms=rms, fps=fps, median_rms=median_rms,
    )

    new_onsets: list[Onset] = list(onsets)
    for i in range(len(beats) - 1):
        beat_a = beats[i]
        beat_b = beats[i + 1]
        midpoint = 0.5 * (beat_a + beat_b)

        in_crescendo = i in crescendo_beats
        in_dense_section = (
            i > 0
            and real_per_beat[i] >= 1
            and real_per_beat[i - 1] >= 1
        )

        # Half-beat subdivision rule. Three gates from least to most
        # restrictive:
        #  - halves_everywhere: anywhere with audio support (Expert)
        #  - halves_in_dense:   only where surrounding beats have onsets (Hard)
        #  - in_crescendo:      forced on rising-energy sections regardless
        place_half = (
            rules["halves_everywhere"]
            or (rules["halves_in_dense"] and in_dense_section)
            or in_crescendo
        )
        if place_half:
            _maybe_add(
                new_onsets,
                t=midpoint,
                rms_val=_rms_at(rms, midpoint, fps),
                threshold=threshold,
                onset_times=onset_times,
                centroid=SYNTH_CENTROIDS[i % 2],
            )

        # Quarter-beat subdivisions only inside crescendos for Expert.
        # The selector + sanity cap will trim if too many land in a row.
        if rules["quarters_in_crescendo"] and in_crescendo:
            q1 = beat_a + 0.25 * (beat_b - beat_a)
            q3 = beat_a + 0.75 * (beat_b - beat_a)
            _maybe_add(
                new_onsets,
                t=q1,
                rms_val=_rms_at(rms, q1, fps),
                threshold=threshold,
                onset_times=onset_times,
                centroid=SYNTH_CENTROIDS[(i + 1) % 2],
            )
            _maybe_add(
                new_onsets,
                t=q3,
                rms_val=_rms_at(rms, q3, fps),
                threshold=threshold,
                onset_times=onset_times,
                centroid=SYNTH_CENTROIDS[i % 2],
            )

    new_onsets.sort(key=lambda o: o.t)
    return new_onsets


def _maybe_add(
    new_onsets: list[Onset],
    *,
    t: float,
    rms_val: float,
    threshold: float,
    onset_times: list[float],
    centroid: float,
) -> None:
    """Insert a synthetic onset at t unless audio is quiet or t is already covered."""
    if rms_val < threshold:
        return
    if _has_nearby(onset_times, t, SUBDIV_DEDUPE_S):
        return
    # Synthetic onsets get strength a hair above SYNTH_STRENGTH for
    # crescendos so they survive thinning at the bottom of the rise.
    new_onsets.append(Onset(t=float(t), strength=SYNTH_STRENGTH, centroid_hz=float(centroid)))


def _onsets_per_beat(
    *,
    beats: list[float],
    onset_times: list[float],
) -> list[int]:
    """Count onsets that fall within each beat-bucket [beats[i], beats[i+1])."""
    counts = [0] * len(beats)
    j = 0
    for i in range(len(beats) - 1):
        lo, hi = beats[i], beats[i + 1]
        while j < len(onset_times) and onset_times[j] < lo:
            j += 1
        k = j
        while k < len(onset_times) and onset_times[k] < hi:
            counts[i] += 1
            k += 1
    return counts


def _detect_crescendo_beats(
    *,
    beats: list[float],
    rms: "np.ndarray",
    fps: float,
    median_rms: float,
) -> set[int]:
    """Indices of beats that are part of a rising-energy stretch.

    Per-beat energy is the average RMS in that beat's window. A beat is
    "rising" if its energy exceeds the previous beat's by at least
    CRESCENDO_RISE_FRACTION of median. Runs of MIN_CRESCENDO_BEATS or
    more consecutive rising beats are marked as crescendo.
    """
    if len(beats) < MIN_CRESCENDO_BEATS + 1:
        return set()

    per_beat_energy = []
    for i in range(len(beats) - 1):
        lo = int(beats[i] * fps)
        hi = int(beats[i + 1] * fps)
        if lo >= len(rms):
            per_beat_energy.append(0.0)
            continue
        hi = min(hi, len(rms))
        if hi <= lo:
            per_beat_energy.append(float(rms[lo]))
        else:
            per_beat_energy.append(float(rms[lo:hi].mean()))

    rise_threshold = median_rms * CRESCENDO_RISE_FRACTION
    rising = [False] * len(per_beat_energy)
    for i in range(1, len(per_beat_energy)):
        rising[i] = per_beat_energy[i] - per_beat_energy[i - 1] >= rise_threshold

    out: set[int] = set()
    i = 0
    while i < len(rising):
        if not rising[i]:
            i += 1
            continue
        start = i
        while i < len(rising) and rising[i]:
            i += 1
        run_len = i - start
        if run_len >= MIN_CRESCENDO_BEATS:
            for k in range(start, i):
                out.add(k)
    return out


def _rms_at(rms: "np.ndarray", t: float, fps: float) -> float:
    f = int(t * fps)
    if f < 0 or f >= len(rms):
        return 0.0
    return float(rms[f])


def _has_nearby(onset_times: list[float], t: float, tol: float) -> bool:
    # Binary search would be cleaner; linear scan is fine for chart-sized lists.
    for ot in onset_times:
        if ot > t + tol:
            return False
        if abs(ot - t) <= tol:
            return True
    return False
