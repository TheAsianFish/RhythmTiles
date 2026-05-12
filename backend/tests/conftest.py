"""Pytest fixtures. Keeps the cache out of the user's real cache directory."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point CACHE_DIR at a fresh tmp_path and rebuild every module that captured
    settings or the cache instance at import time."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    # Disable yt-dlp in tests regardless of the host shell's env.
    monkeypatch.setenv("BACKEND_ALLOW_YTDLP", "0")
    monkeypatch.setenv("USE_DEMUCS", "0")

    import importlib

    # Order matters: config first, then anything that captured the old settings
    # or instantiated module-level singletons against it.
    import app.config as config
    importlib.reload(config)
    import app.cache as cache
    importlib.reload(cache)
    import app.audio.ingest as ingest
    importlib.reload(ingest)
    import app.routes.charts as charts
    importlib.reload(charts)
    yield
