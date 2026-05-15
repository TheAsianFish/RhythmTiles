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
    """Run real Demucs separation. Raises ImportError when demucs is missing.

    Uses the lower-level `demucs.pretrained.get_model` + `demucs.apply.apply_model`
    API that ships with demucs 4.0.1 (the high-level `demucs.api.Separator`
    wrapper was a planned 4.1 feature that never reached PyPI). Functionally
    equivalent; just more explicit about the model load + apply steps.
    """
    import numpy as np  # noqa: WPS433
    import torch  # noqa: WPS433  pyright: ignore[reportMissingImports]
    from demucs.apply import apply_model  # noqa: WPS433  pyright: ignore[reportMissingImports]
    from demucs.pretrained import get_model  # noqa: WPS433  pyright: ignore[reportMissingImports]

    model = get_model(model_name)
    model.eval()
    target_sr = int(model.samplerate)

    # Demucs expects stereo at the model's native sample rate (44100 for
    # htdemucs). We feed the mono signal duplicated to both channels.
    if sr != target_sr:
        import librosa  # noqa: WPS433
        y_at_target = librosa.resample(y, orig_sr=sr, target_sr=target_sr)
    else:
        y_at_target = y
    stereo = np.stack([y_at_target, y_at_target]).astype(np.float32)
    # apply_model wants shape (batch=1, channels=2, samples).
    tensor = torch.from_numpy(stereo).unsqueeze(0)

    # CUDA when the installed torch wheel exposes a GPU. apply_model moves
    # the model to the chosen device internally, so we don't .to() it here.
    # Fallback is CPU; the user-visible cost is "minutes vs seconds" per
    # MODELS.md.
    device = "cuda" if torch.cuda.is_available() else "cpu"
    with torch.no_grad():
        # `sources` shape: (batch, n_sources, channels, samples)
        sources = apply_model(model, tensor, device=device, progress=False)
    sources_np = sources[0].cpu().numpy().astype(np.float32)
    # Average channels back to mono per source.
    mono_sources = sources_np.mean(axis=1)

    # model.sources is the ordered list of source names (drums, bass, other, vocals
    # for htdemucs). Map by name so a different model that reorders won't silently
    # mis-route.
    name_to_signal: dict[str, np.ndarray] = {}
    for name, signal in zip(model.sources, mono_sources):
        if sr != target_sr:
            import librosa  # noqa: WPS433
            signal = librosa.resample(signal, orig_sr=target_sr, target_sr=sr)
        name_to_signal[name] = signal.astype(np.float32)

    return Stems(
        drums=name_to_signal.get("drums", y),
        vocals=name_to_signal.get("vocals", y),
        bass=name_to_signal.get("bass", y),
        other=name_to_signal.get("other", y),
        sr=sr,
        separated=True,
    )
