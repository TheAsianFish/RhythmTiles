"""Source separation. Optional Demucs path, with a pass-through fallback.

The default pipeline runs on the full mix. When `USE_DEMUCS=1` is set AND
the `demucs` package is importable, this module returns separated stems
(drums, vocals, bass, other) that downstream stages can use for cleaner
rhythm detection and more musical lane assignment.

The first time Demucs runs it downloads the model (~300MB to the
torch hub cache). Subsequent runs reuse the cached weights. Inference is
roughly 1-4x realtime on CPU depending on hardware, much faster on GPU.

When Demucs is unavailable or disabled, `separate_stems` returns a Stems
object where every "stem" is the original mix. Callers can treat the
result uniformly without branching on capability.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("beatbridge.pipeline.stems")


@dataclass
class Stems:
    """Four-stem decomposition of a mono input. When separation is disabled
    or unavailable, every field holds the original mix audio."""
    drums: "np.ndarray"
    vocals: "np.ndarray"
    bass: "np.ndarray"
    other: "np.ndarray"
    sr: int
    # True when the stems are the result of real source separation rather
    # than the pass-through fallback. Callers use this to decide whether
    # to route drum onsets to the drum stem only or run onset detection
    # on the full mix.
    separated: bool = False

    @classmethod
    def from_mono_mix(cls, y: "np.ndarray", sr: int) -> "Stems":
        return cls(drums=y, vocals=y, bass=y, other=y, sr=sr, separated=False)


def separate_stems(
    y: "np.ndarray",
    sr: int,
    *,
    use_demucs: bool = False,
    model_name: str = "htdemucs",
) -> Stems:
    """Return a Stems object for the given mono audio.

    When `use_demucs=False`, or when the demucs package is not importable,
    or when the Demucs call raises, returns a pass-through Stems holding
    the original mix in every channel. This keeps the pipeline running
    even on machines without the optional dependency.
    """
    if not use_demucs:
        return Stems.from_mono_mix(y, sr)
    try:
        return _run_demucs(y=y, sr=sr, model_name=model_name)
    except ImportError as exc:
        logger.warning(
            "USE_DEMUCS=1 but demucs not importable (%s). "
            "Install with `pip install '.[stems]'` and re-run.",
            exc,
        )
        return Stems.from_mono_mix(y, sr)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Demucs separation failed, falling back to mix: %s", exc)
        return Stems.from_mono_mix(y, sr)


def _run_demucs(
    *,
    y: "np.ndarray",
    sr: int,
    model_name: str,
) -> Stems:
    """Run real Demucs separation. Raises ImportError when demucs is missing."""
    import numpy as np  # noqa: WPS433
    import torch  # noqa: WPS433  pyright: ignore[reportMissingImports]
    from demucs.api import Separator  # noqa: WPS433  pyright: ignore[reportMissingImports]

    # Demucs wants stereo input shaped [channels=2, samples]. We feed it
    # twice-the-mono so the model sees both channels identically. Returning
    # to mono after separation by averaging the stereo output.
    stereo = np.stack([y, y]).astype(np.float32)
    tensor = torch.from_numpy(stereo)

    sep = Separator(model=model_name, segment=None)
    if sr != sep.samplerate:
        # Demucs expects the model's native sample rate (usually 44100).
        # Resample with librosa so we don't introduce a separate dep.
        import librosa  # noqa: WPS433
        resampled = librosa.resample(y, orig_sr=sr, target_sr=sep.samplerate)
        stereo = np.stack([resampled, resampled]).astype(np.float32)
        tensor = torch.from_numpy(stereo)

    _origin, separated = sep.separate_tensor(tensor)
    # separated maps name -> Tensor shape (channels, samples). Average back to
    # mono and resample back to the caller's sr.
    out: dict[str, np.ndarray] = {}
    for name, t in separated.items():
        mono = t.mean(dim=0).numpy().astype(np.float32)
        if sr != sep.samplerate:
            import librosa  # noqa: WPS433
            mono = librosa.resample(mono, orig_sr=sep.samplerate, target_sr=sr)
        out[name] = mono

    return Stems(
        drums=out.get("drums", y),
        vocals=out.get("vocals", y),
        bass=out.get("bass", y),
        other=out.get("other", y),
        sr=sr,
        separated=True,
    )
