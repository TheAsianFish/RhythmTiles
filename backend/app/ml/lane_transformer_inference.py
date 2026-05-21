"""Inference adapter for the v2 Transformer lane assigner.

Loaded by `app/ml/learned_lanes.py` when `LEARNED_LANES_ARCH=transformer`
is set. Falls back to v1 LightGBM otherwise.

Strategy:
  - Load model + normalization stats + schema once per process
  - At inference, get per-event features (same schema as training)
  - Run model in overlapping windows of SEQ_LEN events
  - Average overlapping predictions to smooth window boundaries
  - Return per-event lane logits; the caller (learned_lanes.assign_lanes_learned)
    applies the same anti-cluster + hand-balance + distribution-correction
    postprocessor that v1 uses

Window choice: stride = SEQ_LEN // 2 (50% overlap). With SEQ_LEN=32 and
~600 events per chart, we get ~38 windows. At inference batch 38 windows
through the model in one forward pass on GPU. Cost: a single GPU pass
of ~38 sequences of length 32, hidden 192. ~1ms on the 4070.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("beatbridge.ml.lane_transformer_inference")

DEFAULT_MODEL_DIR = Path("backend/models/lane_transformer_v1")


@dataclass
class _LoadedTransformer:
    """Cached model + normalization stats."""

    model: object  # torch.nn.Module
    mean: object   # np.ndarray (F,)
    std: object    # np.ndarray (F,)
    feature_columns: tuple[str, ...]
    schema_version: str
    seq_len: int
    device: str


_LOADED: _LoadedTransformer | None = None
_LOCK = threading.Lock()
_LOAD_FAILED: bool = False


def is_available() -> bool:
    """True iff the transformer artifact loads cleanly."""
    if _LOAD_FAILED:
        return False
    if _LOADED is not None:
        return True
    return _load() is not None


def reset_for_tests() -> None:
    """Clear cached state. Tests only."""
    global _LOADED, _LOAD_FAILED
    with _LOCK:
        _LOADED = None
        _LOAD_FAILED = False


def predict_per_event_logits(feature_matrix):
    """Run the transformer over a (N, F) feature matrix; return (N, 4) logits.

    `feature_matrix` is a numpy float32 array with one row per onset in
    feature-schema order. Returns None on any failure so the caller can
    fall back to the v1 model or rule-based assigner.
    """
    loaded = _load()
    if loaded is None:
        return None

    try:
        import numpy as np  # noqa: WPS433
        import torch  # noqa: WPS433

        X = np.asarray(feature_matrix, dtype=np.float32)
        n_events, n_features = X.shape
        if n_features != len(loaded.feature_columns):
            logger.error(
                "feature dim mismatch: got %d, model expects %d",
                n_features, len(loaded.feature_columns),
            )
            return None

        # Normalise with stored train-set stats.
        X_n = (X - np.asarray(loaded.mean)) / np.asarray(loaded.std)

        seq_len = loaded.seq_len
        if n_events < seq_len:
            # Pad with zeros at the end for very short charts.
            pad_amount = seq_len - n_events
            X_padded = np.vstack([X_n, np.zeros((pad_amount, n_features), dtype=np.float32)])
            with torch.no_grad():
                inp = torch.from_numpy(X_padded).unsqueeze(0).to(loaded.device)
                logits = loaded.model(inp)[0, :n_events].cpu().numpy()
            return logits

        # Multi-window with 50% stride. Each event ends up in ~2 windows;
        # we average those logits to smooth boundaries.
        stride = seq_len // 2
        sums = np.zeros((n_events, 4), dtype=np.float32)
        counts = np.zeros(n_events, dtype=np.int32)

        # Build all windows up-front and run them in one batch on GPU.
        starts: list[int] = []
        for s in range(0, n_events - seq_len + 1, stride):
            starts.append(s)
        # Right-align the last window if the stride didn't reach the end.
        if not starts or starts[-1] + seq_len < n_events:
            starts.append(n_events - seq_len)

        window_stack = np.stack([X_n[s:s + seq_len] for s in starts], axis=0)
        with torch.no_grad():
            inp = torch.from_numpy(window_stack).to(loaded.device)
            logits = loaded.model(inp).cpu().numpy()  # (W, T, 4)

        for w_idx, s in enumerate(starts):
            for t in range(seq_len):
                event_idx = s + t
                if event_idx >= n_events:
                    continue
                sums[event_idx] += logits[w_idx, t]
                counts[event_idx] += 1

        # Avoid division-by-zero; clamp counts to >=1.
        counts_safe = np.maximum(counts, 1).astype(np.float32).reshape(-1, 1)
        return sums / counts_safe

    except Exception as exc:  # noqa: BLE001
        logger.exception("transformer inference failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load() -> _LoadedTransformer | None:
    global _LOADED, _LOAD_FAILED
    if _LOADED is not None:
        return _LOADED
    if _LOAD_FAILED:
        return None
    with _LOCK:
        if _LOADED is not None:
            return _LOADED
        if _LOAD_FAILED:
            return None

        model_dir = _resolve_model_dir()
        if not (model_dir / "model.pt").exists():
            logger.info(
                "no transformer model at %s; v2 inference unavailable",
                model_dir,
            )
            _LOAD_FAILED = True
            return None

        try:
            import numpy as np  # noqa: WPS433
            import torch  # noqa: WPS433
            from app.training.lane_transformer import _build_model  # noqa: WPS433
        except ImportError as exc:
            logger.warning("torch / training package missing: %s", exc)
            _LOAD_FAILED = True
            return None

        try:
            schema = json.loads(
                (model_dir / "feature_schema.json").read_text(encoding="utf-8"),
            )
            ckpt = torch.load(str(model_dir / "model.pt"), map_location="cpu", weights_only=False)
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = _build_model(
                feature_dim=ckpt["feature_dim"],
                hidden=ckpt.get("hidden", 192),
                n_heads=ckpt.get("n_heads", 4),
                n_layers=ckpt.get("n_layers", 4),
                max_seq=ckpt.get("max_seq", 64),
                n_classes=ckpt.get("n_classes", 4),
            )
            model.load_state_dict(ckpt["state_dict"])
            model = model.to(device).eval()
            mean = np.asarray(ckpt["mean"], dtype=np.float32)
            std = np.asarray(ckpt["std"], dtype=np.float32)
        except Exception as exc:  # noqa: BLE001
            logger.exception("transformer load failed: %s", exc)
            _LOAD_FAILED = True
            return None

        _LOADED = _LoadedTransformer(
            model=model,
            mean=mean,
            std=std,
            feature_columns=tuple(schema["columns"]),
            schema_version=str(schema.get("version", "unknown")),
            seq_len=int(ckpt.get("seq_len", 32)),
            device=device,
        )
        logger.info(
            "transformer model loaded: schema=%s seq_len=%d device=%s features=%d",
            _LOADED.schema_version, _LOADED.seq_len, device,
            len(_LOADED.feature_columns),
        )
        return _LOADED


def _resolve_model_dir() -> Path:
    import os  # noqa: WPS433
    override = os.environ.get("LANE_TRANSFORMER_MODEL_DIR")
    if override:
        return Path(override).resolve()
    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent.parent
    return repo_root / "backend" / "models" / "lane_transformer_v1"
