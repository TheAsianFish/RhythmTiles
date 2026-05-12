"""MERT audio embeddings for section detection.

Phase 3 of docs/ML_PLAN.md. MERT (Music Encoder Representations from
Transformers, Yizhi Li et al., ISMIR 2024) is a HuBERT-style model
trained on music. Its hidden states encode musical structure (genre,
mood, instrumentation, section), which lets us segment a song into
intro/verse/chorus/bridge/outro labels rather than relying on RMS
energy alone.

We use the 95M-param variant by default: 380MB checkpoint, ~5s warmup,
fast enough on CPU to keep within the cold-start budget. The model
emits 13 layers of 768-d hidden states at 50 frames/sec on its native
24kHz input. We mean-pool across layers and downsample temporally
before clustering (the chart-builder side does the clustering; this
module is pure wrap-and-cache).

License note: MERT-v1-95M is CC-BY-NC. Acceptable for the
demo/personal-use stance v1 takes; flag for relicensing review before
any commercial release.

Activation:
  - USE_MERT=1 env var
  - `transformers` AND `torch` AND `torchaudio` importable (the latter
    is already required for the Beat This! and Demucs paths).

All failure modes fall back to the existing RMS bucket logic. The
heuristic pipeline remains the rollback.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("beatbridge.ml.mert")

# Default model. Tunable later via env var if MERT-330M is wanted; per
# ML_PLAN that's a 1.3GB upgrade with slower inference and the 95M
# version is sufficient for section clustering.
DEFAULT_MODEL_NAME = "m-a-p/MERT-v1-95M"

# MERT models expect 24kHz mono float32 input. The chart pipeline works
# at 22050Hz, so we resample on the way in.
MERT_SAMPLE_RATE = 24000


@dataclass
class MertResult:
    """Per-frame embeddings, mean-pooled across the model's hidden layers.

    embeddings: shape (n_frames, 768) for MERT-v1-95M.
    frame_rate_hz: frames per second (~50 for the 95M variant).
    """
    embeddings: "np.ndarray"
    frame_rate_hz: float


_MODEL = None  # type: ignore[var-annotated]
_PROCESSOR = None  # type: ignore[var-annotated]
_LOCK = threading.Lock()


def is_available() -> bool:
    """True when transformers + torch + torchaudio can be imported.

    The actual model load is deferred to _get_model and happens at most
    once per process.
    """
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
        import torchaudio  # noqa: F401  pyright: ignore[reportMissingImports]
    except Exception:
        return False
    return True


def embed(
    *,
    y: "np.ndarray",
    sr: int,
    model_name: str = DEFAULT_MODEL_NAME,
    device: str | None = None,
) -> MertResult | None:
    """Run MERT on a mono float32 buffer at any sample rate.

    Returns None on any failure (package missing, model load failed,
    inference raised). None is the signal to fall back to RMS bucketing.
    """
    model_processor = _get_model(model_name, device)
    if model_processor is None:
        return None
    try:
        import numpy as np  # noqa: WPS433
        import torch  # noqa: WPS433  pyright: ignore[reportMissingImports]

        resampled = _resample_for_mert(y=y, sr=sr)
        with _LOCK:
            return _run_inference(model_processor, resampled, device)
    except Exception as exc:  # noqa: BLE001
        logger.exception("MERT inference failed, falling back: %s", exc)
        return None


def warm(model_name: str = DEFAULT_MODEL_NAME, device: str | None = None) -> None:
    """Load weights now so the first user request doesn't pay for it.

    No-op when the package isn't installed. Safe to call from a
    background thread at startup.
    """
    if not is_available():
        return
    _get_model(model_name, device)


def _resample_for_mert(*, y: "np.ndarray", sr: int) -> "np.ndarray":
    """Resample to 24kHz if needed. Skip the work when already at target."""
    if sr == MERT_SAMPLE_RATE:
        return y
    import librosa  # noqa: WPS433
    return librosa.resample(y, orig_sr=sr, target_sr=MERT_SAMPLE_RATE)


def _resolve_device(device: str | None) -> str:
    if device:
        return device
    try:
        import torch  # noqa: WPS433  pyright: ignore[reportMissingImports]
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _get_model(model_name: str, device: str | None):  # noqa: ANN202
    """Construct (or return cached) MERT model + processor.

    Returns a (model, processor, device) tuple, or None on import failure.
    """
    global _MODEL, _PROCESSOR
    if _MODEL is not None and _PROCESSOR is not None:
        return _MODEL, _PROCESSOR, _resolve_device(device)
    with _LOCK:
        if _MODEL is not None and _PROCESSOR is not None:
            return _MODEL, _PROCESSOR, _resolve_device(device)
        try:
            from transformers import (
                AutoFeatureExtractor,
                AutoModel,
            )  # pyright: ignore[reportMissingImports]
            import torch  # noqa: WPS433  pyright: ignore[reportMissingImports]
        except Exception as exc:
            logger.warning(
                "USE_MERT requested but transformers/torch not importable "
                "(%s). Install with `pip install '.[mert]'`. Falling back "
                "to RMS bucketing.",
                exc,
            )
            return None
        try:
            # trust_remote_code=True is required by the MERT model card;
            # the model bundles custom modeling code that the standard
            # transformers loader doesn't recognize.
            resolved = _resolve_device(device)
            processor = AutoFeatureExtractor.from_pretrained(
                model_name, trust_remote_code=True,
            )
            model = AutoModel.from_pretrained(
                model_name, trust_remote_code=True,
            )
            model = model.to(resolved)
            model.eval()
            _MODEL = model
            _PROCESSOR = processor
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to construct MERT model: %s", exc)
            return None
        logger.info("MERT detector ready (model=%s device=%s)", model_name, resolved)
        return _MODEL, _PROCESSOR, resolved


def _run_inference(model_processor, audio_24k: "np.ndarray", device: str | None):
    """Run the actual forward pass + mean-pool across layers.

    Returns a MertResult or raises. Caller catches and returns None to
    the upstream fallback path.
    """
    import numpy as np  # noqa: WPS433
    import torch  # noqa: WPS433  pyright: ignore[reportMissingImports]

    model, processor, resolved = model_processor
    inputs = processor(
        audio_24k,
        sampling_rate=MERT_SAMPLE_RATE,
        return_tensors="pt",
    )
    # Move tensors to the model's device.
    for k, v in list(inputs.items()):
        if hasattr(v, "to"):
            inputs[k] = v.to(resolved)
    with torch.no_grad():
        out = model(**inputs, output_hidden_states=True)
    # out.hidden_states is a tuple of (n_layers, batch=1, n_frames, hidden_dim).
    # Stack -> (n_layers, n_frames, hidden_dim), mean-pool across layers ->
    # (n_frames, hidden_dim).
    hidden = torch.stack(out.hidden_states, dim=0).squeeze(1)  # (n_layers, n_frames, H)
    pooled = hidden.mean(dim=0).cpu().numpy().astype(np.float32)
    # MERT-v1-95M runs at ~50 fps on its native 24kHz input. The exact
    # rate depends on the feature extractor's hop size; derive it from
    # the embedding length vs audio duration for accuracy.
    duration_s = len(audio_24k) / MERT_SAMPLE_RATE
    frame_rate_hz = pooled.shape[0] / max(duration_s, 1e-6)
    return MertResult(embeddings=pooled, frame_rate_hz=float(frame_rate_hz))
