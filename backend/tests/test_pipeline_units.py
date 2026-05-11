"""Unit tests for the analysis pipeline.

We don't ship librosa-dependent tests in the skeleton; they require numpy/scipy
to be installed and on Windows that pulls a chunky wheel. Mark these so they're
optional during the initial setup.
"""

from __future__ import annotations

import pytest

from app.pipeline.difficulty import (
    approximate_density,
    shape_difficulty,
    target_density,
)
from app.pipeline.lane_assign import RawNote, assign_lanes
from app.pipeline.onset_detect import Onset


def test_target_density_known_values() -> None:
    assert target_density("easy") == 1.0
    assert target_density("normal") == 1.5
    assert target_density("hard") == 2.5


def test_assign_lanes_routes_by_centroid() -> None:
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),    # low -> 0 or 1
        Onset(t=0.5, strength=1.0, centroid_hz=4000.0),   # high -> 2 or 3
        Onset(t=1.0, strength=1.0, centroid_hz=300.0),    # low again
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    assert len(notes) == 3
    assert notes[0].lane in (0, 1)
    assert notes[1].lane in (2, 3)
    assert notes[2].lane in (0, 1)


def test_assign_lanes_anti_clustering() -> None:
    # Two low onsets 10ms apart should land in different lanes.
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),
        Onset(t=0.01, strength=1.0, centroid_hz=200.0),
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    assert len(notes) == 2
    assert notes[0].lane != notes[1].lane


def test_assign_lanes_drops_when_all_lanes_too_recent() -> None:
    # 5 onsets within 10ms; only 4 lanes; one should be dropped.
    onsets = [
        Onset(t=i * 0.002, strength=1.0, centroid_hz=200.0 + i)
        for i in range(5)
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    assert len(notes) <= 4


def test_shape_difficulty_thins_above_target() -> None:
    # 60 notes over 10 beats = density 6.0. easy target 1.0 -> should thin hard.
    beats = [0.0 + i * 0.5 for i in range(11)]
    notes = [RawNote(t=i * 0.083, lane=i % 4) for i in range(60)]
    shaped = shape_difficulty(notes=notes, difficulty="easy", beats=beats)
    density = approximate_density(shaped, beats)
    assert density <= target_density("easy") * 1.5  # within 50% of target


def test_shape_difficulty_no_op_when_under_target() -> None:
    beats = [0.0 + i * 0.5 for i in range(11)]
    notes = [RawNote(t=i * 1.0, lane=i % 4) for i in range(4)]
    shaped = shape_difficulty(notes=notes, difficulty="hard", beats=beats)
    assert shaped == notes


def test_shape_difficulty_handles_empty() -> None:
    assert shape_difficulty(notes=[], difficulty="normal", beats=[]) == []
