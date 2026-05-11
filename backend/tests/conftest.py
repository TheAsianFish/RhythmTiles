"""Pytest fixtures. Keeps the cache out of the user's real cache directory."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    # Force the cached Settings module to re-read by reimporting.
    import importlib

    import app.config as config

    importlib.reload(config)
    yield
    # No teardown needed; tmp_path is wiped by pytest.
