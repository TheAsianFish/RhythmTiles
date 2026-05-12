"""Disk cache for MERT-derived section labels.

MERT inference is the heaviest stage in the pipeline when active (~5s
weight load, then 0.5-2s for a 4-minute song on CPU). The section
labels it produces are difficulty-independent, so caching by audio
content hash means: run MERT once per song, then easy / normal / hard
/ expert on that song are free of the MERT cost.

The cached payload is tiny: a few section records per song. Stored as
JSON under `<CACHE_DIR>/sections/<hash>-<source>.json`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger("beatbridge.ml.section_cache")

_CACHE_VERSION = 1


@dataclass
class CachedSection:
    start_s: float
    end_s: float
    bucket: int            # 0=low intensity, 1=mid, 2=high
    cluster_id: int        # raw KMeans cluster id (for debugging / future use)
    intensity: float       # mean RMS of the section, normalized 0-1


def _cache_path(content_hash: str, source: str) -> Path:
    folder = settings.cache_dir / "sections"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{content_hash}-{source}.json"


def get(*, content_hash: str | None, source: str) -> list[CachedSection] | None:
    """Return cached sections or None on miss / corrupt / version mismatch."""
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
            CachedSection(
                start_s=float(s["start_s"]),
                end_s=float(s["end_s"]),
                bucket=int(s["bucket"]),
                cluster_id=int(s["cluster_id"]),
                intensity=float(s["intensity"]),
            )
            for s in payload["sections"]
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("section cache read failed (%s); ignoring", exc)
        return None


def put(
    *,
    content_hash: str | None,
    source: str,
    sections: list[CachedSection],
) -> None:
    """Persist sections to disk. Silent on write errors."""
    if not content_hash:
        return
    path = _cache_path(content_hash, source)
    payload = {
        "version": _CACHE_VERSION,
        "sections": [
            {
                "start_s": float(s.start_s),
                "end_s": float(s.end_s),
                "bucket": int(s.bucket),
                "cluster_id": int(s.cluster_id),
                "intensity": float(s.intensity),
            }
            for s in sections
        ],
    }
    try:
        path.write_text(json.dumps(payload, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("section cache write failed (%s); continuing", exc)
