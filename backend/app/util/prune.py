"""Periodic cache prune. Trims old files under CACHE_DIR to bound disk use.

Two flavours of cache live under `<CACHE_DIR>`:
  - `audio/` (yt-dlp downloads, ~10MB each per 4-min song)
  - `beats/`, `onsets/`, `sections/` (tiny ML output caches)
  - `charts.sqlite` (the ChartCache, pruned by its own method)

The audio files are the disk-space concern: a busy server can accumulate
several GB over a month. This module deletes audio + ml caches older
than CACHE_MAX_AGE_DAYS (default 30) and chart-cache rows older than the
same window. Designed to be called from a background asyncio task at
startup + once a day; cheap enough that it's also safe to call by hand.

The audio cache is the worst-case footprint and the easiest to refill -
on next request the yt-dlp path re-downloads and recomputes. The ML
output caches are negligible disk-wise but stale entries against a
bumped PIPELINE_VERSION shouldn't be served, so we prune them too.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger("beatbridge.util.prune")


def _max_age_days() -> float:
    raw = os.environ.get("CACHE_MAX_AGE_DAYS", "30")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


def prune_caches() -> dict[str, int]:
    """Delete files under `<CACHE_DIR>/<sub>/` older than CACHE_MAX_AGE_DAYS.

    Returns a per-subdirectory count of removed files for logging. Silent
    on individual delete failures (a busy file should not block the
    rest of the prune).
    """
    max_age_days = _max_age_days()
    if max_age_days <= 0:
        return {}
    cutoff = time.time() - max_age_days * 86400.0
    counts: dict[str, int] = {}
    cache_root: Path = settings.cache_dir
    for sub in ("audio", "beats", "onsets", "sections"):
        folder = cache_root / sub
        if not folder.exists():
            continue
        removed = 0
        for path in folder.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                # File in use, race with another prune, or perms;
                # skip and continue.
                continue
        if removed:
            counts[sub] = removed
    if counts:
        logger.info("cache prune removed: %s", counts)
    return counts
