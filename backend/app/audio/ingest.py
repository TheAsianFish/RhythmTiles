"""Audio ingest.

Two paths:
  - bytes_to_disk: write user-supplied bytes to a deterministic file under the
    cache directory, keyed by content hash.
  - fetch_videoid: pull audio for a YouTube videoId via yt-dlp. Disabled unless
    BACKEND_ALLOW_YTDLP is set; intended for local development and CI only.

Returns (path, content_hash) so the caller can both cache by hash and read
audio for the pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger("beatbridge.audio")


@dataclass(frozen=True)
class Ingested:
    path: Path
    content_hash: str
    source_label: str  # "upload" or "youtube"


def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def bytes_to_disk(audio_bytes: bytes, suffix: str = ".bin") -> Ingested:
    """Persist arbitrary audio bytes. Used by the upload endpoint."""
    if not audio_bytes:
        raise ValueError("empty audio bytes")
    digest = _hash_bytes(audio_bytes)
    audio_dir = settings.cache_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(audio_bytes)
    return Ingested(path=path, content_hash=digest, source_label="upload")


def fetch_videoid(video_id: str) -> Ingested:
    """Download audio for a YouTube videoId. Requires BACKEND_ALLOW_YTDLP=1.

    Returns the file on disk. Caller is responsible for cleanup if desired.
    """
    if not settings.allow_ytdlp:
        raise RuntimeError(
            "yt-dlp ingest is disabled. set BACKEND_ALLOW_YTDLP=1 to enable for dev"
        )
    if not video_id or len(video_id) > 32:
        raise ValueError("invalid videoId")

    audio_dir = settings.cache_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    # Predictable filename per video; yt-dlp will overwrite if it already exists.
    out_template = str(audio_dir / f"yt-{video_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "wav",
        "-o", out_template,
        "--no-progress",
        "--quiet",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    logger.info("yt-dlp fetch videoId=%s", video_id)
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "yt-dlp not installed. pip install '.[ytdlp]' to enable"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"yt-dlp failed: {exc.stderr.decode(errors='replace')[:200]}") from exc

    wav_path = audio_dir / f"yt-{video_id}.wav"
    if not wav_path.exists():
        raise RuntimeError(f"yt-dlp completed but {wav_path} is missing")
    digest = _hash_bytes(wav_path.read_bytes())
    return Ingested(path=wav_path, content_hash=digest, source_label="youtube")
