"""Onset detection. Returns time + spectral centroid for lane assignment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


@dataclass
class Onset:
    t: float                # seconds from audio start
    strength: float         # normalized onset strength, 0-1
    centroid_hz: float      # spectral centroid at the onset frame


def detect_onsets(*, y: "np.ndarray", sr: int, hop_length: int = 512) -> list[Onset]:
    """Spectral-flux onset detection on the full mix.

    For each detected onset frame we also sample the spectral centroid so the
    lane assigner can route low-frequency hits (kicks) to the left lanes and
    high-frequency hits (snares, hi-hats, vocals) to the right.
    """
    import librosa  # noqa: WPS433
    import numpy as np  # noqa: WPS433

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

    # Spectral centroid per frame.
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop_length)[0]

    # Strengths sampled at onset frames and normalized to 0-1 over this clip.
    raw_strength = onset_env[onset_frames]
    smax = float(raw_strength.max()) if len(raw_strength) else 1.0
    strengths = raw_strength / smax if smax > 0 else raw_strength

    result: list[Onset] = []
    n_frames = len(centroid)
    for frame_idx, t, s in zip(onset_frames, onset_times, strengths):
        f = int(min(frame_idx, n_frames - 1))
        result.append(
            Onset(t=float(t), strength=float(s), centroid_hz=float(centroid[f])),
        )
    return result
