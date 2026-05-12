"""Onset detection. Returns time + spectral centroid for lane assignment.

Default mode runs two onset detectors and merges them:
  - "energy" envelope (librosa default) catches percussive events well.
  - "cqt" envelope (constant-Q transform) catches pitched / vocal events
    that the energy envelope tends to miss on softer choruses.

Both streams are merged with a small min-gap dedupe so a single physical
event detected by both detectors doesn't produce two notes. On songs with
vocal-driven choruses this picks up melodic note attacks that the old
single-envelope path missed; on drum-heavy songs it adds little.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


# Two onset events less than this far apart are treated as the same event.
# Matches the lane-assigner's HIT_WINDOW_S so we never queue two notes the
# game can't tell apart anyway.
_DEDUPE_GAP_S = 0.060


@dataclass
class Onset:
    t: float                # seconds from audio start
    strength: float         # normalized onset strength, 0-1
    centroid_hz: float      # spectral centroid at the onset frame


def detect_onsets(*, y: "np.ndarray", sr: int, hop_length: int = 512) -> list[Onset]:
    """Combined energy + CQT onset detection on the given audio.

    For each detected onset frame we sample the spectral centroid so the
    lane assigner can route low-frequency hits (kicks) to the left lanes
    and high-frequency hits (snares, hi-hats, vocals) to the right.
    """
    import librosa  # noqa: WPS433
    import numpy as np  # noqa: WPS433

    # Pre-compute the centroid once. Both onset streams share it.
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop_length)[0]
    n_frames = int(len(centroid))

    energy_onsets = _onsets_from_envelope(
        y=y,
        sr=sr,
        hop_length=hop_length,
        feature="energy",
        centroid=centroid,
        n_frames=n_frames,
    )
    cqt_onsets = _onsets_from_envelope(
        y=y,
        sr=sr,
        hop_length=hop_length,
        feature="cqt",
        centroid=centroid,
        n_frames=n_frames,
    )

    merged = _merge_onset_lists(energy_onsets, cqt_onsets)
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
    """Run librosa onset detection with the given envelope feature."""
    import librosa  # noqa: WPS433

    try:
        onset_env = librosa.onset.onset_strength(
            y=y,
            sr=sr,
            hop_length=hop_length,
            feature=getattr(librosa.feature, feature, None) if feature != "energy" else None,
        )
    except Exception:
        # If the requested feature isn't available (older librosa), fall back
        # to the default envelope so we don't lose all onsets.
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env,
        sr=sr,
        hop_length=hop_length,
        units="frames",
        backtrack=False,
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
    """Merge two time-sorted onset lists, deduping events within _DEDUPE_GAP_S.

    When two onsets collide, the one with higher strength wins. This biases
    the output toward the more confident detector for each musical event.
    """
    merged = sorted(a + b, key=lambda o: o.t)
    if not merged:
        return merged
    out: list[Onset] = [merged[0]]
    for o in merged[1:]:
        prev = out[-1]
        if o.t - prev.t < _DEDUPE_GAP_S:
            if o.strength > prev.strength:
                out[-1] = o
        else:
            out.append(o)
    return out
