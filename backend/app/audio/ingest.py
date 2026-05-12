"""Audio ingest.

Two paths:
  - bytes_to_disk: write user-supplied bytes to a deterministic file under the
    cache directory, keyed by content hash.
  - fetch_videoid: pull audio for a YouTube videoId via yt-dlp. Disabled unless
    BACKEND_ALLOW_YTDLP is set.

The yt-dlp path needs ffmpeg to transcode to WAV. We use imageio-ffmpeg, which
ships a platform-native ffmpeg binary as a pip package, so users don't have to
install ffmpeg system-wide.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger("beatbridge.audio")


def _ffmpeg_path() -> str | None:
    """Return the ffmpeg executable to hand to yt-dlp, or None if unavailable."""
    # Prefer the bundled binary (no system install needed).
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except Exception:
        pass
    on_path = shutil.which("ffmpeg")
    return on_path


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
    wav_path = audio_dir / f"yt-{video_id}.wav"

    # If we already have a WAV for this id, reuse it without re-downloading.
    if wav_path.exists():
        logger.info("yt-dlp reusing cached audio for %s", video_id)
        digest = _hash_bytes(wav_path.read_bytes())
        return Ingested(path=wav_path, content_hash=digest, source_label="youtube")

    out_template = str(audio_dir / f"yt-{video_id}.%(ext)s")

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found. pip install '.[ytdlp]' brings in imageio-ffmpeg "
            "which ships a binary."
        )

    # Invoke yt-dlp as a Python module so we don't depend on it being on PATH.
    # The venv's Python imports its own yt_dlp module unambiguously.
    cmd = [
        sys.executable,
        "-m", "yt_dlp",
        "-x",
        "--audio-format", "wav",
        "-o", out_template,
        "--no-progress",
        "--quiet",
        "--ffmpeg-location", ffmpeg,
        # 8-minute cap defends against an accidentally-pasted livestream id.
        "--match-filter", "duration < 480",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    logger.info("yt-dlp fetch videoId=%s ffmpeg=%s", video_id, ffmpeg)
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    except FileNotFoundError as exc:
        # python itself missing - extremely unlikely - but report clearly.
        raise RuntimeError(f"could not invoke yt-dlp: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        if "No module named" in stderr:
            raise RuntimeError(
                "yt_dlp module missing. pip install '.[ytdlp]' to enable"
            ) from exc
        raise RuntimeError(f"yt-dlp failed: {stderr[:300]}") from exc

    if not wav_path.exists():
        raise RuntimeError(f"yt-dlp completed but {wav_path} is missing")
    digest = _hash_bytes(wav_path.read_bytes())
    return Ingested(path=wav_path, content_hash=digest, source_label="youtube")
