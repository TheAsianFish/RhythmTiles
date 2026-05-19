"""Extract the audio file out of a .osz archive.

A .osz is just a ZIP. The audio path is named in the .osu file's
[General] AudioFilename field. Different difficulties inside the same
.osz can technically reference different audio files; we read the
specific .osu we trained against (not just "the first .osu") to be
safe.

Outputs the audio bytes to a stable location keyed by beatmapset_id so
multiple difficulties of the same set share one extracted audio file.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from app.training.osu_parse import OsuParseError, parse_osu_text

logger = logging.getLogger("beatbridge.training.osz_extract")


class OszExtractError(RuntimeError):
    """Raised when the archive is missing, malformed, or has no audio."""


def extract_audio(
    *,
    osz_path: Path,
    chart_path: Path,
    out_dir: Path,
) -> Path:
    """Extract the audio file referenced by `chart_path` from `osz_path`.

    Returns the extracted file's path (under `out_dir`). If extraction
    has already happened (same beatmapset_id + same audio filename),
    returns the cached path without re-extracting.

    Raises OszExtractError if the archive is malformed, the chart's
    [General] AudioFilename is absent, or the referenced audio entry is
    missing from the archive.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    chart_text = chart_path.read_text(encoding="utf-8-sig")
    try:
        beatmap = parse_osu_text(chart_text)
    except OsuParseError as exc:
        raise OszExtractError(f"chart parse failed: {exc}") from exc

    if not beatmap.audio_filename:
        raise OszExtractError("chart has no [General] AudioFilename")

    suffix = Path(beatmap.audio_filename).suffix.lower() or ".mp3"
    dest = out_dir / f"{osz_path.stem}{suffix}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    if not osz_path.exists():
        raise OszExtractError(f"osz file not found: {osz_path}")

    try:
        with zipfile.ZipFile(osz_path) as zf:
            entry_name = _resolve_audio_entry(zf, beatmap.audio_filename)
            if entry_name is None:
                raise OszExtractError(
                    f"audio entry {beatmap.audio_filename!r} not found in {osz_path.name}",
                )
            with zf.open(entry_name) as src, dest.open("wb") as out:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    except zipfile.BadZipFile as exc:
        raise OszExtractError(f"bad zip: {exc}") from exc

    return dest


def _resolve_audio_entry(zf: zipfile.ZipFile, target: str) -> str | None:
    """Case-insensitive, separator-tolerant lookup of `target` in the archive.

    .osu files reference audio with the filename only (no folder), but
    some .osz archives put files under subdirectories. Match by basename
    case-insensitively.
    """
    target_basename = target.replace("\\", "/").split("/")[-1].lower()
    for name in zf.namelist():
        if name.replace("\\", "/").split("/")[-1].lower() == target_basename:
            return name
    return None
