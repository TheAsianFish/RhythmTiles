"""Beat tracking. Two backends behind one interface.

`detect_beats` dispatches between:
  - librosa.beat.beat_track (default, fast, decent on steady-tempo pop)
  - Beat This! (CPJKU, ISMIR 2024 SOTA) when `USE_BEAT_THIS=1` and the
    `beat_this` package is importable. Beat This! also returns downbeat
    times, which the chart builder uses to emit musically-grounded accent
    chord stacks and per-section difficulty pacing.

The Beat This! path is cache-aware (audio content hash -> beat list)
because inference takes a few seconds on CPU and the chart cache may
ask us to regenerate the same audio at multiple difficulties.

All ML failures degrade gracefully to the librosa path. The heuristic
pipeline is always the rollback. See docs/ML_PLAN.md Phase 1.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import settings
from app.ml import beat_cache, beat_this

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("beatbridge.pipeline.beat_track")


@dataclass
class BeatInfo:
    bpm: float
    beats: list[float]
    # First beat of each bar, in seconds. Populated by the Beat This! path
    # and used by the lane assigner to fire accent chord stacks on real
    # downbeats. None when the librosa path is used (it has no downbeat
    # detector); callers must handle both cases.
    downbeats: list[float] | None = None
    # Piecewise (time_s, bpm) tempo curve. Populated when we have enough
    # beats to compute meaningful local tempo. Callers use this to drive
    # tempo-aware difficulty pacing (e.g. denser holds on slow sections,
    # tighter min-gaps on fast).
    bpm_curve: list[tuple[float, float]] | None = None
    # Which backend produced these beats. Useful in logs and in the bench
    # tool; never branched on by gameplay code.
    source: str = "librosa"


def detect_beats(
    *,
    y: "np.ndarray",
    sr: int,
    content_hash: str | None = None,
    use_beat_this: bool | None = None,
    device: str | None = None,
) -> BeatInfo:
    """Run beat tracking. Returns tempo, beats, downbeats, and bpm curve.

    Dispatch:
      - explicit `use_beat_this=True` AND beat_this importable -> Beat This!
      - explicit `use_beat_this=False` -> librosa, even if env says otherwise
      - `use_beat_this=None` (default) -> read settings.use_beat_this

    The explicit kwarg lets bench / test code override the backend per call
    without monkeypatching env vars and reloading modules. The env-driven
    default is what production goes through.

    The `content_hash` argument enables the disk cache for Beat This!
    outputs (see app/ml/beat_cache.py). Pass it whenever the audio came
    from a hashable source; pass None for synthetic / test buffers.
    """
    use_bt = settings.use_beat_this if use_beat_this is None else use_beat_this
    if use_bt and beat_this.is_available():
        cached = beat_cache.get(content_hash=content_hash, source="beat-this")
        if cached is not None:
            logger.info(
                "beat cache hit (beat-this) hash=%s bpm=%.1f beats=%d downbeats=%d",
                content_hash,
                cached.bpm,
                len(cached.beats),
                len(cached.downbeats or ()),
            )
            return BeatInfo(
                bpm=cached.bpm,
                beats=cached.beats,
                downbeats=cached.downbeats,
                bpm_curve=cached.bpm_curve,
                source="beat-this (cached)",
            )
        info = _detect_with_beat_this(y=y, sr=sr, device=device)
        if info is not None:
            beat_cache.put(
                content_hash=content_hash,
                source="beat-this",
                bpm=info.bpm,
                beats=info.beats,
                downbeats=info.downbeats,
                bpm_curve=info.bpm_curve,
            )
            return info
        # Fell through; the ML module already logged why. Use librosa.
        logger.warning("Beat This! returned no result, falling back to librosa")

    return _detect_with_librosa(y=y, sr=sr, content_hash=content_hash)


def _detect_with_beat_this(
    *,
    y: "np.ndarray",
    sr: int,
    device: str | None = None,
) -> BeatInfo | None:
    """Beat This! path. Returns None on any failure so the caller can fall back."""
    t0 = time.perf_counter()
    result = beat_this.detect(y=y, sr=sr, device=device)
    elapsed = time.perf_counter() - t0
    if result is None:
        return None
    if not result.beats:
        # Detector ran but found nothing (silent or extremely short audio).
        # Treat as a no-op fall-through; librosa will say the same thing.
        logger.warning("Beat This! produced no beats; falling back to librosa")
        return None

    bpm = _bpm_from_beats(result.beats)
    bpm_curve = _bpm_curve_from_beats(result.beats)
    downbeats = _sanity_filter_downbeats(
        beats=result.beats, downbeats=result.downbeats,
    )
    logger.info(
        "Beat This! detect elapsed=%.2fs beats=%d downbeats=%d (raw=%d) bpm=%.1f",
        elapsed,
        len(result.beats),
        len(downbeats or []),
        len(result.downbeats),
        bpm,
    )
    return BeatInfo(
        bpm=bpm,
        beats=result.beats,
        downbeats=downbeats,
        bpm_curve=bpm_curve,
        source="beat-this",
    )


# Cap on downbeats / beats. Real time signatures put downbeats at 1/4
# (4/4), 1/3 (3/4), 1/6 (6/8), or 1/2 (2/4). A ratio above this means
# the downbeat head is fooling itself on overly-regular or out-of-
# distribution audio (e.g. synthetic click tracks, drone music). When
# that happens, dropping the downbeat list is much better than emitting
# a chord stack on nearly every onset.
_MAX_PLAUSIBLE_DOWNBEAT_RATIO = 0.55


def _sanity_filter_downbeats(
    *,
    beats: list[float],
    downbeats: list[float],
) -> list[float] | None:
    """Return downbeats if plausible, None otherwise."""
    if not downbeats:
        return None
    if not beats:
        return downbeats
    ratio = len(downbeats) / len(beats)
    if ratio > _MAX_PLAUSIBLE_DOWNBEAT_RATIO:
        logger.warning(
            "Beat This! downbeat ratio %.2f exceeds plausibility cap %.2f; "
            "dropping downbeat list and falling back to strength/centroid accents.",
            ratio,
            _MAX_PLAUSIBLE_DOWNBEAT_RATIO,
        )
        return None
    return downbeats


def _detect_with_librosa(
    *,
    y: "np.ndarray",
    sr: int,
    content_hash: str | None = None,
) -> BeatInfo:
    """librosa.beat.beat_track. The baseline / fallback path.

    Cache key uses source="librosa" so a future Beat This! cache entry
    for the same audio doesn't collide.
    """
    cached = beat_cache.get(content_hash=content_hash, source="librosa")
    if cached is not None:
        return BeatInfo(
            bpm=cached.bpm,
            beats=cached.beats,
            downbeats=None,  # librosa has no downbeats
            bpm_curve=cached.bpm_curve,
            source="librosa (cached)",
        )

    import librosa  # noqa: WPS433
    import numpy as np  # noqa: WPS433

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    tempo_scalar = float(np.atleast_1d(np.asarray(tempo)).ravel()[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    beats = [float(b) for b in beat_times]
    bpm_curve = _bpm_curve_from_beats(beats)

    beat_cache.put(
        content_hash=content_hash,
        source="librosa",
        bpm=tempo_scalar,
        beats=beats,
        downbeats=None,
        bpm_curve=bpm_curve,
    )
    return BeatInfo(
        bpm=tempo_scalar,
        beats=beats,
        downbeats=None,
        bpm_curve=bpm_curve,
        source="librosa",
    )


def _bpm_from_beats(beats: list[float]) -> float:
    """Median inter-beat interval -> BPM. Robust to outliers.

    Beat This! doesn't return a "tempo" scalar; we derive it. Using the
    median (not the mean) lets a single dropped or doubled beat detection
    not skew the global tempo number that shows up in the popup HUD.
    """
    if len(beats) < 2:
        return 0.0
    intervals = sorted(b2 - b1 for b1, b2 in zip(beats[:-1], beats[1:]))
    mid = len(intervals) // 2
    if len(intervals) % 2:
        median = intervals[mid]
    else:
        median = 0.5 * (intervals[mid - 1] + intervals[mid])
    if median <= 0:
        return 0.0
    return 60.0 / median


def _bpm_curve_from_beats(
    beats: list[float],
    *,
    window: int = 8,
) -> list[tuple[float, float]] | None:
    """Produce a piecewise tempo curve from beat times.

    For each beat we compute the local tempo as 60 / (avg interval over
    the next `window` beats). The curve is downsampled to one point per
    `window` beats so it stays compact in the Chart JSON. Returns None
    when there are too few beats to make a meaningful curve.

    This is the field the Chart JSON contract advertises as `bpmCurve`.
    Today nothing in the game uses it for scoring, but it's there for the
    HUD ("song slowing down here") and for future tempo-aware pacing.
    """
    n = len(beats)
    if n < window + 2:
        return None
    curve: list[tuple[float, float]] = []
    step = max(1, window // 2)
    for i in range(0, n - window, step):
        local_intervals = [beats[j + 1] - beats[j] for j in range(i, i + window)]
        avg = sum(local_intervals) / len(local_intervals)
        if avg <= 0:
            continue
        curve.append((float(beats[i]), float(60.0 / avg)))
    if not curve:
        return None
    return curve
