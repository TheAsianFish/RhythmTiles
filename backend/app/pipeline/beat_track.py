"""Beat tracking. librosa for v1; swap to madmom later via the same interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


@dataclass
class BeatInfo:
    bpm: float
    beats: list[float]
    bpm_curve: list[tuple[float, float]] | None = None


def detect_beats(*, y: "np.ndarray", sr: int) -> BeatInfo:
    """Run librosa beat tracking. Returns global tempo and beat times in seconds.

    librosa.beat.beat_track gives a single tempo plus beat frames. For a v1
    constant-BPM chart this is enough. bpm_curve stays None until Stage 2
    decides we need per-segment tempo (madmom).
    """
    import librosa  # noqa: WPS433
    import numpy as np  # noqa: WPS433

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    # librosa returns tempo as a 1-element numpy array in recent versions.
    tempo_scalar = float(np.atleast_1d(np.asarray(tempo)).ravel()[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    return BeatInfo(bpm=tempo_scalar, beats=[float(b) for b in beat_times], bpm_curve=None)
