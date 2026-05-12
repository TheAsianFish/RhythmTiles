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
from app.pipeline.beat_track import detect_beats
from app.pipeline.difficulty import shape_difficulty
from app.pipeline.hold_detect import detect_holds
from app.pipeline.lane_assign import assign_lanes
from app.pipeline.onset_detect import detect_onsets

logger = logging.getLogger("beatbridge.pipeline")


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
    onsets = detect_onsets(y=y, sr=sr)
    beat_period_s: float | None = None
    if beat_info.bpm and beat_info.bpm > 0:
        beat_period_s = 60.0 / beat_info.bpm
    raw_notes = assign_lanes(onsets=onsets, y=y, sr=sr, beat_period_s=beat_period_s)
    thinned = shape_difficulty(notes=raw_notes, difficulty=difficulty, beats=beat_info.beats)
    notes = detect_holds(
        notes=thinned,
        y=y,
        sr=sr,
        beat_period_s=beat_period_s,
        beats_s=beat_info.beats,
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
        notes=[Note(**n.__dict__) for n in notes],
    )
