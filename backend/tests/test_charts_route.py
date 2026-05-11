from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_generate_returns_placeholder_chart() -> None:
    client = _client()
    r = client.post(
        "/charts/generate",
        json={"videoId": "dQw4w9WgXcQ", "difficulty": "normal"},
    )
    assert r.status_code == 200, r.text
    chart = r.json()
    assert chart["version"] == "1.0"
    assert chart["audio"]["videoId"] == "dQw4w9WgXcQ"
    assert chart["metadata"]["difficulty"] == "normal"
    assert len(chart["notes"]) == 20
    lanes = {n["lane"] for n in chart["notes"]}
    assert lanes.issubset({0, 1, 2, 3})


def test_generate_requires_video_or_audio_url() -> None:
    client = _client()
    r = client.post("/charts/generate", json={"difficulty": "normal"})
    assert r.status_code == 422


def test_generate_caches_repeated_requests() -> None:
    client = _client()
    body = {"videoId": "cachekey-1", "difficulty": "normal"}
    r1 = client.post("/charts/generate", json=body)
    r2 = client.post("/charts/generate", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Same content hash on both responses.
    assert r1.json()["audio"]["contentHash"] == r2.json()["audio"]["contentHash"]


def test_get_chart_by_hash_404_when_missing() -> None:
    client = _client()
    r = client.get("/charts/does-not-exist")
    assert r.status_code == 404
