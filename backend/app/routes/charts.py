"""Chart generation endpoints.

For Stage 1 the generate endpoint returns a hardcoded chart so the extension
has something to render. The real pipeline lands in Stage 2 inside
app/pipeline/, gated behind a feature flag while it stabilizes.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile

from app.cache import ChartCache
from app.models import (
    AudioMeta,
    Chart,
    ChartMeta,
    GenerateRequest,
    Note,
    PIPELINE_VERSION,
)
from app.pipeline.chart_builder import build_chart_from_audio

router = APIRouter(prefix="/charts", tags=["charts"])
logger = logging.getLogger("beatbridge.charts")

_cache = ChartCache()


def _hardcoded_chart(req: GenerateRequest) -> Chart:
    """Stage 1 placeholder: 20 evenly-spaced notes over 30 seconds.

    Returned when no real audio is provided and the pipeline cannot run.
    """
    duration = req.duration or 30.0
    note_count = 20
    spacing = duration / (note_count + 1)
    notes: list[Note] = []
    for i in range(note_count):
        t = round(spacing * (i + 1), 3)
        lane = i % 4
        notes.append(Note(t=t, lane=lane, type="tap"))

    content_hash = hashlib.sha256(
        f"placeholder:{req.videoId or req.audioUrl}:{req.difficulty}".encode()
    ).hexdigest()[:16]

    return Chart(
        audio=AudioMeta(
            source="youtube" if req.videoId else "upload",
            videoId=req.videoId,
            contentHash=content_hash,
            duration=duration,
            bpm=120.0,
        ),
        metadata=ChartMeta(
            generatedAt=datetime.now(timezone.utc),
            pipelineVersion=f"{PIPELINE_VERSION}-placeholder",
            difficulty=req.difficulty,
            keyMode=4,
        ),
        notes=notes,
    )


@router.post("/generate", response_model=Chart, response_model_exclude_none=True)
async def generate(req: GenerateRequest) -> Chart:
    """Generate a chart. JSON body only.

    Stage 1: returns a placeholder chart for any request.
    Stage 2: returns a real chart when audio is available, otherwise placeholder.
    """
    logger.info(
        "generate request videoId=%s difficulty=%s",
        req.videoId,
        req.difficulty,
    )

    # Cache lookup.
    cache_key = req.videoId or req.audioUrl or "unknown"
    cached = _cache.get(content_hash=cache_key, difficulty=req.difficulty)
    if cached is not None:
        logger.info("cache hit for %s/%s", cache_key, req.difficulty)
        return cached

    chart = _hardcoded_chart(req)
    _cache.put(content_hash=cache_key, difficulty=req.difficulty, chart=chart)
    return chart


@router.post(
    "/generate-from-audio",
    response_model=Chart,
    response_model_exclude_none=True,
)
async def generate_from_audio(
    audio: UploadFile,
    difficulty: str = "normal",
) -> Chart:
    """Upload an audio file and get back a real chart.

    Available when the real pipeline is wired in (Stage 2).
    Until then this endpoint will reject with 501 unless the file is a WAV.
    """
    if difficulty not in {"easy", "normal", "hard"}:
        raise HTTPException(status_code=422, detail="difficulty must be easy/normal/hard")

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="empty audio upload")

    try:
        chart = build_chart_from_audio(
            audio_bytes=audio_bytes,
            filename=audio.filename or "upload.wav",
            difficulty=difficulty,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    _cache.put(content_hash=chart.audio.contentHash or "", difficulty=difficulty, chart=chart)
    return chart


@router.get("/{content_hash}", response_model=Chart, response_model_exclude_none=True)
async def get_chart(content_hash: str, difficulty: str = "normal") -> Chart:
    chart = _cache.get(content_hash=content_hash, difficulty=difficulty)
    if chart is None:
        raise HTTPException(status_code=404, detail="chart not found")
    return chart
