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

from pathlib import Path

from app.audio.ingest import bytes_to_disk, fetch_videoid
from app.cache import ChartCache
from app.config import settings
from app.models import (
    AudioMeta,
    Chart,
    ChartMeta,
    GenerateRequest,
    Note,
    PIPELINE_VERSION,
)
from app.pipeline.chart_builder import build_chart_from_audio, load_audio_to_mono
from app.util.concurrency import ChartQueueFull, chart_slot

router = APIRouter(prefix="/charts", tags=["charts"])
logger = logging.getLogger("beatbridge.charts")

_cache = ChartCache()

_PLACEHOLDER_SUFFIX = "-placeholder"

# Hard cap on upload audio duration in seconds. Matches the
# `--match-filter "duration < 480"` cap on the yt-dlp path so the upload
# route can't be used to bypass it. 8 minutes covers ~99% of pop songs;
# longer mixes are rejected with 413.
MAX_UPLOAD_DURATION_S = 480.0


def _mode_key() -> str:
    """Compact tag for the current ML-flag combination, baked into the
    chart cache key so a song generated under one mode is NOT served
    when the user is running a different mode.

    Format `vMAJOR-bt{0|1}-dm{0|1}-mt{0|1}` where MAJOR is the major+
    minor of PIPELINE_VERSION. A bump to PIPELINE_VERSION therefore
    auto-invalidates the cache; flag changes do too. Patch-level bumps
    are deliberately ignored - they shouldn't change chart output.
    """
    major_minor = ".".join(PIPELINE_VERSION.split(".")[:2])
    return (
        f"v{major_minor}-bt{int(settings.use_beat_this)}"
        f"-dm{int(settings.use_demucs)}-mt{int(settings.use_mert)}"
    )


def _cache_key(base: str) -> str:
    """Compose `<base>|<mode_key>` so the same audio under different
    modes occupies different cache rows."""
    return f"{base}|{_mode_key()}"


def _is_placeholder(chart: Chart) -> bool:
    """Was this chart produced by `_hardcoded_chart`, or by the real pipeline?

    The pipeline version is the single source of truth: `_hardcoded_chart`
    appends `-placeholder` to PIPELINE_VERSION. Real charts do not.
    """
    return chart.metadata.pipelineVersion.endswith(_PLACEHOLDER_SUFFIX)


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
    """Generate a chart from a videoId.

    Behaviour:
      - cache hit on (videoId, difficulty) -> return cached chart.
      - BACKEND_ALLOW_YTDLP=1 -> fetch audio via yt-dlp, run the real pipeline.
      - otherwise -> return a placeholder chart so the extension has something
        to render. This keeps the popup demo working without a yt-dlp install.
    """
    logger.info(
        "generate request videoId=%s difficulty=%s allow_ytdlp=%s",
        req.videoId,
        req.difficulty,
        settings.allow_ytdlp,
    )

    cache_key = req.videoId or req.audioUrl or "unknown"
    # Mode-aware cache key: a chart generated under ML-light must NOT be
    # served when the user is running ML-full. _cache_key() folds the
    # current pipeline version + the three ML flags into the lookup so
    # each mode has its own cache namespace.
    keyed = _cache_key(cache_key)
    cached = _cache.get(content_hash=keyed, difficulty=req.difficulty)
    # Cached placeholders are never returned. They poison subsequent requests
    # once the operator turns ytdlp on. We always re-generate in that case.
    if cached is not None and not _is_placeholder(cached):
        logger.info("cache hit for %s/%s mode=%s", cache_key, req.difficulty, _mode_key())
        return cached
    if cached is not None:
        logger.warning(
            "cache had a placeholder for %s/%s; ignoring and trying real pipeline",
            cache_key,
            req.difficulty,
        )

    if settings.allow_ytdlp and req.videoId:
        try:
            ingested = fetch_videoid(req.videoId)
            audio_bytes = ingested.path.read_bytes()
            # Secondary cache lookup by audio content hash. Catches the case
            # where the same audio is reached via a different request key
            # (e.g. a re-uploaded copy of the song under a different
            # videoId). Without this, we'd run the multi-minute pipeline
            # again on identical audio. Mode-aware via _cache_key().
            keyed_hash = _cache_key(ingested.content_hash)
            chart_from_hash = _cache.get(
                content_hash=keyed_hash, difficulty=req.difficulty,
            )
            if chart_from_hash is not None and not _is_placeholder(chart_from_hash):
                logger.info(
                    "cache hit by contentHash %s/%s mode=%s (request key %s)",
                    ingested.content_hash, req.difficulty, _mode_key(), cache_key,
                )
                # Backfill the request-key cache so the next click is a
                # direct hit without the audio fetch.
                _cache.put(
                    content_hash=keyed,
                    difficulty=req.difficulty,
                    chart=chart_from_hash,
                )
                return chart_from_hash
            try:
                async with chart_slot():
                    chart = build_chart_from_audio(
                        audio_bytes=audio_bytes,
                        filename=ingested.path.name,
                        difficulty=req.difficulty,
                        audio_source="youtube",
                        video_id=req.videoId,
                    )
            except ChartQueueFull as exc:
                raise HTTPException(status_code=429, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            # Loud: this is almost always the reason a player sees a 20-note
            # chart in production. Print the full traceback to the backend
            # terminal so it cannot be missed.
            logger.exception(
                "REAL PIPELINE FAILED for videoId=%s, returning 20-note placeholder. "
                "Cause: %s",
                req.videoId,
                exc,
            )
            chart = _hardcoded_chart(req)
    else:
        if not settings.allow_ytdlp:
            logger.warning(
                "Returning 20-note placeholder for videoId=%s because "
                "BACKEND_ALLOW_YTDLP is not set. Restart the backend with "
                "BACKEND_ALLOW_YTDLP=1 to enable real chart generation.",
                req.videoId,
            )
        chart = _hardcoded_chart(req)

    # Skip cache writes for placeholders. Real charts get cached under both
    # the request key (for popup re-clicks) and the audio hash (for re-use
    # across request shapes pointing at the same audio). Mode-aware keys
    # via _cache_key() so different ML modes don't cross-pollinate.
    if not _is_placeholder(chart):
        _cache.put(content_hash=keyed, difficulty=req.difficulty, chart=chart)
        if chart.audio.contentHash:
            _cache.put(
                content_hash=_cache_key(chart.audio.contentHash),
                difficulty=req.difficulty,
                chart=chart,
            )
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
    if difficulty not in {"easy", "normal", "hard", "expert"}:
        raise HTTPException(
            status_code=422, detail="difficulty must be easy/normal/hard/expert",
        )

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="empty audio upload")

    # Persist to the cache dir so we can keep it for debugging and replays.
    ingested = bytes_to_disk(audio_bytes, suffix=Path(audio.filename or "upload.wav").suffix or ".wav")
    keyed_hash = _cache_key(ingested.content_hash)
    cached = _cache.get(content_hash=keyed_hash, difficulty=difficulty)
    if cached is not None:
        return cached

    # Audio length cap. Mirrors the yt-dlp `--match-filter "duration < 480"`
    # on the videoId path so the upload route can't be used to bypass the
    # 8-minute cap and lock the server on a 30-minute mix. We probe duration
    # by decoding (load_audio_to_mono handles the formats librosa supports).
    try:
        y, sr = load_audio_to_mono(audio_bytes)
        duration_s = len(y) / float(sr)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail=f"could not decode audio: {exc}",
        ) from exc
    if duration_s > MAX_UPLOAD_DURATION_S:
        raise HTTPException(
            status_code=413,
            detail=(
                f"audio too long ({duration_s:.0f}s); cap is "
                f"{MAX_UPLOAD_DURATION_S:.0f}s. Trim and re-upload."
            ),
        )

    try:
        async with chart_slot():
            chart = build_chart_from_audio(
                audio_bytes=audio_bytes,
                filename=audio.filename or "upload.wav",
                difficulty=difficulty,
            )
    except ChartQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    _cache.put(content_hash=keyed_hash, difficulty=difficulty, chart=chart)
    return chart


@router.get("/{content_hash}", response_model=Chart, response_model_exclude_none=True)
async def get_chart(content_hash: str, difficulty: str = "normal") -> Chart:
    # Mode-aware lookup: the cache stores under "<hash>|<mode>" so a raw
    # content_hash from a URL needs the mode suffix applied.
    chart = _cache.get(content_hash=_cache_key(content_hash), difficulty=difficulty)
    if chart is None:
        raise HTTPException(status_code=404, detail="chart not found")
    return chart
