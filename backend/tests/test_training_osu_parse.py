"""Tests for app.training.osu_parse.

These are pure-text fixtures so the suite runs without network or audio
deps. The fixtures exercise the format's known quirks:
  - hold-note `endTime:hitSample` colon separator
  - UTF-8 BOM tolerance
  - mode/key filtering rejects converted maps + 7K mania
  - lane mapping for 4K (x=64,192,320,448 -> lane 0,1,2,3)
"""

from __future__ import annotations

import pytest

from app.training.osu_parse import (
    OsuParseError,
    parse_osu_text,
)

# Minimal valid 4K mania chart with one tap and one hold.
_VALID_MIN = """\
osu file format v14

[General]
AudioFilename: song.mp3
AudioLeadIn: 0
Mode: 3

[Metadata]
Title: TestTitle
TitleUnicode: TestTitle
Artist: TestArtist
ArtistUnicode: TestArtist
Creator: TestCreator
Version: Insane
BeatmapID: 12345
BeatmapSetID: 67890

[Difficulty]
HPDrainRate: 8
CircleSize: 4
OverallDifficulty: 8
ApproachRate: 5
SliderMultiplier: 1.4
SliderTickRate: 1

[TimingPoints]
0,500,4,2,1,30,1,0

[HitObjects]
64,192,1000,1,0,0:0:0:0:
448,192,2000,128,0,2500:0:0:0:0:
"""


def test_parse_minimal_4k_mania_tap_and_hold():
    bm = parse_osu_text(_VALID_MIN)
    assert bm.mode == 3
    assert bm.key_count == 4
    assert bm.audio_filename == "song.mp3"
    assert bm.beatmap_id == 12345
    assert bm.beatmapset_id == 67890
    assert len(bm.events) == 2

    tap, hold = bm.events
    assert tap.type == "tap"
    assert tap.lane == 0  # x=64 -> lane 0 in 4K
    assert tap.t_s == pytest.approx(1.0)
    assert tap.duration_s == 0.0

    assert hold.type == "hold"
    assert hold.lane == 3  # x=448 -> lane 3 in 4K
    assert hold.t_s == pytest.approx(2.0)
    assert hold.duration_s == pytest.approx(0.5)


def test_lane_mapping_all_four_columns():
    """Verify the x_centre -> lane mapping for the standard 4K x positions."""
    body = "\n".join([
        "[General]\nMode: 3",
        "[Difficulty]\nCircleSize: 4",
        "[HitObjects]\n"
        "64,192,1000,1,0,0:0:0:0:\n"
        "192,192,2000,1,0,0:0:0:0:\n"
        "320,192,3000,1,0,0:0:0:0:\n"
        "448,192,4000,1,0,0:0:0:0:",
    ])
    bm = parse_osu_text(body)
    assert [e.lane for e in bm.events] == [0, 1, 2, 3]


def test_rejects_osu_standard_mode():
    body = _VALID_MIN.replace("Mode: 3", "Mode: 0")
    with pytest.raises(OsuParseError, match="not mania"):
        parse_osu_text(body)


def test_rejects_7k_mania():
    body = _VALID_MIN.replace("CircleSize: 4", "CircleSize: 7")
    with pytest.raises(OsuParseError, match="not 4K"):
        parse_osu_text(body)


def test_skips_hit_object_with_negative_time():
    """Some maps put a lead-in marker at t<0; we drop those."""
    body = _VALID_MIN.replace(
        "[HitObjects]\n"
        "64,192,1000,1,0,0:0:0:0:\n"
        "448,192,2000,128,0,2500:0:0:0:0:",
        "[HitObjects]\n"
        "64,192,-100,1,0,0:0:0:0:\n"
        "64,192,1000,1,0,0:0:0:0:",
    )
    bm = parse_osu_text(body)
    assert len(bm.events) == 1
    assert bm.events[0].t_s == pytest.approx(1.0)


def test_skips_zero_duration_hold():
    body = _VALID_MIN.replace(
        "448,192,2000,128,0,2500:0:0:0:0:",
        "448,192,2000,128,0,2000:0:0:0:0:",
    )
    bm = parse_osu_text(body)
    # Tap survived; the zero-duration hold was dropped.
    assert len(bm.events) == 1
    assert bm.events[0].type == "tap"


def test_skips_slider_and_spinner_rows():
    """Defensive: if a mode-3 file contains stray slider/spinner objects
    (artifact of conversion tools), skip those rows rather than crash."""
    body = _VALID_MIN.replace(
        "[HitObjects]\n"
        "64,192,1000,1,0,0:0:0:0:\n"
        "448,192,2000,128,0,2500:0:0:0:0:",
        "[HitObjects]\n"
        "64,192,1000,1,0,0:0:0:0:\n"
        "100,100,1500,2,0,P|99:99,1,140\n"   # slider
        "256,192,1800,8,0,2500,0:0:0:0:\n"   # spinner
        "448,192,2000,128,0,2500:0:0:0:0:",
    )
    bm = parse_osu_text(body)
    # Slider/spinner skipped; tap + hold survive.
    assert [e.type for e in bm.events] == ["tap", "hold"]


def test_tolerates_utf8_bom():
    body_with_bom = "﻿" + _VALID_MIN
    bm = parse_osu_text(body_with_bom)
    assert bm.mode == 3
    assert bm.key_count == 4


def test_unsorted_hit_objects_get_sorted_by_time():
    """Some older maps emit hit objects out of chronological order; parser sorts."""
    body = _VALID_MIN.replace(
        "[HitObjects]\n"
        "64,192,1000,1,0,0:0:0:0:\n"
        "448,192,2000,128,0,2500:0:0:0:0:",
        "[HitObjects]\n"
        "448,192,2000,128,0,2500:0:0:0:0:\n"
        "64,192,1000,1,0,0:0:0:0:",
    )
    bm = parse_osu_text(body)
    assert [e.t_s for e in bm.events] == pytest.approx([1.0, 2.0])


def test_hold_event_with_chained_hitsample_colons():
    """endTime is the FIRST colon-separated token; the rest is hitSample.
    The naive '.split(":", 1)' approach must not eat into endTime."""
    body = _VALID_MIN.replace(
        "448,192,2000,128,0,2500:0:0:0:0:",
        "448,192,2000,128,0,2750:0:0:0:0:custom-sample.wav",
    )
    bm = parse_osu_text(body)
    hold = bm.events[-1]
    assert hold.type == "hold"
    assert hold.duration_s == pytest.approx(0.75)
