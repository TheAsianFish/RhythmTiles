"""Tests for the production-hardening pieces (queue, cap, prune)."""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app.main import create_app


def _synth_wav_bytes(seconds: float, sr: int = 22050) -> bytes:
    n = int(seconds * sr)
    rng = np.random.default_rng(0)
    y = (rng.standard_normal(n) * 0.05).astype(np.float32)
    # Add a few clicks so it's not pure noise.
    for k in range(0, n, sr // 2):
        end = min(k + int(0.02 * sr), n)
        y[k:end] += 0.5
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_upload_rejects_too_long_audio() -> None:
    """Upload route caps audio at MAX_UPLOAD_DURATION_S (8 min). Anything
    longer must be rejected with 413, not run through the multi-minute
    pipeline."""
    app = create_app()
    client = TestClient(app)
    # 9-minute upload exceeds the 480s cap.
    audio_bytes = _synth_wav_bytes(seconds=9 * 60)
    files = {"audio": ("long.wav", audio_bytes, "audio/wav")}
    r = client.post("/charts/generate-from-audio?difficulty=normal", files=files)
    assert r.status_code == 413
    assert "audio too long" in r.json()["detail"].lower()


def test_upload_accepts_short_audio() -> None:
    """A short upload (well under 8 min) should pass the cap check and
    succeed. This guards against the cap regressing to reject everything."""
    app = create_app()
    client = TestClient(app)
    audio_bytes = _synth_wav_bytes(seconds=5)
    files = {"audio": ("short.wav", audio_bytes, "audio/wav")}
    r = client.post("/charts/generate-from-audio?difficulty=normal", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audio"]["duration"] == pytest.approx(5.0, abs=0.2)


def test_chart_slot_acquires_and_releases() -> None:
    """The chart_slot context manager must release the semaphore on exit
    so the next request can acquire it. Without this, the first heavy
    request would lock the queue forever."""
    from app.util.concurrency import chart_slot, _ensure_semaphore

    async def go() -> None:
        # Take + release. Second acquire must succeed (otherwise we'd hang).
        async with chart_slot():
            pass
        async with chart_slot():
            pass

    asyncio.run(go())


def test_chart_cache_prune_older_than_drops_old_rows(tmp_path, monkeypatch) -> None:
    """ChartCache.prune_older_than deletes rows whose created_at is past
    the cutoff. New rows must survive. We backdate by manipulating the
    SQLite directly since the public put() always uses time.time()."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    import importlib

    import app.config as config
    importlib.reload(config)
    import app.cache as cache_mod
    importlib.reload(cache_mod)

    from app.models import AudioMeta, Chart, ChartMeta

    cache = cache_mod.ChartCache()

    def make_chart(hash_: str) -> Chart:
        from datetime import datetime, timezone
        return Chart(
            audio=AudioMeta(source="upload", contentHash=hash_, duration=10.0, bpm=120.0),
            metadata=ChartMeta(
                generatedAt=datetime.now(timezone.utc),
                pipelineVersion="0.2.0",
                difficulty="normal",
                keyMode=4,
            ),
            notes=[],
        )

    cache.put(content_hash="old", difficulty="normal", chart=make_chart("old"))
    cache.put(content_hash="new", difficulty="normal", chart=make_chart("new"))

    # Backdate the "old" row 60 days into the past.
    old_ts = time.time() - 60 * 86400
    with cache._lock:  # type: ignore[attr-defined]
        cache._conn.execute(  # type: ignore[attr-defined]
            "UPDATE chart_cache SET created_at = ? WHERE content_hash = ?",
            (old_ts, "old"),
        )
        cache._conn.commit()  # type: ignore[attr-defined]

    removed = cache.prune_older_than(max_age_days=30.0)
    assert removed == 1
    assert cache.get(content_hash="old", difficulty="normal") is None
    assert cache.get(content_hash="new", difficulty="normal") is not None


def test_prune_caches_drops_stale_files(tmp_path, monkeypatch) -> None:
    """prune_caches() deletes files under <CACHE_DIR>/{audio,beats,...}
    older than CACHE_MAX_AGE_DAYS. Fresh files must survive."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_MAX_AGE_DAYS", "30")
    import importlib

    import app.config as config
    importlib.reload(config)
    import app.util.prune as prune_mod
    importlib.reload(prune_mod)

    audio_dir = Path(tmp_path) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    old_file = audio_dir / "old.wav"
    new_file = audio_dir / "new.wav"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    # Backdate old_file 60 days into the past.
    old_ts = time.time() - 60 * 86400
    import os
    os.utime(old_file, (old_ts, old_ts))

    counts = prune_mod.prune_caches()
    assert counts.get("audio") == 1
    assert not old_file.exists()
    assert new_file.exists()
