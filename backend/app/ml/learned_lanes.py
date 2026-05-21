"""Learned lane assigner (Phase 5 of docs/ML_PLAN.md, full plan in PHASE5_PLAN.md).

Loads a LightGBM model trained on osu!mania 4K ranked charts and uses it
to pick the lane for each detected onset. Falls back to the rule-based
assigner on:
  - `USE_LEARNED_LANES=0` (default)
  - Model artifact not at `backend/models/lane_v1/`
  - LightGBM not importable
  - Inference error
  - Feature schema version mismatch

Inference flow (autoregressive):
    prev_lane = None
    for onset in onsets:
        features = extract_features_at(onset.t, prev_lane=prev_lane, ...)
        logits = model.predict(features)
        lane = argmax of logits MASKED by anti-cluster constraint
               (don't pick a lane busy within HIT_WINDOW_S)
        prev_lane = lane

Chord emission is NOT done by the model in v1. Same-t chord pairs come
from the existing accent-detection path in `lane_assign.py`; this module
only resolves single-lane decisions. Chord support is a v2 feature.

The module-level model handle is cached and protected by a lock; loads
on first request, stays for the process lifetime.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from app.pipeline.beat_track import BeatInfo
    from app.pipeline.lane_assign import RawNote
    from app.pipeline.onset_detect import Onset
    from app.pipeline.stems import Stems

logger = logging.getLogger("beatbridge.ml.learned_lanes")

# Default model artifact location. Match docs/PHASE5_PLAN.md.
DEFAULT_MODEL_DIR = Path("backend/models/lane_v1")

# Anti-cluster: even if the model picks a lane, don't place two notes
# in the same lane within this many seconds. Matches lane_assign.HIT_WINDOW_S.
HIT_WINDOW_S = 0.080

# How many recently-predicted lanes we keep for the distribution correction.
# 16 = roughly one phrase at typical pop tempo. Larger = stabler correction
# but reacts slowly; smaller = punchier but noisy.
_RECENT_WINDOW = 16

# Lane index -> hand (0=left D/F, 1=right J/K). Mirrors lane_assign.py.
_HAND_OF_LANE = (0, 0, 1, 1)

# Forbid extending a same-hand streak past this length when the model
# would otherwise add another. Matches the rule-based assigner's
# MAX_SAME_HAND_STREAK so the learned path has the same playability
# floor as the heuristic path.
_MAX_SAME_HAND_STREAK = 2

# Scale on the lane-distribution penalty subtracted from over-represented
# lanes' logits. LightGBM softmax logits at our scale are typically in
# [-2, 2]; a penalty around 1.5 is firm but doesn't completely override
# the model when its confidence is genuinely high.
_DISTRIBUTION_PENALTY_SCALE = 1.5

# Target lane distribution. Uniform is the right target for 4K mania
# (human mappers average ~25% per lane across full songs).
_TARGET_LANE_DIST = (0.25, 0.25, 0.25, 0.25)


@dataclass
class _LoadedModel:
    """Cached LightGBM Booster + schema metadata."""

    booster: object  # lightgbm.Booster
    feature_columns: tuple[str, ...]
    schema_version: str
    num_classes: int


_MODEL: _LoadedModel | None = None
_MODEL_LOCK = threading.Lock()
_LOAD_FAILED: bool = False  # latched after the first unrecoverable failure


def is_available() -> bool:
    """True iff a learned-lane model can be loaded for the active architecture.

    Cheap on the second call (cached). When LEARNED_LANES_ARCH=transformer,
    we check the transformer artifact. Otherwise (default), we check the
    LightGBM artifact.
    """
    arch = _resolve_architecture()
    if arch == "transformer":
        # Transformer is the primary; v1 LightGBM is optional fallback.
        from app.ml import lane_transformer_inference  # noqa: WPS433
        return lane_transformer_inference.is_available()
    # LightGBM path (default)
    if _LOAD_FAILED:
        return False
    if _MODEL is not None:
        return True
    return _load_model() is not None


def reset_for_tests() -> None:
    """Clear cached state. Tests only; not called from production code."""
    global _MODEL, _LOAD_FAILED
    with _MODEL_LOCK:
        _MODEL = None
        _LOAD_FAILED = False


def assign_lanes_learned(
    *,
    onsets: list["Onset"],
    y: "np.ndarray",
    sr: int,
    beat_info: "BeatInfo",
    stems: "Stems",
    mert_sections: list,
) -> list["RawNote"] | None:
    """Predict a lane per onset using the trained model.

    Returns None when the model is unavailable so the caller can fall
    back to the rule-based assigner. Returns [] only when `onsets` is
    empty (a degenerate but valid input).

    Anti-cluster constraint is enforced after model prediction: if the
    argmax lane has been hit within HIT_WINDOW_S, the next-best lane is
    picked instead. If all four lanes are busy the onset is dropped
    (same policy as the rule-based path).
    """
    from app.pipeline.lane_assign import RawNote  # noqa: WPS433

    if not onsets:
        return []

    arch = _resolve_architecture()
    # For arch=transformer we don't strictly need the v1 LightGBM model
    # loaded — the transformer can run on its own. We do still try to
    # load it because (a) it serves as fallback if transformer fails
    # mid-inference, and (b) the schema-version check below uses the
    # v1 metadata to validate runtime feature schema.
    model = _load_model()
    if model is None and arch != "transformer":
        return None

    try:
        from app.training.features import (
            ALL_FEATURE_COLUMNS,
            extract_features_at_times,
        )
    except ImportError as exc:
        # Training package isn't installed; can't extract features.
        logger.warning("training package missing, cannot extract features: %s", exc)
        return None

    if model is not None and tuple(model.feature_columns) != ALL_FEATURE_COLUMNS:
        logger.error(
            "feature schema mismatch: model expects %d cols, current schema has %d. "
            "Retrain or use a model trained on the current schema.",
            len(model.feature_columns),
            len(ALL_FEATURE_COLUMNS),
        )
        return None

    # We feed features in autoregressively: each row's prev-lane one-hot
    # depends on the previous row's predicted lane. We pre-extract the
    # heavy per-audio stuff once, then loop event-by-event.
    times = [float(o.t) for o in onsets]

    try:
        # First pass: features WITHOUT prev_lane context (we'll override
        # the one-hot per row in the loop below). We pass events_for_labels
        # = None so the rows come back without label columns.
        rows = extract_features_at_times(
            y=y,
            sr=sr,
            times=times,
            events_for_labels=None,
            use_demucs=False,  # caller already separated; we reuse via stems below
            use_beat_this=False,  # caller already detected; reuse beat_info
            use_mert=False,  # caller already computed; reuse mert_sections
        )
        # Patch in the audio side that the caller already computed (avoid
        # re-running Demucs / Beat This! / MERT inside this module).
        _hydrate_with_caller_outputs(
            rows=rows,
            times=times,
            sr=sr,
            stems=stems,
            beat_info=beat_info,
            mert_sections=mert_sections,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("feature extraction failed, falling back: %s", exc)
        return None

    # Now predict. Two paths:
    #   - v1 (LightGBM, default): per-event prediction with autoregressive
    #     prev_lane patching.
    #   - v2 (Transformer, LEARNED_LANES_ARCH=transformer): pre-compute a
    #     whole-song logit matrix in one batched pass, then loop and pick
    #     lanes with the same postprocessor.
    import numpy as np  # noqa: WPS433

    arch = _resolve_architecture()
    transformer_logits = None
    if arch == "transformer":
        from app.ml import lane_transformer_inference  # noqa: WPS433

        if lane_transformer_inference.is_available():
            # For Transformer inference we feed a single feature matrix.
            # prev_lane history is implicit in the sequence — the model
            # sees the surrounding context and learns the prev-lane
            # correlations itself, so we don't need to patch one-hot
            # prev-lane features (and the v2 schema stores zeros there
            # to avoid confusing it).
            feature_matrix = np.array(
                [
                    [_flatten_value(row, col) for col in ALL_FEATURE_COLUMNS]
                    for row in rows
                ],
                dtype=np.float32,
            )
            transformer_logits = lane_transformer_inference.predict_per_event_logits(
                feature_matrix,
            )
            if transformer_logits is None:
                logger.warning(
                    "transformer inference returned None; falling back to v1 LightGBM",
                )

    notes: list[RawNote] = []
    last_hit = [-1e9, -1e9, -1e9, -1e9]
    prev_lane: int | None = None
    # Rolling window of the last N predicted lanes, used for the lane-
    # distribution correction. Larger window = slower adaptation but
    # more stable; smaller = punchy correction but noisy. 16 = about
    # 1-2 measures at typical pop tempos.
    recent_lanes: list[int] = []
    booster = model.booster if model is not None else None  # type: ignore[assignment]
    for event_idx, (row, onset) in enumerate(zip(rows, onsets)):
        _patch_prev_lane(row, prev_lane)
        if transformer_logits is not None:
            # Transformer path: pre-computed; just look up.
            raw_logits = transformer_logits[event_idx]
        elif booster is not None:
            # v1 LightGBM path: per-event predict.
            feature_vec = np.array(
                [
                    _flatten_value(row, col)
                    for col in ALL_FEATURE_COLUMNS
                ],
                dtype=np.float32,
            ).reshape(1, -1)
            try:
                raw_logits = booster.predict(feature_vec)[0]  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.exception("inference failed on onset t=%.3f: %s", onset.t, exc)
                return None
        else:
            # Neither path available; bail and let the caller fall back to rules.
            return None
        # Postprocessor 1: penalize lanes that have been over-represented
        # in the recent window. This directly fights the F/J bias the
        # raw model exhibits on Western pop music.
        adjusted_logits = _apply_distribution_correction(
            logits=raw_logits, recent_lanes=recent_lanes,
        )
        # Postprocessor 2: pick the best-scoring lane that also satisfies
        # anti-cluster AND hand-balance constraints.
        lane = _pick_lane_with_constraints(
            logits=adjusted_logits,
            last_hit=last_hit,
            onset_t=float(onset.t),
            recent_lanes=recent_lanes,
        )
        if lane is None:
            # All four lanes too recent; drop (matches rule-based behaviour).
            continue
        notes.append(
            RawNote(
                t=round(float(onset.t), 4),
                lane=int(lane),
                type="tap",
                strength=float(onset.strength),
            ),
        )
        last_hit[lane] = float(onset.t)
        recent_lanes.append(int(lane))
        if len(recent_lanes) > _RECENT_WINDOW:
            recent_lanes.pop(0)
        prev_lane = int(lane)
    return notes


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_model() -> _LoadedModel | None:
    """Construct (or return cached) loaded model. None on any failure."""
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None:
        return _MODEL
    if _LOAD_FAILED:
        return None
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        if _LOAD_FAILED:
            return None

        model_dir = _resolve_model_dir()
        if not (model_dir / "model.lgb").exists():
            logger.info(
                "no model at %s; learned lanes unavailable. Train with "
                "`python -m app.training.train_lane train`.",
                model_dir,
            )
            _LOAD_FAILED = True
            return None

        try:
            import lightgbm as lgb  # noqa: WPS433
        except ImportError as exc:
            logger.warning(
                "lightgbm not importable (%s); USE_LEARNED_LANES will fall back",
                exc,
            )
            _LOAD_FAILED = True
            return None

        try:
            booster = lgb.Booster(model_file=str(model_dir / "model.lgb"))
            schema = json.loads(
                (model_dir / "feature_schema.json").read_text(encoding="utf-8"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("model load failed: %s", exc)
            _LOAD_FAILED = True
            return None

        _MODEL = _LoadedModel(
            booster=booster,
            feature_columns=tuple(schema["columns"]),
            schema_version=str(schema.get("version", "unknown")),
            num_classes=int(schema.get("num_classes", 4)),
        )
        logger.info(
            "learned-lane model loaded: schema=%s features=%d",
            _MODEL.schema_version,
            len(_MODEL.feature_columns),
        )
        return _MODEL


def _resolve_architecture() -> str:
    """Return 'transformer' or 'lightgbm' based on LEARNED_LANES_ARCH env."""
    import os  # noqa: WPS433
    arch = os.environ.get("LEARNED_LANES_ARCH", "lightgbm").strip().lower()
    if arch in ("transformer", "v2"):
        return "transformer"
    return "lightgbm"


def _resolve_model_dir() -> Path:
    """Honour LEARNED_LANES_MODEL_DIR env override; else use DEFAULT_MODEL_DIR."""
    import os  # noqa: WPS433
    override = os.environ.get("LEARNED_LANES_MODEL_DIR")
    if override:
        return Path(override).resolve()
    # The default is relative to the repo root, not the cwd. Resolve via
    # this file's location: backend/app/ml/learned_lanes.py -> repo root.
    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent.parent  # ml -> app -> backend -> repo
    return repo_root / "backend" / "models" / "lane_v1"


def _hydrate_with_caller_outputs(
    *,
    rows: list,
    times: list[float],
    sr: int,
    stems,
    beat_info,
    mert_sections,
) -> None:
    """Overwrite the audio-side stem/MERT features with caller-supplied versions.

    extract_features_at_times above is called with use_demucs=False
    (passing-through mix as all four stems). The caller already ran
    Demucs once for the whole pipeline; we re-sample those stems here
    so the model gets the real per-stem RMS, not the pass-through mix
    duplicated four times.
    """
    from app.training.features import _mert_bucket_at, _rms_at_time  # noqa: WPS433

    import numpy as np  # noqa: WPS433

    for row, t in zip(rows, times):
        rms_drums = _rms_at_time(stems.drums, sr=sr, t=t)
        rms_vocals = _rms_at_time(stems.vocals, sr=sr, t=t)
        rms_bass = _rms_at_time(stems.bass, sr=sr, t=t)
        rms_other = _rms_at_time(stems.other, sr=sr, t=t)
        row.audio["rms_drums"] = rms_drums
        row.audio["rms_vocals"] = rms_vocals
        row.audio["rms_bass"] = rms_bass
        row.audio["rms_other"] = rms_other
        row.audio["mert_section_bucket"] = float(
            _mert_bucket_at(t=t, sections=mert_sections),
        )
        # v2.0 feature: recompute dominant_stem one-hot from the
        # caller-supplied (real) per-stem RMS rather than the
        # pass-through values the inline extractor would have written.
        if "dominant_stem_drums" in row.audio:
            stem_rms = (rms_drums, rms_vocals, rms_bass, rms_other)
            dom = int(np.argmax(stem_rms))
            row.audio["dominant_stem_drums"] = 1.0 if dom == 0 else 0.0
            row.audio["dominant_stem_vocals"] = 1.0 if dom == 1 else 0.0
            row.audio["dominant_stem_bass"] = 1.0 if dom == 2 else 0.0
            row.audio["dominant_stem_other"] = 1.0 if dom == 3 else 0.0


def _patch_prev_lane(row, prev_lane: int | None) -> None:
    """Set the prev_lane_* one-hot fields on a feature row in place."""
    for k in ("prev_lane_0", "prev_lane_1", "prev_lane_2", "prev_lane_3", "prev_lane_none"):
        row.context[k] = 0.0
    if prev_lane is None:
        row.context["prev_lane_none"] = 1.0
    else:
        key = f"prev_lane_{int(prev_lane)}"
        if key in row.context:
            row.context[key] = 1.0


def _flatten_value(row, col: str) -> float:
    """Pull a column out of the FeatureRow.audio or .context dict."""
    if col in row.audio:
        return float(row.audio[col])
    return float(row.context[col])


def _apply_distribution_correction(
    *,
    logits,
    recent_lanes: list[int],
):
    """Subtract a penalty from over-represented lanes' logits.

    Penalty = max(0, actual_share - target_share) * scale, applied per
    lane. So a lane that's already 40% of the recent window when target
    is 25% gets a 0.15 * scale penalty subtracted from its logit. Lanes
    at or below target are untouched.

    This directly counters the F/J bias the raw v0.1/v0.2 models exhibit
    on Western pop music without changing the model's understanding of
    which lane is musically right; it just lowers the threshold for D/K
    to win when the recent stream has been F/J-heavy.

    No-op for the first _RECENT_WINDOW notes (signal isn't stable yet).
    """
    import numpy as np  # noqa: WPS433

    if len(recent_lanes) < _RECENT_WINDOW // 2:
        return logits
    counts = [0, 0, 0, 0]
    for L in recent_lanes:
        counts[int(L)] += 1
    n = len(recent_lanes)
    penalty = np.array(
        [
            max(0.0, (counts[i] / n) - _TARGET_LANE_DIST[i]) * _DISTRIBUTION_PENALTY_SCALE
            for i in range(4)
        ],
        dtype=np.float32,
    )
    return np.asarray(logits, dtype=np.float32) - penalty


def _pick_lane_with_constraints(
    *,
    logits,
    last_hit: list[float],
    onset_t: float,
    recent_lanes: list[int],
) -> int | None:
    """Pick the best-scoring lane satisfying anti-cluster + hand-balance.

    Two-pass:
      1. Pick the best lane satisfying BOTH anti-cluster and hand-balance.
      2. If no lane satisfies both, relax hand-balance (keep anti-cluster).

    Returns None only when all four lanes were used within HIT_WINDOW_S
    of `onset_t`, in which case the caller drops the onset (matches the
    rule-based assigner's policy).
    """
    import numpy as np  # noqa: WPS433

    order = np.argsort(-np.asarray(logits))

    # Build the same-hand-streak picture from recent_lanes.
    same_hand_streak = 0
    last_hand: int = -1
    for L in recent_lanes:
        h = _HAND_OF_LANE[int(L)]
        if h == last_hand:
            same_hand_streak += 1
        else:
            same_hand_streak = 1
        last_hand = h

    # Pass 1: anti-cluster AND hand-balance.
    for lane in order:
        lane = int(lane)
        if onset_t - last_hit[lane] < HIT_WINDOW_S:
            continue
        candidate_hand = _HAND_OF_LANE[lane]
        if (
            same_hand_streak >= _MAX_SAME_HAND_STREAK
            and candidate_hand == last_hand
        ):
            continue
        return lane

    # Pass 2: anti-cluster only. The model + dist correction picked it;
    # better to accept a same-hand-streak violation than drop the note.
    for lane in order:
        lane = int(lane)
        if onset_t - last_hit[lane] >= HIT_WINDOW_S:
            return lane

    return None
