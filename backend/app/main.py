"""FastAPI app entrypoint.

Run with: uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import asyncio

from app import __version__
from app.config import settings
from app.middleware import RequestIdMiddleware
from app.routes import charts, health


def _setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def create_app() -> FastAPI:
    _setup_logging()
    app = FastAPI(
        title="BeatBridge",
        version=__version__,
        description="Audio analysis pipeline that returns Chart JSON.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(charts.router)

    @app.on_event("startup")
    async def _warmup() -> None:
        # Warm numba/JIT off the main thread so startup is non-blocking.
        from app.pipeline.warmup import warm_pipeline

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, warm_pipeline)

    @app.on_event("startup")
    async def _prune_caches() -> None:
        # One-shot prune of stale cached files + chart-cache rows. Cheap
        # at startup; bounds the disk footprint on long-running deploys.
        # Failures are swallowed so a permissions glitch doesn't keep the
        # backend from booting.
        from app.cache import ChartCache
        from app.util.prune import prune_caches

        loop = asyncio.get_event_loop()

        def _run() -> None:
            try:
                prune_caches()
            except Exception:  # noqa: BLE001
                pass
            try:
                from app.config import settings as _s
                age = float(_s.cache_dir.stat().st_mtime)  # noqa: F841
                # Chart cache prune uses a 30-day floor by default; the
                # cache itself reads CACHE_MAX_AGE_DAYS via the prune
                # helper. We just call its method with the same env value.
                import os as _os
                try:
                    days = float(_os.environ.get("CACHE_MAX_AGE_DAYS", "30"))
                except ValueError:
                    days = 30.0
                ChartCache().prune_older_than(max_age_days=days)
            except Exception:  # noqa: BLE001
                pass

        loop.run_in_executor(None, _run)

    return app


app = create_app()
