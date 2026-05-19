"""Parser for .osu beatmap files (osu!mania 4K subset).

Pure-Python, no network, no third-party deps. Returns typed hit events
plus the metadata needed to locate the audio file.

References:
  - https://osu.ppy.sh/wiki/en/Client/File_formats/osu_(file_format)
  - https://osu.ppy.sh/wiki/en/Client/File_formats/osz_(file_format)

Why we rolled our own instead of using `osu-beatmap-parser` or
`python-osu-parser`: both PyPI options are unmaintained as of 2026 and
neither cleanly handles the mania-hold `endTime:hitSample` quirk where
the last field uses `:` as a separator instead of `,`. We only need 4
sections out of the format so a focused parser is ~120 lines.

Filtering policy enforced here:
  - Mode == 3 (osu!mania)
  - CircleSize == 4 (strict 4K)
  - Reject converts (Mode == 0 charts that play as mania)
  - Drop hit events with t < 0 (lead-in markers, defensive)

Output is sorted by time. Hold events carry `duration_s`; taps have
`duration_s == 0.0`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Mania-mode key count for our v1 dataset. Strict; 5K/7K/keys-other are
# their own gameplay paradigms and shouldn't pollute training labels.
TARGET_KEY_COUNT = 4

# Osu! file format mode IDs. We accept only mania.
_MODE_OSU_STD = 0
_MODE_TAIKO = 1
_MODE_CATCH = 2
_MODE_MANIA = 3

# Bitfield for the `type` column of a hit object.
_TYPE_HIT_CIRCLE = 1
_TYPE_SLIDER = 2
_TYPE_NEW_COMBO = 4
_TYPE_SPINNER = 8
_TYPE_MANIA_HOLD = 128


class OsuParseError(ValueError):
    """Raised when the file is unparseable or fails the mode/key filter."""


@dataclass(frozen=True)
class OsuHitEvent:
    """One mania hit event: tap or hold, on a specific lane, at time t_s."""

    t_s: float
    lane: int  # 0..3 for 4K
    type: str  # "tap" or "hold"
    duration_s: float  # 0.0 for tap; positive for hold

    def __post_init__(self) -> None:
        if self.lane < 0 or self.lane >= TARGET_KEY_COUNT:
            raise ValueError(f"lane out of range for {TARGET_KEY_COUNT}K: {self.lane}")
        if self.type not in ("tap", "hold"):
            raise ValueError(f"unknown type: {self.type}")
        if self.type == "tap" and self.duration_s != 0.0:
            raise ValueError("tap events must have duration_s == 0.0")
        if self.type == "hold" and self.duration_s <= 0.0:
            raise ValueError("hold events must have duration_s > 0")


@dataclass(frozen=True)
class OsuBeatmap:
    """Parsed beatmap. `events` is sorted ascending by t_s.

    `audio_filename` is the relative path inside the .osz archive. The
    fetcher's job to resolve it to an extracted file on disk.
    """

    beatmap_id: int | None
    beatmapset_id: int | None
    title: str
    artist: str
    creator: str
    version: str
    mode: int
    key_count: int
    audio_filename: str
    audio_lead_in_ms: int
    events: list[OsuHitEvent]


def parse_osu(path: str | Path) -> OsuBeatmap:
    """Parse an .osu file from disk. Raises OsuParseError on mode/key mismatch.

    Uses `utf-8-sig` encoding to tolerate UTF-8 BOM that some .osu files
    ship with. Line endings are normalised by Python's universal newline
    handling.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    return parse_osu_text(text)


def parse_osu_text(text: str) -> OsuBeatmap:
    """Parse .osu file contents. The pure-text entry point (unit-testable)."""
    sections = _split_sections(text)

    general = _parse_kv_section(sections.get("General", ""))
    metadata = _parse_kv_section(sections.get("Metadata", ""))
    difficulty = _parse_kv_section(sections.get("Difficulty", ""))

    mode = int(general.get("Mode", "0"))
    if mode != _MODE_MANIA:
        raise OsuParseError(f"not mania (Mode={mode})")

    key_count = int(float(difficulty.get("CircleSize", "0")))
    if key_count != TARGET_KEY_COUNT:
        raise OsuParseError(
            f"not {TARGET_KEY_COUNT}K (CircleSize={key_count})",
        )

    audio_filename = general.get("AudioFilename", "").strip()
    audio_lead_in_ms = int(general.get("AudioLeadIn", "0"))

    events = _parse_hit_objects(
        sections.get("HitObjects", ""),
        key_count=key_count,
    )
    events.sort(key=lambda e: e.t_s)

    return OsuBeatmap(
        beatmap_id=_int_or_none(metadata.get("BeatmapID")),
        beatmapset_id=_int_or_none(metadata.get("BeatmapSetID")),
        title=metadata.get("Title", ""),
        artist=metadata.get("Artist", ""),
        creator=metadata.get("Creator", ""),
        version=metadata.get("Version", ""),
        mode=mode,
        key_count=key_count,
        audio_filename=audio_filename,
        audio_lead_in_ms=audio_lead_in_ms,
        events=events,
    )


# ---------------------------------------------------------------------------
# Section splitting + KV parsing
# ---------------------------------------------------------------------------

_SECTION_HEADER_RE = re.compile(r"^\[([A-Za-z]+)\]\s*$")


def _split_sections(text: str) -> dict[str, str]:
    """Group .osu lines under their [Section] headers.

    The format-version header line ("osu file format vNN") above the
    first section is ignored.
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        m = _SECTION_HEADER_RE.match(line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _parse_kv_section(body: str) -> dict[str, str]:
    """Parse a `Key: value` or `Key:value` section into a dict.

    Whitespace around the separator is tolerated. Blank lines and
    comment lines (starting with `//`) are skipped.
    """
    out: dict[str, str] = {}
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Hit objects
# ---------------------------------------------------------------------------


def _parse_hit_objects(body: str, *, key_count: int) -> list[OsuHitEvent]:
    """Parse the [HitObjects] section into mania hit events.

    Skips slider/spinner rows that osu!standard maps include but which
    shouldn't appear in a mode-3 file. Defensive: we filter them out
    rather than raising, because some test maps mix object types.
    """
    out: list[OsuHitEvent] = []
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        event = _parse_hit_object_line(line, key_count=key_count)
        if event is not None:
            out.append(event)
    return out


def _parse_hit_object_line(line: str, *, key_count: int) -> OsuHitEvent | None:
    """Parse one [HitObjects] line. Returns None for unsupported object types.

    Format reference:
      tap:  x,y,time,type,hitSound,hitSample
      hold: x,y,time,type,hitSound,endTime:hitSample

    The hold's `endTime:hitSample` field uses `:` (not `,`) as separator,
    which is the quirk that breaks naive comma-splits. We split on the
    first 5 commas and treat the rest as one params blob, then peel the
    leading int off that blob with `partition(':')`.
    """
    parts = line.split(",", maxsplit=5)
    if len(parts) < 5:
        return None
    try:
        x = int(parts[0])
        time_ms = int(parts[2])
        type_bits = int(parts[3])
    except ValueError:
        return None

    # Reject slider / spinner (osu!standard objects that shouldn't appear
    # in a real mania chart). Defensive against mixed-mode test data.
    if type_bits & (_TYPE_SLIDER | _TYPE_SPINNER):
        return None

    is_hold = bool(type_bits & _TYPE_MANIA_HOLD)
    is_tap = bool(type_bits & _TYPE_HIT_CIRCLE)
    if not is_hold and not is_tap:
        return None
    # Hold takes precedence: some converters set both bits.
    if is_hold:
        is_tap = False

    lane = _x_to_lane(x, key_count=key_count)

    if time_ms < 0:
        return None  # lead-in marker / pre-roll; not a real note

    t_s = time_ms / 1000.0

    if is_hold:
        end_field = parts[5] if len(parts) > 5 else ""
        end_str, _, _ = end_field.partition(":")
        try:
            end_ms = int(end_str)
        except ValueError:
            return None
        if end_ms <= time_ms:
            return None  # zero or negative hold; skip
        duration_s = (end_ms - time_ms) / 1000.0
        return OsuHitEvent(
            t_s=t_s,
            lane=lane,
            type="hold",
            duration_s=duration_s,
        )

    return OsuHitEvent(t_s=t_s, lane=lane, type="tap", duration_s=0.0)


def _x_to_lane(x: int, *, key_count: int) -> int:
    """Mania lane = clamp(floor(x * keys / 512), 0, keys-1).

    The playfield is 512 wide. For 4K the four x-centers are at 64, 192,
    320, 448, mapping to lanes 0..3.
    """
    lane = (x * key_count) // 512
    return max(0, min(key_count - 1, lane))


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
