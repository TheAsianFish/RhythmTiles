"""Beat This! beat + downbeat detector.

Wraps the CPJKU `beat_this` package (ISMIR 2024 SOTA beat tracking) behind
the same `BeatInfo` interface that the librosa path returns. Three reasons
this is the highest-priority ML upgrade per docs/ML_PLAN.md:

  1. Librosa's beat tracker drifts on tempo-shifting songs and on tracks
     where the kick isn't the loudest event. Beat This! holds up on both.
  2. Librosa gives us beats only. Beat This! gives downbeats too, which
     unlocks musically-grounded accent chord emission (lane_assign no
     longer has to guess "this loud-high-centroid onset is an accent";
     real downbeats tell us where the bar starts).
  3. Pip-installable, MIT-licensed, weights tens of MB. Drop-in if the
     env flag is set; pure fallback to librosa otherwise.

Activation:
  - `USE_BEAT_THIS=1` environment variable
  - `beat_this` package importable

When either of those is false, callers fall through to the librosa path.
Errors during inference also fall through (the heuristic pipeline stays
the rollback). The pattern matches `pipeline/stems.py`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("beatbridge.ml.beat_this")


@dataclass
class BeatThisResult:
    """Raw output of Beat This! before we wrap it in BeatInfo."""
    beats: list[float]
    downbeats: list[float]


# A single Beat This! detector is expensive to construct (weights load,
# torch graph setup). Cache it at module scope; calls are serialized by
# a lock because the underlying torch model isn't thread-safe across
# requests on CPU.
_DETECTOR = None  # type: ignore[var-annotated]
_DETECTOR_LOCK = threading.Lock()


def is_available() -> bool:
    """True when the `beat_this` package can be imported.

    Cheap and side-effect-free; safe to call on every request. The actual
    model load is deferred to `_get_detector` and happens at most once
    per process.
    """
    try:
        import beat_this  # noqa: F401
    except Exception:
        return False
    return True


def detect(
    *,
    y: "np.ndarray",
    sr: int,
    device: str | None = None,
) -> BeatThisResult | None:
    """Run Beat This! on a mono float32 audio buffer.

    Returns BeatThisResult or None on any failure. None is the signal to
    the caller to fall back to the librosa path. We deliberately catch
    broadly here: the heuristic pipeline is the safety net and we'd
    rather chart a song with librosa than 500 the request.
    """
    detector = _get_detector(device=device)
    if detector is None:
        return None
    try:
        import numpy as np  # noqa: WPS433

        with _DETECTOR_LOCK:
            # Beat This! Audio2Beats expects (audio_array, sr) and returns
            # (beats, downbeats) as numpy arrays of times in seconds. The
            # API is stable as of beat_this >= 0.2.
            beats_arr, downbeats_arr = detector(np.asarray(y, dtype=np.float32), sr)
        beats = [float(b) for b in beats_arr]
        downbeats = [float(b) for b in downbeats_arr]
        return BeatThisResult(beats=beats, downbeats=downbeats)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Beat This! inference failed, falling back: %s", exc)
        return None


def _resolve_device(device: str | None) -> str:
    """Pick a device string for Beat This! / torch.

    beat_this >= 1.1.0 requires a concrete device string (not None). We
    default to CUDA when torch sees a GPU, else CPU. Caller can override.
    """
    if device:
        return device
    try:
        import torch  # noqa: WPS433  pyright: ignore[reportMissingImports]
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _get_detector(device: str | None = None):  # noqa: ANN202
    """Construct (or return cached) Beat This! detector. None on import failure.

    Loading the model is ~1-3s on first call (weight download + graph
    init). The cached instance handles every subsequent request.
    """
    global _DETECTOR
    if _DETECTOR is not None:
        return _DETECTOR
    with _DETECTOR_LOCK:
        if _DETECTOR is not None:
            return _DETECTOR
        try:
            from beat_this.inference import Audio2Beats  # pyright: ignore[reportMissingImports]
        except Exception as exc:
            logger.warning(
                "USE_BEAT_THIS requested but beat_this is not importable (%s). "
                "Install with `pip install '.[ml]'`. Falling back to librosa.",
                exc,
            )
            return None
        resolved = _resolve_device(device)
        try:
            # `dbn=False` skips the slow DBN post-processing. The pure
            # neural output is what the paper reports SOTA on; the DBN
            # mode is for evaluation parity with older trackers and is
            # roughly 10x slower.
            _DETECTOR = Audio2Beats(device=resolved, dbn=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to construct Beat This! detector: %s", exc)
            return None
        logger.info("Beat This! detector ready (device=%s)", resolved)
        return _DETECTOR


def warm() -> None:
    """Load weights now so the first user request doesn't pay for it.

    Safe to call from a background thread at startup. No-ops when the
    package isn't installed.
    """
    if not is_available():
        return
    _get_detector()
