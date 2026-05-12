"""Onset detection. Returns time + spectral centroid for lane assignment.

Default mode runs THREE onset detectors and merges them:
  - "energy" envelope on full mix (librosa default) catches percussive
    events well.
  - "cqt" envelope (constant-Q transform) on full mix catches pitched
    events that the energy envelope tends to miss on softer choruses.
  - HARMONIC stream via librosa.effects.hpss + energy envelope on the
    harmonic component, which strips drums and isolates vocal/melodic
    onsets. This is the big win on JPOP / vocal-driven choruses where
    drums get quiet and the chart used to go nearly empty.

Sync notes:
  - `backtrack=True` on the detector traces each onset back to the
    local minimum BEFORE the energy spike. That's much closer to where
    the human ear places the note start than the spike peak itself
    (the spike is the loudest moment, not the attack moment).
  - When two detectors find the same event within DEDUPE_GAP_S, we
    keep the EARLIER timestamp (not the higher-strength one), since
    earlier is closer to perceptual onset.
  - When two onsets are close in time but their spectral centroids
    are very different (CENTROID_SEPARATION_HZ apart), they're treated
    as distinct musical events (e.g. two piano keys struck together,
    or a kick and a hat at the same instant). This preserves chord-
    style multi-pitch events that a single detector can't separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


# Two onset events less than this far apart are CANDIDATES for dedupe.
# Tighter than before (was 60ms) because we now also gate on spectral
# centroid: close-but-different-pitch events survive.
_DEDUPE_GAP_S = 0.035

# Centroid spread above which two near-simultaneous onsets are treated
# as distinct musical events (a chord stack, not a duplicate detection).
# 800 Hz is roughly the distance between a bass note and a midrange one.
_CENTROID_SEPARATION_HZ = 800.0


@dataclass
class Onset:
    t: float                # seconds from audio start
    strength: float         # normalized onset strength, 0-1
    centroid_hz: float      # spectral centroid at the onset frame


def detect_onsets(*, y: "np.ndarray", sr: int, hop_length: int = 512) -> list[Onset]:
    """Combined energy + CQT + harmonic onset detection on the given audio.

    For each detected onset frame we sample the spectral centroid so the
    lane assigner can route low-frequency hits (kicks) to the left lanes
    and high-frequency hits (snares, hi-hats, vocals) to the right.
    """
    import librosa  # noqa: WPS433

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop_length)[0]
    n_frames = int(len(centroid))

    energy_onsets = _onsets_from_envelope(
        y=y, sr=sr, hop_length=hop_length, feature="energy",
        centroid=centroid, n_frames=n_frames,
    )
    cqt_onsets = _onsets_from_envelope(
        y=y, sr=sr, hop_length=hop_length, feature="cqt",
        centroid=centroid, n_frames=n_frames,
    )

    harmonic_onsets: list[Onset] = []
    try:
        y_harm, _y_perc = librosa.effects.hpss(y)
        harmonic_onsets = _onsets_from_envelope(
            y=y_harm, sr=sr, hop_length=hop_length, feature="energy",
            centroid=centroid, n_frames=n_frames,
        )
    except Exception:
        harmonic_onsets = []

    merged = _merge_onset_lists(energy_onsets, cqt_onsets)
    merged = _merge_onset_lists(merged, harmonic_onsets)
    return merged


def _onsets_from_envelope(
    *,
    y: "np.ndarray",
    sr: int,
    hop_length: int,
    feature: str,
    centroid: "np.ndarray",
    n_frames: int,
) -> list[Onset]:
    """Run librosa onset detection with the given envelope feature.

    Uses `backtrack=True` so each onset frame is the local minimum BEFORE
    the energy spike, which corresponds to the perceived attack of the
    note rather than its peak. This shifts onsets ~10-30ms earlier on
    average and removes the "feels late" artifact players noticed.
    """
    import librosa  # noqa: WPS433

    try:
        onset_env = librosa.onset.onset_strength(
            y=y,
            sr=sr,
            hop_length=hop_length,
            feature=getattr(librosa.feature, feature, None) if feature != "energy" else None,
        )
    except Exception:
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env,
        sr=sr,
        hop_length=hop_length,
        units="frames",
        backtrack=True,
    )
    if len(onset_frames) == 0:
        return []

    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length)
    raw_strength = onset_env[onset_frames]
    smax = float(raw_strength.max()) if len(raw_strength) else 1.0
    strengths = raw_strength / smax if smax > 0 else raw_strength

    result: list[Onset] = []
    for frame_idx, t, s in zip(onset_frames, onset_times, strengths):
        f = int(min(frame_idx, n_frames - 1))
        result.append(
            Onset(t=float(t), strength=float(s), centroid_hz=float(centroid[f])),
        )
    return result


def _merge_onset_lists(a: list[Onset], b: list[Onset]) -> list[Onset]:
    """Merge two time-sorted onset lists with centroid-aware dedupe.

    Two onsets within _DEDUPE_GAP_S are considered a duplicate detection
    UNLESS their spectral centroids differ by >= _CENTROID_SEPARATION_HZ
    (in which case they're distinct musical events at almost the same
    instant - e.g. a chord stack the detectors picked apart, or a kick +
    snare hit together). The earlier of two duplicates wins; this aligns
    onset times with perceptual attack rather than spike peak.
    """
    merged = sorted(a + b, key=lambda o: o.t)
    if not merged:
        return merged
    out: list[Onset] = [merged[0]]
    for o in merged[1:]:
        prev = out[-1]
        if o.t - prev.t < _DEDUPE_GAP_S:
            if abs(o.centroid_hz - prev.centroid_hz) >= _CENTROID_SEPARATION_HZ:
                # Distinct musical events at nearly the same time. Keep both.
                out.append(o)
            else:
                # Duplicate. Keep the earlier one (prev) but adopt the
                # stronger strength + middle-most centroid so downstream
                # decisions still benefit from both detectors' confidence.
                if o.strength > prev.strength:
                    prev.strength = o.strength
        else:
            out.append(o)
    return out


def snap_onsets_to_beats(
    onsets: list[Onset],
    beats: list[float],
    *,
    snap_tolerance_s: float = 0.022,
) -> list[Onset]:
    """Snap each onset to the nearest beat if it's within `snap_tolerance_s`.

    The beat tracker's beat positions are derived from the song's tempogram,
    which gives them a more "musical" grounding than spectral-flux peaks.
    If an onset detector reports a note at t=1.018s while the beat is at
    t=1.000s, snapping to the beat aligns the note with where the player
    feels the beat rather than where the spectrum peaks.

    Collision avoidance: if snapping two onsets would put them at the same
    beat, only the CLOSER one snaps and the second is left at its original
    time. Otherwise the snap manufactures fake chord stacks from genuinely
    sequential events that just happened to both be near the same beat.

    Onsets outside the tolerance are left alone so off-beat events stay
    off-beat. Mutates onset objects in place AND returns a sorted list.
    """
    if not onsets or not beats:
        return onsets
    sorted_beats = sorted(beats)
    nb = len(sorted_beats)
    # Track which beat times we've already snapped an onset to so a second
    # near-beat onset doesn't collide with it.
    snapped_to: set[float] = set()
    j = 0
    # We need stable iteration in time order and a way to compare candidate
    # distances, so do two passes: first find each onset's snap candidate,
    # then resolve collisions (winner is the onset closer to the beat).
    candidates: list[tuple[int, float, float]] = []  # (idx, target_beat, distance)
    for idx, o in enumerate(onsets):
        while j + 1 < nb and sorted_beats[j + 1] <= o.t:
            j += 1
        best_beat = sorted_beats[j]
        if j + 1 < nb and abs(sorted_beats[j + 1] - o.t) < abs(best_beat - o.t):
            best_beat = sorted_beats[j + 1]
        dist = abs(best_beat - o.t)
        if dist <= snap_tolerance_s:
            candidates.append((idx, float(best_beat), dist))

    # Resolve per beat: closest onset wins the snap; others stay put.
    candidates.sort(key=lambda c: c[2])  # smallest distance first
    snap_for_idx: dict[int, float] = {}
    for idx, beat_t, _dist in candidates:
        if beat_t in snapped_to:
            continue
        snap_for_idx[idx] = beat_t
        snapped_to.add(beat_t)

    for idx, beat_t in snap_for_idx.items():
        onsets[idx].t = beat_t

    onsets.sort(key=lambda o: o.t)
    return onsets
