"""Disk cache for beat-tracker outputs keyed by audio content.

Beat tracking is the most expensive deterministic stage in the pipeline
when Beat This! is enabled (a few seconds on CPU for a 4-minute song).
The same audio always produces the same beats, so caching by content
hash buys us the cold-start budget for every regenerated chart (e.g.
"replay this song on Hard" doesn't re-run the model).

Stored as JSON under `<cache_dir>/beats/<hash>-<source>.json`. Tiny
payload (a few KB at most); no SQLite or eviction needed. If the file
is missing or unreadable we just return None and the caller re-runs the
detector.

The cache key includes the detector name so a librosa run and a Beat
This! run never collide.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger("beatbridge.ml.beat_cache")

_CACHE_VERSION = 1


@dataclass
class CachedBeats:
    bpm: float
    beats: list[float]
    downbeats: list[float] | None
    bpm_curve: list[tuple[float, float]] | None


def _cache_path(content_hash: str, source: str) -> Path:
    folder = settings.cache_dir / "beats"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{content_hash}-{source}.json"


def get(*, content_hash: str | None, source: str) -> CachedBeats | None:
    """Return cached beats for this audio, or None on miss / read error."""
    if not content_hash:
        return None
    path = _cache_path(content_hash, source)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if payload.get("version") != _CACHE_VERSION:
            return None
        return CachedBeats(
            bpm=float(payload["bpm"]),
            beats=[float(b) for b in payload["beats"]],
            downbeats=(
                [float(b) for b in payload["downbeats"]]
                if payload.get("downbeats") is not None else None
            ),
            bpm_curve=(
                [(float(t), float(v)) for t, v in payload["bpm_curve"]]
                if payload.get("bpm_curve") is not None else None
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("beat cache read failed (%s); ignoring", exc)
        return None


def put(
    *,
    content_hash: str | None,
    source: str,
    bpm: float,
    beats: list[float],
    downbeats: list[float] | None,
    bpm_curve: list[tuple[float, float]] | None,
) -> None:
    """Persist beats for this audio. Silent on write errors."""
    if not content_hash:
        return
    path = _cache_path(content_hash, source)
    payload = {
        "version": _CACHE_VERSION,
        "bpm": float(bpm),
        "beats": [float(b) for b in beats],
        "downbeats": ([float(b) for b in downbeats] if downbeats is not None else None),
        "bpm_curve": (
            [[float(t), float(v)] for t, v in bpm_curve] if bpm_curve is not None else None
        ),
    }
    try:
        path.write_text(json.dumps(payload, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("beat cache write failed (%s); continuing", exc)
