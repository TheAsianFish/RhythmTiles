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
    """True iff the learned-lane model can be loaded.

    Cheap on the second call (cached). Returns False if either LightGBM
    is missing, the artifact directory is missing, or a previous load
    attempt failed.
    """
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

    model = _load_model()
    if model is None:
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

    if tuple(model.feature_columns) != ALL_FEATURE_COLUMNS:
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

    # Now predict autoregressively.
    import numpy as np  # noqa: WPS433

    notes: list[RawNote] = []
    last_hit = [-1e9, -1e9, -1e9, -1e9]
    prev_lane: int | None = None
    booster = model.booster  # type: ignore[assignment]
    for row, onset in zip(rows, onsets):
        _patch_prev_lane(row, prev_lane)
        feature_vec = np.array(
            [
                _flatten_value(row, col)
                for col in ALL_FEATURE_COLUMNS
            ],
            dtype=np.float32,
        ).reshape(1, -1)
        try:
            logits = booster.predict(feature_vec)[0]  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.exception("inference failed on onset t=%.3f: %s", onset.t, exc)
            return None
        lane = _pick_lane_with_anticluster(
            logits=logits,
            last_hit=last_hit,
            onset_t=float(onset.t),
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

    for row, t in zip(rows, times):
        row.audio["rms_drums"] = _rms_at_time(stems.drums, sr=sr, t=t)
        row.audio["rms_vocals"] = _rms_at_time(stems.vocals, sr=sr, t=t)
        row.audio["rms_bass"] = _rms_at_time(stems.bass, sr=sr, t=t)
        row.audio["rms_other"] = _rms_at_time(stems.other, sr=sr, t=t)
        row.audio["mert_section_bucket"] = float(
            _mert_bucket_at(t=t, sections=mert_sections),
        )


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


def _pick_lane_with_anticluster(
    *,
    logits,
    last_hit: list[float],
    onset_t: float,
) -> int | None:
    """Pick the highest-scoring lane that isn't busy within HIT_WINDOW_S."""
    # numpy import is deferred to keep this module light on import.
    import numpy as np  # noqa: WPS433

    order = np.argsort(-np.asarray(logits))
    for lane in order:
        lane = int(lane)
        if onset_t - last_hit[lane] >= HIT_WINDOW_S:
            return lane
    return None
