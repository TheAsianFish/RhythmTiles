"""Glue that wires the pipeline stages into a Chart.

Calls (in order): load_audio_to_mono -> detect_beats -> separate_stems
(optional) -> detect_onsets / detect_onsets_per_stem -> snap_onsets_to_beats
-> fill_empty_beats -> add_subdivision_onsets -> assign_lanes -> MERT
section labelling (optional) -> shape_difficulty -> detect_holds -> Chart.

ML stages (Beat This!, Demucs, MERT) all gate behind env flags or per-call
kwargs and fall back to the librosa heuristic on failure. The chart cache
keys by audio content hash; the per-stage caches (beats, per-stem onsets,
sections) reuse the same hash so re-generating at a different difficulty
is fast.
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
    Section,
)
from app.config import settings
from app.ml import onset_cache
from app.ml import mert, section_cache
from app.ml import sections as ml_sections
from app.pipeline.beat_fill import add_subdivision_onsets, fill_empty_beats
from app.pipeline.beat_track import detect_beats
from app.pipeline.difficulty import shape_difficulty
from app.pipeline.hold_detect import detect_holds
from app.pipeline.lane_assign import assign_lanes
from app.pipeline.onset_detect import (
    detect_onsets,
    detect_onsets_per_stem,
    merge_onset_lists,
    snap_onsets_to_beats,
)
from app.pipeline.stems import separate_stems

logger = logging.getLogger("beatbridge.pipeline")


# Window used to smooth RMS into a "section energy" curve. Roughly the
# length of a musical phrase so individual beats and bars don't dominate.
_ENERGY_WINDOW_S = 4.0

# Per-difficulty tuning. Higher density + lower chord quantile + higher
# hold ratio = more challenge. Expert is meant to feel busy and technical,
# not random; the lane assigner's hand-balance and chord rules still apply.
_DIFFICULTY_TUNING = {
    # chord_quantile = strength quantile above which an onset is a CHORD
    # CANDIDATE. The lane assigner ALSO gates on centroid (must clear
    # CHORD_ACCENT_MIN_CENTROID_HZ) so a loud kick doesn't double-fire.
    # Both gates aim chord stacks at musical accents (cymbal crashes,
    # bright snares, downbeat accents). Target rate: 3-6% of notes in
    # chord groups, tuned together with the centroid threshold. Was
    # 0.98-0.90 (too few chords); these widen the strength gate so the
    # centroid gate becomes the dominant filter.
    # hold_ratio cut roughly in half across all tiers. Sliders were stacking
    # too thickly through sustained vocal regions and felt like a different
    # instrument from the tap stream. Keeping them rare makes each one read
    # as a deliberate musical moment rather than ambient noise.
    # Wider quantile spread between Hard and Expert so Expert visibly has
    # more chord stacks (and feels distinct from Hard), matching the n/s
    # band separation in difficulty.TARGET_NOTES_PER_SEC. Easy/Normal
    # unchanged: their tier separation comes mostly from the n/s band.
    "easy":   {"chord_quantile": 0.97, "hold_ratio": 0.01},
    "normal": {"chord_quantile": 0.94, "hold_ratio": 0.025},
    "hard":   {"chord_quantile": 0.89, "hold_ratio": 0.04},
    "expert": {"chord_quantile": 0.84, "hold_ratio": 0.07},
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


def _energy_buckets_from_sections(
    *,
    notes,
    sections: list[ml_sections.LabeledSection],
) -> list[int]:
    """Bucket each note by which MERT-derived section it sits in.

    Compared to the RMS path: sections give sharp boundaries (chorus
    starts THERE, not "gradually around there"), and MERT clustering
    catches vocal-driven choruses that RMS alone misses on songs with
    quiet drums under loud vocals.
    """
    if not notes:
        return []
    times = [float(n.t) for n in notes]
    return ml_sections.buckets_for_note_times(
        note_times_s=times, sections=sections,
    )


def _compute_mert_sections(
    *,
    y: np.ndarray,
    sr: int,
    content_hash: str,
) -> list[ml_sections.LabeledSection]:
    """Run MERT + section clustering, caching by audio hash.

    Returns [] if MERT isn't available, the model fails, or the audio
    is too short for clustering to be meaningful. Caller falls back to
    RMS bucketing on empty.
    """
    cached = section_cache.get(content_hash=content_hash, source="mert")
    if cached is not None:
        logger.info("MERT section cache hit (%d sections)", len(cached))
        return [
            ml_sections.LabeledSection(
                start_s=c.start_s,
                end_s=c.end_s,
                bucket=c.bucket,
                cluster_id=c.cluster_id,
                intensity=c.intensity,
            )
            for c in cached
        ]
    result = mert.embed(y=y, sr=sr)
    if result is None:
        return []
    labeled = ml_sections.sections_from_embeddings(
        embeddings=result.embeddings,
        frame_rate_hz=result.frame_rate_hz,
        y=y,
        sr=sr,
    )
    if not labeled:
        return []
    section_cache.put(
        content_hash=content_hash,
        source="mert",
        sections=[
            section_cache.CachedSection(
                start_s=s.start_s,
                end_s=s.end_s,
                bucket=s.bucket,
                cluster_id=s.cluster_id,
                intensity=s.intensity,
            )
            for s in labeled
        ],
    )
    logger.info(
        "MERT sections produced %d records spanning %.1fs",
        len(labeled),
        labeled[-1].end_s if labeled else 0.0,
    )
    return labeled


def _section_label(cluster_id: int, bucket: int) -> str:
    """Human-readable label for the Chart.sections wire format.

    We don't actually know if a cluster is the verse or the chorus.
    But the player UX wants something short. Bucket-derived labels
    ("intense"/"steady"/"breakdown") are honest about what we know.
    """
    if bucket == 2:
        return "intense"
    if bucket == 0:
        return "breakdown"
    return "steady"


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
    use_beat_this: bool | None = None,
    use_demucs: bool | None = None,
    use_mert: bool | None = None,
) -> Chart:
    """Run the full pipeline on the given audio. Returns Chart.

    `use_beat_this`, `use_demucs`, and `use_mert` override the env-driven
    defaults for this one call. Pass them when benchmarking, testing, or
    otherwise needing to force the heuristic baseline (or force ML)
    without touching environment variables. None = use settings.

    Raises NotImplementedError only if librosa is unavailable. The pipeline
    itself is implemented in Stage 2; this entry point ties it together.
    """
    use_demucs_eff = settings.use_demucs if use_demucs is None else use_demucs
    use_mert_eff = settings.use_mert if use_mert is None else use_mert
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

    beat_info = detect_beats(
        y=y, sr=sr, content_hash=content_hash, use_beat_this=use_beat_this,
    )
    logger.info(
        "beat tracking source=%s bpm=%.1f beats=%d downbeats=%d",
        beat_info.source,
        beat_info.bpm,
        len(beat_info.beats),
        len(beat_info.downbeats or ()),
    )
    # Stems are pass-through unless USE_DEMUCS=1 AND demucs is installed.
    # When real, run onset detection PER STEM (drums + vocals) so each
    # onset carries the instrument it came from. The lane assigner uses
    # the stem tag to route drums to lanes 0/1 (left hand) and vocals to
    # lanes 2/3 (right hand), which is much cleaner than the full-mix
    # spectral-centroid heuristic on songs where vocals and drums share
    # a frequency band. See Phase 2 of docs/ML_PLAN.md.
    # Per-stem onset detection is the slowest stage when active (Demucs
    # is minutes on CPU). Its output is difficulty-independent, so we
    # cache it by audio content hash and skip Demucs entirely on repeat
    # generations of the same audio at different difficulties.
    cached_stem_onsets: list = []
    if use_demucs_eff:
        cached_stem_onsets = onset_cache.get(
            content_hash=content_hash, source="per-stem",
        ) or []

    if cached_stem_onsets:
        onsets = cached_stem_onsets
        drum_count = sum(1 for o in onsets if o.stem == "drums")
        vocal_count = sum(1 for o in onsets if o.stem == "vocals")
        logger.info(
            "per-stem onset cache hit total=%d drums=%d vocals=%d residual=%d",
            len(onsets), drum_count, vocal_count,
            len(onsets) - drum_count - vocal_count,
        )
    else:
        stems = separate_stems(y, sr, use_demucs=use_demucs_eff)
        if stems.separated:
            onsets = detect_onsets_per_stem(
                drums=stems.drums, vocals=stems.vocals, sr=sr,
            )
            # Merge in a residual pass on the full mix so anything that
            # lives outside the drum/vocal stems (bright synth stabs in
            # "other", bass plucks) still produces notes. The per-stem
            # detections win the dedupe because they're processed first.
            residual = detect_onsets(y=y, sr=sr)
            onsets = merge_onset_lists(onsets, residual)
            drum_count = sum(1 for o in onsets if o.stem == "drums")
            vocal_count = sum(1 for o in onsets if o.stem == "vocals")
            logger.info(
                "per-stem onsets total=%d drums=%d vocals=%d residual=%d",
                len(onsets), drum_count, vocal_count,
                len(onsets) - drum_count - vocal_count,
            )
            # Cache for future regenerations at other difficulties.
            onset_cache.put(
                content_hash=content_hash, source="per-stem", onsets=onsets,
            )
        else:
            onsets = detect_onsets(y=y, sr=sr)
    # Beat-snap: onsets within ~22ms of a beat get pulled to the beat
    # exactly. Removes the ~10-30ms perceptual offset that survives even
    # with backtracking on.
    onsets = snap_onsets_to_beats(onsets, beat_info.beats)
    # Beat-grid safety net: fill empty stretches with synthetic onsets so
    # vocal-only choruses don't go dead. Runs on the full-mix beat grid
    # regardless of stem path. See app/pipeline/beat_fill.py.
    onsets_before_fill = len(onsets)
    onsets = fill_empty_beats(
        onsets, beat_info.beats, downbeats=beat_info.downbeats,
    )
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
        # Downbeats come from Beat This! (Phase 1 ML). When None (librosa
        # fallback), the lane assigner falls back to the strength+centroid
        # accent gate it has always used.
        downbeats=beat_info.downbeats,
    )
    # Energy bucket source: MERT-derived sections when the flag is on
    # AND the model produces a usable result; RMS heuristic otherwise.
    # The thinner's _ENERGY_MULT applies the same way to both - section
    # buckets just give sharper boundaries between verse and chorus.
    mert_sections: list[ml_sections.LabeledSection] = []
    if use_mert_eff and mert.is_available():
        mert_sections = _compute_mert_sections(
            y=y, sr=sr, content_hash=content_hash,
        )
    if mert_sections:
        energy_buckets = _energy_buckets_from_sections(
            notes=raw_notes, sections=mert_sections,
        )
        logger.info(
            "energy buckets sourced from MERT (%d sections)",
            len(mert_sections),
        )
    else:
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

    # AudioMeta.bpmCurve is the wire-format tempo curve. tuple-of-pairs in
    # Pydantic comes through fine, but BeatInfo carries it as a list[tuple]
    # which is exactly the expected shape. None passes through too.
    wire_sections: list[Section] | None = None
    if mert_sections:
        intensities = [s.intensity for s in mert_sections]
        max_i = max(intensities) or 1.0
        wire_sections = [
            Section(
                start=round(s.start_s, 3),
                end=round(s.end_s, 3),
                intensity=round(min(1.0, s.intensity / max_i), 3),
                label=_section_label(s.cluster_id, s.bucket),
            )
            for s in mert_sections
        ]
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
        sections=wire_sections,
    )
