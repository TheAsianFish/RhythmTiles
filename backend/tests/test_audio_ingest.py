from __future__ import annotations

import pytest

from app.audio.ingest import bytes_to_disk, fetch_videoid


def test_bytes_to_disk_writes_and_hashes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    import importlib

    import app.config as config

    importlib.reload(config)
    # Re-import the ingest module so it picks up the reloaded settings.
    import app.audio.ingest as ingest

    importlib.reload(ingest)

    payload = b"fake-audio-bytes" * 10
    res = ingest.bytes_to_disk(payload, suffix=".wav")
    assert res.path.exists()
    assert res.path.read_bytes() == payload
    assert len(res.content_hash) == 16

    # Same content -> same path (idempotent).
    res2 = ingest.bytes_to_disk(payload, suffix=".wav")
    assert res2.path == res.path


def test_fetch_videoid_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BACKEND_ALLOW_YTDLP", raising=False)
    import importlib

    import app.config as config

    importlib.reload(config)
    import app.audio.ingest as ingest

    importlib.reload(ingest)
    with pytest.raises(RuntimeError, match="disabled"):
        ingest.fetch_videoid("abc")


def test_fetch_videoid_rejects_invalid_id(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_ALLOW_YTDLP", "1")
    import importlib

    import app.config as config

    importlib.reload(config)
    import app.audio.ingest as ingest

    importlib.reload(ingest)
    with pytest.raises(ValueError):
        ingest.fetch_videoid("")
    with pytest.raises(ValueError):
        ingest.fetch_videoid("x" * 64)
