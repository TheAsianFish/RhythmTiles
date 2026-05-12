"""Liveness, readiness, and root index."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.ml import beat_this, mert

router = APIRouter(tags=["health"])


@router.get("/")
async def root() -> dict[str, object]:
    return {
        "name": "BeatBridge backend",
        "version": __version__,
        "endpoints": {
            "health": "/healthz",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "generate_chart": "POST /charts/generate",
            "generate_from_audio": "POST /charts/generate-from-audio",
            "get_cached_chart": "GET /charts/{content_hash}",
        },
    }


@router.get("/healthz")
async def healthz() -> dict[str, object]:
    """Liveness + a snapshot of which ML modes are active.

    The extension's menu reads `ml` to render a tiny mode indicator so the
    player can see at a glance which pipeline is producing their charts.
    `beatThisActive` is True only when the flag is on AND the package
    actually imports, matching the runtime dispatch in detect_beats.
    """
    return {
        "status": "ok",
        "version": __version__,
        "ml": {
            "beatThisFlag": settings.use_beat_this,
            "beatThisActive": settings.use_beat_this and beat_this.is_available(),
            "demucsFlag": settings.use_demucs,
            "mertFlag": settings.use_mert,
            "mertActive": settings.use_mert and mert.is_available(),
        },
    }
