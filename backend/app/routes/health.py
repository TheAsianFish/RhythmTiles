"""Liveness, readiness, and root index."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__

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
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
