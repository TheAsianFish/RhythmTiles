"""End-to-end corpus collector.

Walks /beatmapsets/search, downloads the .osu chart for each 4K mania
hit, and pulls the .osz archive from a mirror so we can extract audio
later. Writes a resumable manifest so re-runs skip already-cached
beatmaps.

CLI:
    OSU_CLIENT_ID=... OSU_CLIENT_SECRET=... \
    python -m app.training.collect --target 1000 --out cache/training

What this DOES NOT do (intentionally separate concerns):
  - Audio extraction (see app.training.osz_extract)
  - Feature extraction (see app.training.features)
  - Training (see app.training.train_lane)

Why the data layer is separate from feature/training:
  - Resumability: a network hiccup mid-feature-extract shouldn't force
    a re-download of 1000 .osz files.
  - Compute split: feature extraction is CPU/Demucs-bound; data
    collection is network-bound. Different machines may run different
    parts (collect on laptop, extract on workstation).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.training.osu_api import (
    BeatmapSearchHit,
    NerinyanClient,
    OsuApiError,
    OsuClient,
)
from app.training.osu_parse import OsuParseError, parse_osu

logger = logging.getLogger("beatbridge.training.collect")


@dataclass
class ManifestEntry:
    """One row in cache/training/manifest.json."""

    beatmap_id: int
    beatmapset_id: int
    title: str
    artist: str
    creator: str
    version: str
    star_rating: float
    bpm: float
    total_length_s: int
    status: str
    chart_path: str  # relative to cache/training/
    osz_path: str | None  # relative to cache/training/, None if osz fetch failed
    fetched_at: str  # ISO-8601 UTC


def collect(
    *,
    target: int,
    out_dir: Path,
    skip_osz: bool = False,
    use_mirror: bool = False,
) -> list[ManifestEntry]:
    """Fetch up to `target` 4K mania charts into `out_dir`.

    Resumable: any beatmap with an existing chart file is skipped. Pass
    `skip_osz=True` to only fetch .osu chart files (useful for a quick
    pattern-only smoke test before bothering with audio).

    `use_mirror=True` uses the no-auth NerinyanClient path: chart files
    are extracted from the .osz at extraction time rather than fetched
    individually via osu.ppy.sh. With this mode there is NO osu! OAuth
    requirement.

    Returns the FULL manifest (existing + new entries). Caller should
    write it back to disk.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    osu_dir = out_dir / "osu"
    osz_dir = out_dir / "osz"
    osu_dir.mkdir(exist_ok=True)
    osz_dir.mkdir(exist_ok=True)

    manifest_path = out_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    already_have = {entry.beatmap_id for entry in manifest}

    if use_mirror:
        _collect_via_mirror(
            target=target,
            manifest=manifest,
            already_have=already_have,
            osu_dir=osu_dir,
            osz_dir=osz_dir,
            skip_osz=skip_osz,
            manifest_path=manifest_path,
        )
    else:
        _collect_via_official_api(
            target=target,
            manifest=manifest,
            already_have=already_have,
            osu_dir=osu_dir,
            osz_dir=osz_dir,
            skip_osz=skip_osz,
            manifest_path=manifest_path,
        )

    _save_manifest(manifest_path, manifest)
    return manifest


def _collect_via_official_api(
    *,
    target: int,
    manifest: list[ManifestEntry],
    already_have: set[int],
    osu_dir: Path,
    osz_dir: Path,
    skip_osz: bool,
    manifest_path: Path,
) -> None:
    with OsuClient() as client:
        cursor: dict[str, Any] | None = None
        while len(manifest) < target:
            try:
                hits, cursor = client.search_ranked_4k(cursor=cursor)
            except OsuApiError as exc:
                logger.error("search failed, stopping early: %s", exc)
                break

            if not hits:
                logger.info("search exhausted; pulled %d total", len(manifest))
                break

            for hit in hits:
                if len(manifest) >= target:
                    break
                if hit.beatmap_id in already_have:
                    continue
                entry = _fetch_one(
                    client=client,
                    hit=hit,
                    osu_dir=osu_dir,
                    osz_dir=osz_dir,
                    skip_osz=skip_osz,
                )
                if entry is None:
                    continue
                manifest.append(entry)
                already_have.add(entry.beatmap_id)
                _save_manifest(manifest_path, manifest)

            if cursor is None:
                logger.info("no more pages; pulled %d total", len(manifest))
                break


def _collect_via_mirror(
    *,
    target: int,
    manifest: list[ManifestEntry],
    already_have: set[int],
    osu_dir: Path,
    osz_dir: Path,
    skip_osz: bool,
    manifest_path: Path,
) -> None:
    """Mirror-only path: no auth, no OAuth. nerinyan search + download.

    Charts are extracted from the .osz at feature-extraction time
    (osz_extract.py reads the .osu from inside the archive). When
    skip_osz=True the mirror path is degenerate (no audio means no
    chart either, since charts live in the .osz); we still record
    manifest entries with chart_path pointing into the .osz.
    """
    import zipfile  # noqa: WPS433 - local to keep top-level light

    with NerinyanClient() as client:
        page = 1
        while len(manifest) < target:
            try:
                hits = client.search_ranked_4k(page=page)
            except OsuApiError as exc:
                logger.error("mirror search failed, stopping: %s", exc)
                break
            if not hits:
                logger.info("mirror exhausted at page %d; pulled %d", page, len(manifest))
                break

            for hit in hits:
                if len(manifest) >= target:
                    break
                if hit.beatmap_id in already_have:
                    continue

                osz_path = osz_dir / f"{hit.beatmapset_id}.osz"
                if not osz_path.exists():
                    try:
                        client.fetch_osz(hit.beatmapset_id, dest=osz_path)
                    except OsuApiError as exc:
                        logger.warning(
                            "osz fetch %d failed: %s", hit.beatmapset_id, exc,
                        )
                        continue

                # Extract just the .osu for this beatmap_id from the archive.
                # nerinyan packs all difficulties; we match by [Metadata]
                # BeatmapID (canonical, ID-keyed) rather than the Version
                # display string, which can drift through unicode, casing,
                # and special chars.
                chart_path = _extract_chart_from_osz(
                    osz_path=osz_path,
                    beatmap_id=hit.beatmap_id,
                    out_dir=osu_dir,
                )
                if chart_path is None:
                    logger.info(
                        "chart for bmap=%d version=%r not in osz=%d, skipping",
                        hit.beatmap_id, hit.version, hit.beatmapset_id,
                    )
                    continue

                entry = ManifestEntry(
                    beatmap_id=hit.beatmap_id,
                    beatmapset_id=hit.beatmapset_id,
                    title=hit.title,
                    artist=hit.artist,
                    creator=hit.creator,
                    version=hit.version,
                    star_rating=hit.star_rating,
                    bpm=hit.bpm,
                    total_length_s=hit.total_length_s,
                    status=hit.status,
                    chart_path=f"osu/{chart_path.name}",
                    osz_path=f"osz/{osz_path.name}",
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )
                manifest.append(entry)
                already_have.add(entry.beatmap_id)
                _save_manifest(manifest_path, manifest)
            page += 1


def _extract_chart_from_osz(
    *,
    osz_path: Path,
    beatmap_id: int,
    out_dir: Path,
) -> Path | None:
    """Extract the .osu whose [Metadata] BeatmapID matches `beatmap_id`.

    Returns the written path, or None if no matching difficulty was found.
    Matching by BeatmapID is robust to mirror-side annotations like the
    `[4K]` prefix nerinyan adds to convert difficulties (which we already
    filter out at search time, but ID matching is the right invariant
    regardless).
    """
    import zipfile  # noqa: WPS433

    target_path = out_dir / f"{beatmap_id}.osu"
    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path
    try:
        with zipfile.ZipFile(osz_path) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".osu"):
                    continue
                with zf.open(name) as f:
                    contents = f.read()
                text = contents.decode("utf-8-sig", errors="replace")
                if _osu_beatmap_id(text) == beatmap_id:
                    target_path.write_bytes(contents)
                    return target_path
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("bad osz %s: %s", osz_path.name, exc)
    return None


def _osu_beatmap_id(text: str) -> int | None:
    """Return the [Metadata] BeatmapID value from a .osu file's text, or None."""
    in_metadata = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_metadata = line == "[Metadata]"
            continue
        if in_metadata and line.startswith("BeatmapID:"):
            try:
                return int(line.partition(":")[2].strip())
            except ValueError:
                return None
    return None


def _fetch_one(
    *,
    client: OsuClient,
    hit: BeatmapSearchHit,
    osu_dir: Path,
    osz_dir: Path,
    skip_osz: bool,
) -> ManifestEntry | None:
    """Fetch + validate one beatmap. None if it should be excluded."""
    chart_path = osu_dir / f"{hit.beatmap_id}.osu"
    try:
        chart_bytes = client.fetch_chart(hit.beatmap_id)
    except OsuApiError as exc:
        logger.warning("chart fetch %d failed: %s", hit.beatmap_id, exc)
        return None
    chart_path.write_bytes(chart_bytes)

    # Validate by parsing; reject if not strict 4K mania (e.g. converted maps).
    try:
        parse_osu(chart_path)
    except OsuParseError as exc:
        logger.info("rejecting %d: %s", hit.beatmap_id, exc)
        chart_path.unlink(missing_ok=True)
        return None

    osz_relpath: str | None = None
    if not skip_osz:
        osz_path = osz_dir / f"{hit.beatmapset_id}.osz"
        if not osz_path.exists():
            try:
                client.fetch_osz(hit.beatmapset_id, dest=osz_path)
            except OsuApiError as exc:
                logger.warning(
                    "osz fetch %d failed (continuing without audio): %s",
                    hit.beatmapset_id,
                    exc,
                )
                osz_path = None  # type: ignore[assignment]
        if osz_path is not None and osz_path.exists():
            osz_relpath = f"osz/{osz_path.name}"

    return ManifestEntry(
        beatmap_id=hit.beatmap_id,
        beatmapset_id=hit.beatmapset_id,
        title=hit.title,
        artist=hit.artist,
        creator=hit.creator,
        version=hit.version,
        star_rating=hit.star_rating,
        bpm=hit.bpm,
        total_length_s=hit.total_length_s,
        status=hit.status,
        chart_path=f"osu/{chart_path.name}",
        osz_path=osz_relpath,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _load_manifest(path: Path) -> list[ManifestEntry]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("entries", [])
    return [ManifestEntry(**e) for e in entries]


def _save_manifest(path: Path, manifest: list[ManifestEntry]) -> None:
    payload = {
        "_license_posture": (
            "Research, non-commercial, non-redistributing. "
            "Charts collected for ML training only; not redistributed. "
            "See docs/PHASE5_PLAN.md (License + ethics section)."
        ),
        "_source": "osu.ppy.sh API v2 + community mirror",
        "_collected_at": datetime.now(timezone.utc).isoformat(),
        "entries": [asdict(e) for e in manifest],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Collect 4K mania ranked charts into a local training corpus.",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=1000,
        help="Number of charts to collect in total (existing + new).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("cache/training"),
        help="Output directory.",
    )
    parser.add_argument(
        "--skip-osz",
        action="store_true",
        help="Skip .osz audio archive download (chart-only smoke test).",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Use the unauth nerinyan.moe mirror path instead of the "
            "official osu! API. No OSU_CLIENT_ID/SECRET needed."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        manifest = collect(
            target=args.target,
            out_dir=args.out,
            skip_osz=args.skip_osz,
            use_mirror=args.no_auth,
        )
    except OsuApiError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    print(f"collected {len(manifest)} charts at {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
