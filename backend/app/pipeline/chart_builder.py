"""Glue that wires the pipeline stages into a Chart.

Stage 1 stub: this raises NotImplementedError for paths that need real audio.
Stage 2 lands the actual implementation that calls beat_track, onset_detect,
lane_assign, and difficulty in sequence.
"""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, timezone

import numpy as np

from app.models import (
    AudioMeta,
    Chart,
    ChartMeta,
    Note,
    PIPELINE_VERSION,
)
from app.config import settings
from app.pipeline.beat_fill import add_subdivision_onsets, fill_empty_beats
from app.pipeline.beat_track import detect_beats
from app.pipeline.difficulty import shape_difficulty
from app.pipeline.hold_detect import detect_holds
from app.pipeline.lane_assign import assign_lanes
from app.pipeline.onset_detect import detect_onsets, snap_onsets_to_beats
from app.pipeline.stems import separate_stems

logger = logging.getLogger("beatbridge.pipeline")


# Window used to smooth RMS into a "section energy" curve. Roughly the
# length of a musical phrase so individual beats and bars don't dominate.
_ENERGY_WINDOW_S = 4.0

# Per-difficulty tuning. Higher density + lower chord quantile + higher
# hold ratio = more challenge. Expert is meant to feel busy and technical,
# not random; the lane assigner's hand-balance and chord rules still apply.
_DIFFICULTY_TUNING = {
    "easy":   {"chord_quantile": 0.95, "hold_ratio": 0.02},
    "normal": {"chord_quantile": 0.88, "hold_ratio": 0.05},
    "hard":   {"chord_quantile": 0.82, "hold_ratio": 0.08},
    "expert": {"chord_quantile": 0.72, "hold_ratio": 0.12},
}


def _tuning_for(difficulty: str) -> dict:
    return _DIFFICULTY_TUNING.get(difficulty, _DIFFICULTY_TUNING["normal"])


def _energy_buckets_for_notes(
    *,
    notes,
    y: np.ndarray,
    sr: int,
) -> list[int]:
    """Bucket each note into 0 (low), 1 (medium), 2 (high) by local energy.

    Computes a smoothed RMS curve over `_ENERGY_WINDOW_S`-second windows,
    samples it at each note's time, then quantile-buckets by the 33/67
    percentile thresholds across all sampled values.
    """
    if not notes:
        return []
    import librosa  # noqa: WPS433

    hop = max(1, int(sr * (_ENERGY_WINDOW_S / 4.0)))
    frame_length = max(hop * 2, int(sr * _ENERGY_WINDOW_S))
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop)[0]
    if len(rms) == 0:
        return [1] * len(notes)
    frame_times = np.arange(len(rms), dtype=np.float32) * hop / sr
    note_times = np.fromiter((n.t for n in notes), dtype=np.float32, count=len(notes))
    note_energies = np.interp(note_times, frame_times, rms)
    q33 = float(np.quantile(note_energies, 0.33))
    q67 = float(np.quantile(note_energies, 0.67))
    buckets: list[int] = []
    for e in note_energies:
        if e < q33:
            buckets.append(0)
        elif e > q67:
            buckets.append(2)
        else:
            buckets.append(1)
    return buckets


def load_audio_to_mono(audio_bytes: bytes, target_sr: int = 22050) -> tuple[np.ndarray, int]:
    """Decode arbitrary audio bytes to a mono float32 numpy array at target_sr.

    We import librosa lazily so the rest of the app loads fast and tests that
    don't need audio can run without it being importable.
    """
    import soundfile as sf  # noqa: WPS433
    import librosa  # noqa: WPS433

    with io.BytesIO(audio_bytes) as buf:
        y, sr = sf.read(buf, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != target_sr:
        y = librosa.resample(y, orig_sr=sr, target_sr=target_sr)
        sr = target_sr
    return y.astype(np.float32, copy=False), sr


def build_chart_from_audio(
    *,
    audio_bytes: bytes,
    filename: str,
    difficulty: str,
    audio_source: str = "upload",
    video_id: str | None = None,
) -> Chart:
    """Run the full pipeline on the given audio. Returns Chart.

    Raises NotImplementedError only if librosa is unavailable. The pipeline
    itself is implemented in Stage 2; this entry point ties it together.
    """
    try:
        y, sr = load_audio_to_mono(audio_bytes)
    except Exception as exc:  # pragma: no cover
        raise NotImplementedError(
            f"audio decode failed; install soundfile and librosa. detail: {exc}"
        ) from exc

    duration = float(len(y) / sr)
    content_hash = hashlib.sha256(audio_bytes).hexdigest()[:16]
    logger.info(
        "pipeline start filename=%s hash=%s duration=%.2fs",
        filename,
        content_hash,
        duration,
    )

    beat_info = detect_beats(y=y, sr=sr)
    # Stems are pass-through unless USE_DEMUCS=1 AND demucs is installed.
    # When real, onset detection runs on the drum stem for cleaner rhythm
    # extraction (kicks and snares dominate instead of competing with the
    # full mix). Lane assignment still uses the full mix for spectral
    # centroid so the band split logic stays unchanged.
    stems = separate_stems(y, sr, use_demucs=settings.use_demucs)
    onset_audio = stems.drums if stems.separated else y
    onsets = detect_onsets(y=onset_audio, sr=sr)
    if stems.separated:
        logger.info(
            "stems separated; onset detection ran on drum stem (%d onsets)",
            len(onsets),
        )
    # Beat-snap: onsets within ~22ms of a beat get pulled to the beat
    # exactly. Removes the ~10-30ms perceptual offset that survives even
    # with backtracking on.
    onsets = snap_onsets_to_beats(onsets, beat_info.beats)
    # Beat-grid safety net: fill empty stretches with synthetic onsets so
    # vocal-only choruses don't go dead. Runs on the full-mix beat grid
    # regardless of stem path. See app/pipeline/beat_fill.py.
    onsets_before_fill = len(onsets)
    onsets = fill_empty_beats(onsets, beat_info.beats)
    if len(onsets) != onsets_before_fill:
        logger.info(
            "beat-grid fill added %d synthetic onsets to cover empty runs",
            len(onsets) - onsets_before_fill,
        )
    # Subdivision augmentation: insert half-beat (Hard/Expert) and
    # quarter-beat (Expert in dense sections) candidates plus crescendo
    # subdivisions so the thinner has material to keep at higher tiers.
    onsets_before_subdiv = len(onsets)
    onsets = add_subdivision_onsets(
        onsets, beat_info.beats, y=y, sr=sr, difficulty=difficulty,
    )
    if len(onsets) != onsets_before_subdiv:
        logger.info(
            "subdivision augment added %d candidate onsets for difficulty=%s",
            len(onsets) - onsets_before_subdiv,
            difficulty,
        )
    beat_period_s: float | None = None
    if beat_info.bpm and beat_info.bpm > 0:
        beat_period_s = 60.0 / beat_info.bpm
    tuning = _tuning_for(difficulty)
    raw_notes = assign_lanes(
        onsets=onsets,
        y=y,
        sr=sr,
        beat_period_s=beat_period_s,
        chord_quantile=tuning["chord_quantile"],
    )
    energy_buckets = _energy_buckets_for_notes(notes=raw_notes, y=y, sr=sr)
    thinned = shape_difficulty(
        notes=raw_notes,
        difficulty=difficulty,
        beats=beat_info.beats,
        energy_buckets=energy_buckets,
        song_duration_s=duration,
    )
    notes = detect_holds(
        notes=thinned,
        y=y,
        sr=sr,
        beat_period_s=beat_period_s,
        beats_s=beat_info.beats,
        max_hold_ratio=tuning["hold_ratio"],
    )

    return Chart(
        audio=AudioMeta(
            source=audio_source if audio_source in {"youtube", "upload", "synthetic"} else "upload",
            videoId=video_id,
            contentHash=content_hash,
            duration=duration,
            bpm=beat_info.bpm,
            bpmCurve=beat_info.bpm_curve,
        ),
        metadata=ChartMeta(
            generatedAt=datetime.now(timezone.utc),
            pipelineVersion=PIPELINE_VERSION,
            difficulty=difficulty,  # type: ignore[arg-type]
            keyMode=4,
        ),
        # `strength` lives on RawNote for the difficulty selector but is not
        # part of the wire-format Note; strip it before constructing the
        # Pydantic model (which has extra="forbid").
        notes=[
            Note(t=n.t, lane=n.lane, type=n.type, duration=n.duration)
            for n in notes
        ],
    )
