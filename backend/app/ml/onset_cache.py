"""Disk cache for per-stem onset lists keyed by audio content.

Demucs is the slowest stage in the pipeline by an order of magnitude
(minutes on CPU). The chart cache helps for repeat plays of the same
chart, but a player who replays a song at a different difficulty pays
Demucs cost again because chart cache keys include difficulty.

Per-stem onsets, on the other hand, are difficulty-INDEPENDENT (the
difficulty selectivity runs downstream of onset detection). So caching
them by audio content hash means: run Demucs + onset detection once
per song, then easy / normal / hard / expert on that song are all free
of the Demucs cost.

Payload is small (a few thousand floats per song), stored as JSON next
to the beat cache under `<cache_dir>/onsets/`. Cache hits include the
stem tag on each onset so lane routing works the same as a fresh run.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import settings
from app.pipeline.onset_detect import Onset

logger = logging.getLogger("beatbridge.ml.onset_cache")

_CACHE_VERSION = 1


def _cache_path(content_hash: str, source: str) -> Path:
    folder = settings.cache_dir / "onsets"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{content_hash}-{source}.json"


def get(*, content_hash: str | None, source: str) -> list[Onset] | None:
    """Return cached onsets or None on miss / corrupt / version mismatch."""
    if not content_hash:
        return None
    path = _cache_path(content_hash, source)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if payload.get("version") != _CACHE_VERSION:
            return None
        return [
            Onset(
                t=float(o["t"]),
                strength=float(o["strength"]),
                centroid_hz=float(o["centroid_hz"]),
                stem=o.get("stem"),
            )
            for o in payload["onsets"]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("onset cache read failed (%s); ignoring", exc)
        return None


def put(
    *,
    content_hash: str | None,
    source: str,
    onsets: list[Onset],
) -> None:
    """Persist onsets to disk. Silent on write errors."""
    if not content_hash:
        return
    path = _cache_path(content_hash, source)
    payload = {
        "version": _CACHE_VERSION,
        "onsets": [
            {
                "t": float(o.t),
                "strength": float(o.strength),
                "centroid_hz": float(o.centroid_hz),
                "stem": o.stem,
            }
            for o in onsets
        ],
    }
    try:
        path.write_text(json.dumps(payload, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("onset cache write failed (%s); continuing", exc)
