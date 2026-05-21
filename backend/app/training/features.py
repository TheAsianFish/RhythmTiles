"""Per-event feature extraction for the learned-lane training corpus.

Given an audio file and a list of (time, lane) events from a .osu chart,
produce one feature row per event. The row schema is the contract
between training and inference; the inference path (app.ml.learned_lanes)
must produce IDENTICAL columns in the same order.

Critical architectural choice (see docs/PHASE5_PLAN.md): we compute
features AT the chart's own event times during training. We do NOT run
our onset detector during training. This eliminates the
detected-onset-vs-chart-event alignment problem entirely. At inference
the SAME feature schema is computed at our detector's onset times.

Heavy stages (Beat This!, Demucs, MERT) run once per audio file and
their frame-level outputs are sampled at each event time. This means
the per-event cost is microseconds; the per-audio cost is dominated by
Demucs (minutes on CPU). The collector caches per-audio outputs to
parquet so re-extraction is a no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    import numpy as np
    from app.ml.sections import LabeledSection
    from app.pipeline.beat_track import BeatInfo
    from app.pipeline.stems import Stems
    from app.training.osu_parse import OsuHitEvent

logger = logging.getLogger("beatbridge.training.features")

# Feature schema. The order MUST be stable; both training and inference
# use this list. Adding a feature is a breaking change for any trained
# model; bump FEATURE_SCHEMA_VERSION when you do.
#
# 1.0: initial release. 26 features (15 audio + 11 chart context).
# 2.0: added flow-aware features for v0.3 after v0.2 felt stream-spammy /
#      lane-biased. Added pitch_delta_bins (melodic contour),
#      dominant_stem_* (which instrument leads at this moment),
#      phrase_position (where we are in the section), and
#      onset_strength_norm (loudness relative to song's peak). The
#      hypothesis: these features carry "musical flow" signal the v1.0
#      schema couldn't represent.
FEATURE_SCHEMA_VERSION = "2.0"

# Audio-side features (computed at the event time from frame-level arrays).
AUDIO_FEATURE_COLUMNS = (
    "spectral_centroid_hz",
    "spectral_flux",
    "rms_full_mix",
    "rms_drums",
    "rms_vocals",
    "rms_bass",
    "rms_other",
    "chroma_max_bin",
    "chroma_strength",
    "mfcc_1",
    "mfcc_2",
    "mfcc_3",
    "mfcc_4",
    "mfcc_5",
    "mert_section_bucket",
    "onset_strength_norm",   # v2.0: spectral flux at this onset divided by song-wide 95th percentile
    # v2.0: one-hot of which stem is loudest at this onset. Computed from
    # the four rms_* values above; lives in audio side because it's
    # purely a function of the audio at the event time.
    "dominant_stem_drums",
    "dominant_stem_vocals",
    "dominant_stem_bass",
    "dominant_stem_other",
)

# Chart-context features (need the event sequence; computed on the fly).
CHART_CONTEXT_COLUMNS = (
    "delta_t_prev",
    "delta_t_next",
    "local_density_500ms",
    "beat_phase",
    "bar_phase",
    "is_on_downbeat",
    "prev_lane_0",   # one-hot of immediately previous lane (one of these is 1)
    "prev_lane_1",
    "prev_lane_2",
    "prev_lane_3",
    "prev_lane_none",  # first event has no previous lane
    # v2.0: flow-aware additions in the context block.
    "pitch_delta_bins",         # signed chroma distance from prev event in semitones (-6..+6)
    "phrase_position",          # 0-1 normalized position within current MERT section
)

# Label columns (training only; absent at inference).
LABEL_COLUMNS = ("lane", "type", "duration_s")

ALL_FEATURE_COLUMNS = AUDIO_FEATURE_COLUMNS + CHART_CONTEXT_COLUMNS


@dataclass
class FeatureRow:
    """One row of the training corpus. Values are plain floats / ints."""

    audio: dict  # AUDIO_FEATURE_COLUMNS -> float
    context: dict  # CHART_CONTEXT_COLUMNS -> float/int
    labels: dict | None  # LABEL_COLUMNS -> value (None at inference)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_features_for_beatmap(
    *,
    audio_path: Path,
    events: list["OsuHitEvent"],
    use_demucs: bool = True,
    use_beat_this: bool = True,
    use_mert: bool = True,
) -> list[FeatureRow]:
    """One-shot: load audio, run heavy ML stages, sample features at event times.

    Used by the training collector. The serving inference path uses
    `extract_features_at_times` directly with a pre-loaded audio + stems
    so it can share the cache with the live chart pipeline.
    """
    from app.pipeline.chart_builder import load_audio_to_mono  # local import: keep training optional

    audio_bytes = audio_path.read_bytes()
    y, sr = load_audio_to_mono(audio_bytes)

    return extract_features_at_times(
        y=y,
        sr=sr,
        times=[e.t_s for e in events],
        events_for_labels=events,
        use_demucs=use_demucs,
        use_beat_this=use_beat_this,
        use_mert=use_mert,
    )


def extract_features_at_times(
    *,
    y: "np.ndarray",
    sr: int,
    times: list[float],
    events_for_labels: list["OsuHitEvent"] | None = None,
    use_demucs: bool = True,
    use_beat_this: bool = True,
    use_mert: bool = True,
) -> list[FeatureRow]:
    """Compute one FeatureRow per time in `times`.

    `events_for_labels`, if given, supplies labels (lane / type / duration)
    aligned positionally with `times`. Pass None at inference. Pass the
    full event list at training; we copy `lane`, `type`, `duration_s`
    onto each row.

    Heavy stages are env-flag-controllable here (overriding `settings`)
    so the training collector can force features ON regardless of the
    serving environment.
    """
    if not times:
        return []

    audio_frames = _compute_audio_frames(y=y, sr=sr)
    beat_info = _compute_beat_info(y=y, sr=sr, use_beat_this=use_beat_this)
    stems = _compute_stems(y=y, sr=sr, use_demucs=use_demucs)
    mert_sections = _compute_mert(y=y, sr=sr, use_mert=use_mert)

    # Song-wide onset-strength normaliser. 95th percentile, not max, so a
    # single outlier (a glitch or a cymbal smash) doesn't squash everything
    # else into the 0.0-0.2 range.
    import numpy as np  # noqa: WPS433

    if len(audio_frames.flux) > 0:
        flux_p95 = float(np.percentile(audio_frames.flux, 95))
        if flux_p95 <= 1e-6:
            flux_p95 = 1.0
    else:
        flux_p95 = 1.0

    # Chart-context features need the prior-event lane history. At inference
    # we use predicted lanes from earlier in the sequence (autoregressive)
    # so the schema only needs the IMMEDIATELY previous lane to be defined.
    rows: list[FeatureRow] = []
    prev_lane: int | None = None
    prev_chroma_bin: int | None = None
    for i, t in enumerate(times):
        audio_feats = _sample_audio_features(
            t=t,
            sr=sr,
            frames=audio_frames,
            stems=stems,
            mert_sections=mert_sections,
            flux_p95=flux_p95,
        )
        prev_t = times[i - 1] if i > 0 else None
        next_t = times[i + 1] if i + 1 < len(times) else None
        ctx_feats = _sample_context_features(
            t=t,
            prev_t=prev_t,
            next_t=next_t,
            prev_lane=prev_lane,
            prev_chroma_bin=prev_chroma_bin,
            times=times,
            i=i,
            beat_info=beat_info,
            mert_sections=mert_sections,
            audio_frames=audio_frames,
        )
        labels: dict | None = None
        if events_for_labels is not None:
            evt = events_for_labels[i]
            labels = {
                "lane": int(evt.lane),
                "type": str(evt.type),
                "duration_s": float(evt.duration_s),
            }
            prev_lane = int(evt.lane)
        # Update chroma history for next iteration's pitch_delta. We track
        # ACTUAL chroma at the current event time, not the model's prediction.
        prev_chroma_bin = int(audio_feats["chroma_max_bin"])
        rows.append(FeatureRow(audio=audio_feats, context=ctx_feats, labels=labels))
    return rows


# ---------------------------------------------------------------------------
# Heavy-stage drivers (one call per audio file)
# ---------------------------------------------------------------------------


@dataclass
class _AudioFrames:
    """Frame-level numpy arrays. All share the same hop length / fps."""

    fps: float
    centroid: "np.ndarray"  # (n_frames,)
    flux: "np.ndarray"
    rms: "np.ndarray"
    chroma: "np.ndarray"   # (12, n_frames)
    mfcc: "np.ndarray"     # (5, n_frames)


def _compute_audio_frames(*, y: "np.ndarray", sr: int) -> _AudioFrames:
    """Compute the frame-level audio descriptors. ~1-2s on CPU for a 4-min song."""
    import librosa  # noqa: WPS433
    import numpy as np  # noqa: WPS433

    hop_length = 512
    fps = sr / hop_length
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop_length)[0]
    flux = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=hop_length)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop_length, n_mfcc=5)
    return _AudioFrames(
        fps=float(fps),
        centroid=centroid.astype(np.float32),
        flux=flux.astype(np.float32),
        rms=rms.astype(np.float32),
        chroma=chroma.astype(np.float32),
        mfcc=mfcc.astype(np.float32),
    )


def _compute_beat_info(*, y: "np.ndarray", sr: int, use_beat_this: bool):
    """Run beat tracking. Returns BeatInfo-like with .beats, .downbeats, .bpm.

    The content_hash is derived from the audio buffer so the per-song beat
    cache works correctly. Passing a constant placeholder (early bug from
    2026-05-18) made every song after the first reuse the first song's
    beat grid via cache hit, poisoning every beat-derived feature.

    Beat This! runs on CPU during training (~2-5s per song) so we don't
    contend with Demucs for the 8GB VRAM on a laptop 4070. The 4070's
    VRAM fits Demucs comfortably but not Demucs + Beat This! + MERT
    simultaneously. Tiny inference like Beat This! gets little benefit
    from GPU anyway.
    """
    import hashlib  # noqa: WPS433
    import os  # noqa: WPS433
    from app.pipeline.beat_track import detect_beats  # noqa: WPS433
    # SHA-256 of the audio bytes; short slice is plenty for cache-key purposes.
    content_hash = hashlib.sha256(y.tobytes()).hexdigest()[:16]
    # Honour env override so a desktop with a 24GB GPU can keep Beat This! on GPU.
    device = os.environ.get("BEAT_THIS_DEVICE", "cpu") if use_beat_this else None
    return detect_beats(
        y=y, sr=sr, content_hash=content_hash, use_beat_this=use_beat_this,
        device=device,
    )


def _compute_stems(*, y: "np.ndarray", sr: int, use_demucs: bool) -> "Stems":
    from app.pipeline.stems import separate_stems  # noqa: WPS433
    stems = separate_stems(y, sr, use_demucs=use_demucs)
    # Demucs allocates substantial VRAM per song. Explicitly release the
    # cache between songs so a long training run doesn't slowly accumulate
    # allocator fragments and OOM on song N. No-op when torch isn't
    # importable or no CUDA device is visible.
    if use_demucs:
        try:
            import torch  # noqa: WPS433
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
    return stems


def _compute_mert(*, y: "np.ndarray", sr: int, use_mert: bool) -> list:
    """MERT runs on CPU during training. Same reasoning as Beat This!:
    sharing the 8GB on a laptop 4070 with Demucs causes OOM. MERT on
    CPU is ~1-3s per song which is acceptable."""
    if not use_mert:
        return []
    import os  # noqa: WPS433
    from app.ml import mert  # noqa: WPS433
    from app.ml import sections as ml_sections  # noqa: WPS433
    if not mert.is_available():
        return []
    device = os.environ.get("MERT_DEVICE", "cpu")
    result = mert.embed(y=y, sr=sr, device=device)
    if result is None:
        return []
    return ml_sections.sections_from_embeddings(
        embeddings=result.embeddings,
        frame_rate_hz=result.frame_rate_hz,
        y=y,
        sr=sr,
    )


# ---------------------------------------------------------------------------
# Per-event sampling
# ---------------------------------------------------------------------------


def _sample_audio_features(
    *,
    t: float,
    sr: int,
    frames: _AudioFrames,
    stems: "Stems",
    mert_sections: list,
    flux_p95: float = 1.0,
) -> dict:
    """Look up the frame at time t for each frame-level feature."""
    import numpy as np  # noqa: WPS433

    frame_idx = int(t * frames.fps)
    n = len(frames.centroid)
    frame_idx = max(0, min(n - 1, frame_idx))

    chroma_col = frames.chroma[:, frame_idx]
    chroma_max_bin = int(np.argmax(chroma_col))
    chroma_strength = float(chroma_col[chroma_max_bin])

    # Per-stem RMS at this time. When Demucs didn't run, all four stems
    # are the same pass-through mix; we still emit four columns so the
    # schema is fixed-shape. Inference can detect "stems unavailable"
    # downstream by checking equality, but the schema doesn't change.
    rms_drums = _rms_at_time(stems.drums, sr=sr, t=t)
    rms_vocals = _rms_at_time(stems.vocals, sr=sr, t=t)
    rms_bass = _rms_at_time(stems.bass, sr=sr, t=t)
    rms_other = _rms_at_time(stems.other, sr=sr, t=t)

    section_bucket = _mert_bucket_at(t=t, sections=mert_sections)

    # v2.0: onset_strength_norm. Spectral flux at this frame divided by
    # the song's 95th-percentile flux. >= 1.0 means this is a song-peak
    # onset; ~0.2 means it's a quiet detected event. Lets the model
    # learn "loud accents tend to go in <these> lanes" without having
    # to memorise per-song amplitude scales.
    raw_flux = float(frames.flux[frame_idx])
    onset_strength_norm = raw_flux / max(flux_p95, 1e-6)

    # v2.0: dominant_stem one-hot. Which of the four stems is loudest
    # at this onset. On songs with real Demucs separation this is a
    # strong "drum hit vs vocal note" signal; on pass-through stems
    # (Demucs off) all four are tied and the one-hot picks arbitrarily,
    # which is fine because the column is constant across songs and the
    # model learns to ignore it.
    stem_rms = (rms_drums, rms_vocals, rms_bass, rms_other)
    dom_idx = int(np.argmax(stem_rms))

    return {
        "spectral_centroid_hz": float(frames.centroid[frame_idx]),
        "spectral_flux": raw_flux,
        "rms_full_mix": float(frames.rms[frame_idx]),
        "rms_drums": rms_drums,
        "rms_vocals": rms_vocals,
        "rms_bass": rms_bass,
        "rms_other": rms_other,
        "chroma_max_bin": float(chroma_max_bin),
        "chroma_strength": chroma_strength,
        "mfcc_1": float(frames.mfcc[0, frame_idx]),
        "mfcc_2": float(frames.mfcc[1, frame_idx]),
        "mfcc_3": float(frames.mfcc[2, frame_idx]),
        "mfcc_4": float(frames.mfcc[3, frame_idx]),
        "mfcc_5": float(frames.mfcc[4, frame_idx]),
        "mert_section_bucket": float(section_bucket),
        "onset_strength_norm": float(onset_strength_norm),
        "dominant_stem_drums": 1.0 if dom_idx == 0 else 0.0,
        "dominant_stem_vocals": 1.0 if dom_idx == 1 else 0.0,
        "dominant_stem_bass": 1.0 if dom_idx == 2 else 0.0,
        "dominant_stem_other": 1.0 if dom_idx == 3 else 0.0,
    }


def _sample_context_features(
    *,
    t: float,
    prev_t: float | None,
    next_t: float | None,
    prev_lane: int | None,
    prev_chroma_bin: int | None,
    times: list[float],
    i: int,
    beat_info,
    mert_sections: list,
    audio_frames: "_AudioFrames",
) -> dict:
    """Features that need event-sequence context, not audio-only context."""
    import numpy as np  # noqa: WPS433

    delta_t_prev = float(t - prev_t) if prev_t is not None else 0.0
    delta_t_next = float(next_t - t) if next_t is not None else 0.0
    local_density = _local_density(times=times, i=i, window_s=0.5)
    beat_phase, bar_phase, is_on_downbeat = _beat_position(
        t=t, beat_info=beat_info,
    )
    prev_one_hot = _one_hot_prev_lane(prev_lane)

    # v2.0: pitch_delta_bins. Signed semitone distance from the previous
    # event's chroma_max_bin to the current's. Chroma is circular (12
    # pitch classes), so we take the SHORTEST signed path (range -6..+6).
    # Carries melodic contour: positive = pitch going up, negative = down,
    # 0 = same pitch class. First event has no prev, default 0.
    frame_idx = max(0, min(len(audio_frames.centroid) - 1, int(t * audio_frames.fps)))
    cur_chroma_bin = int(np.argmax(audio_frames.chroma[:, frame_idx]))
    pitch_delta = 0
    if prev_chroma_bin is not None:
        raw_delta = cur_chroma_bin - prev_chroma_bin
        # Map to shortest signed path on the circular 12-pitch-class wheel.
        if raw_delta > 6:
            raw_delta -= 12
        elif raw_delta < -6:
            raw_delta += 12
        pitch_delta = raw_delta

    # v2.0: phrase_position. Where in the current MERT-detected section
    # are we, normalized 0-1. Captures "are we at the start of a phrase
    # (often a downbeat-y moment) or at the end (often a fill / transition)".
    phrase_position = _phrase_position_at(t=t, sections=mert_sections)

    return {
        "delta_t_prev": delta_t_prev,
        "delta_t_next": delta_t_next,
        "local_density_500ms": float(local_density),
        "beat_phase": float(beat_phase),
        "bar_phase": float(bar_phase),
        "is_on_downbeat": float(is_on_downbeat),
        **prev_one_hot,
        "pitch_delta_bins": float(pitch_delta),
        "phrase_position": float(phrase_position),
        # dominant_stem_* are sampled from the audio side and re-emitted
        # here so all chart-context columns are in one place at the row
        # level. The audio-side sampler also has them; we copy here so
        # the schema position is correct. (Both functions are merged in
        # write_parquet via the AUDIO + CONTEXT column ordering.)
    }


def _phrase_position_at(*, t: float, sections: list) -> float:
    """Return position 0-1 within the section containing t.

    Returns 0.5 (mid-phrase) when sections aren't available or t is
    outside all sections. This is a soft default that doesn't push the
    model in either direction at the chart's boundaries.
    """
    if not sections:
        return 0.5
    for s in sections:
        if s.start_s <= t < s.end_s:
            span = s.end_s - s.start_s
            if span <= 0:
                return 0.5
            return max(0.0, min(0.999, (t - s.start_s) / span))
    return 0.5


def _rms_at_time(stem: "np.ndarray", *, sr: int, t: float) -> float:
    """Mean absolute value over a ~20ms window centred at t.

    Cheaper than computing the full RMS curve up-front for every stem;
    we're sampling at sparse points only. ~1ms per call.
    """
    import numpy as np  # noqa: WPS433

    half_window = int(0.010 * sr)  # 10ms each side
    centre = int(t * sr)
    lo = max(0, centre - half_window)
    hi = min(len(stem), centre + half_window)
    if hi <= lo:
        return 0.0
    seg = stem[lo:hi]
    return float(np.sqrt(np.mean(seg.astype(np.float32) ** 2)))


def _mert_bucket_at(*, t: float, sections: list) -> int:
    """Return the bucket (0=low, 1=mid, 2=high) of the section containing t."""
    if not sections:
        return 1
    for s in sections:
        if s.start_s <= t < s.end_s:
            return int(s.bucket)
    # After the last section's end_s, fall through to its bucket.
    return int(sections[-1].bucket)


def _local_density(*, times: list[float], i: int, window_s: float) -> int:
    """Count events in [t-window_s, t+window_s] including the current event."""
    t = times[i]
    lo_t = t - window_s
    hi_t = t + window_s
    # Linear scan outward; sparse enough that bisect isn't worth it here.
    count = 1
    j = i - 1
    while j >= 0 and times[j] >= lo_t:
        count += 1
        j -= 1
    j = i + 1
    while j < len(times) and times[j] <= hi_t:
        count += 1
        j += 1
    return count


def _beat_position(*, t: float, beat_info) -> tuple[float, float, int]:
    """Return (beat_phase, bar_phase, is_on_downbeat).

    beat_phase = (t - prev_beat) / beat_period, in [0, 1).
    bar_phase  = (t - prev_downbeat) / bar_period, in [0, 1) if we have
                 downbeats; 0 otherwise.
    is_on_downbeat = 1 iff t is within 50ms of a downbeat.
    """
    beats = beat_info.beats
    if not beats or len(beats) < 2:
        return 0.0, 0.0, 0

    # Locate the beat just before t.
    import bisect  # noqa: WPS433

    pos = bisect.bisect_right(beats, t) - 1
    pos = max(0, min(len(beats) - 2, pos))
    beat_period = beats[pos + 1] - beats[pos]
    beat_phase = (t - beats[pos]) / beat_period if beat_period > 0 else 0.0
    beat_phase = max(0.0, min(0.999, beat_phase))

    downbeats = getattr(beat_info, "downbeats", None) or []
    bar_phase = 0.0
    is_on_downbeat = 0
    if downbeats:
        db_pos = bisect.bisect_right(downbeats, t) - 1
        if 0 <= db_pos < len(downbeats) - 1:
            bar_period = downbeats[db_pos + 1] - downbeats[db_pos]
            if bar_period > 0:
                bar_phase = (t - downbeats[db_pos]) / bar_period
                bar_phase = max(0.0, min(0.999, bar_phase))
        # On-downbeat within 50ms tolerance.
        if db_pos >= 0:
            if abs(t - downbeats[db_pos]) <= 0.05:
                is_on_downbeat = 1
            elif db_pos + 1 < len(downbeats) and abs(t - downbeats[db_pos + 1]) <= 0.05:
                is_on_downbeat = 1
    return float(beat_phase), float(bar_phase), int(is_on_downbeat)


def _one_hot_prev_lane(prev_lane: int | None) -> dict:
    out = {
        "prev_lane_0": 0.0,
        "prev_lane_1": 0.0,
        "prev_lane_2": 0.0,
        "prev_lane_3": 0.0,
        "prev_lane_none": 0.0,
    }
    if prev_lane is None:
        out["prev_lane_none"] = 1.0
    else:
        key = f"prev_lane_{int(prev_lane)}"
        if key in out:
            out[key] = 1.0
    return out


# ---------------------------------------------------------------------------
# Parquet I/O (optional dep)
# ---------------------------------------------------------------------------


def write_parquet(rows: Iterable[FeatureRow], path: Path) -> None:
    """Save rows to parquet. Requires pyarrow; raises ImportError otherwise.

    Schema is flattened: audio + context + labels columns side by side.
    Labels columns are present iff every row carries labels (i.e. this is
    a training shard, not an inference batch).
    """
    try:
        import pyarrow as pa  # noqa: WPS433
        import pyarrow.parquet as pq  # noqa: WPS433
    except ImportError as exc:
        raise ImportError(
            "writing parquet requires pyarrow; install with `pip install pyarrow`",
        ) from exc

    rows = list(rows)
    if not rows:
        return

    has_labels = all(r.labels is not None for r in rows)

    cols: dict[str, list] = {c: [] for c in ALL_FEATURE_COLUMNS}
    if has_labels:
        cols["lane"] = []
        cols["type"] = []
        cols["duration_s"] = []

    for r in rows:
        for c in AUDIO_FEATURE_COLUMNS:
            cols[c].append(r.audio[c])
        for c in CHART_CONTEXT_COLUMNS:
            cols[c].append(r.context[c])
        if has_labels:
            cols["lane"].append(r.labels["lane"])    # type: ignore[index]
            cols["type"].append(r.labels["type"])    # type: ignore[index]
            cols["duration_s"].append(r.labels["duration_s"])  # type: ignore[index]

    table = pa.table(cols)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
