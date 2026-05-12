from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_ok() -> None:
    app = create_app()
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_healthz_reports_ml_flags() -> None:
    """The extension menu reads /healthz.ml to render the active-mode chip."""
    app = create_app()
    client = TestClient(app)
    body = client.get("/healthz").json()
    assert "ml" in body
    ml = body["ml"]
    # Baseline by default (the conftest unsets these flags). Just verify
    # the shape; bool truthiness is what the frontend keys on.
    assert isinstance(ml.get("beatThisFlag"), bool)
    assert isinstance(ml.get("beatThisActive"), bool)
    assert isinstance(ml.get("demucsFlag"), bool)


def test_request_id_echoed() -> None:
    app = create_app()
    client = TestClient(app)
    r = client.get("/healthz", headers={"x-request-id": "abc123"})
    assert r.headers.get("x-request-id") == "abc123"


def test_request_id_generated_when_missing() -> None:
    app = create_app()
    client = TestClient(app)
    r = client.get("/healthz")
    rid = r.headers.get("x-request-id")
    assert rid is not None and len(rid) >= 8
