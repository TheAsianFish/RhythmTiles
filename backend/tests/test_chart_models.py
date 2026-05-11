"""Round-trip the Pydantic Chart model against the JSON schema.

If they drift, this test fails. The fix is to update whichever one is wrong.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from app.models import AudioMeta, Chart, ChartMeta, Note

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "shared" / "chart-schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _sample_chart() -> Chart:
    return Chart(
        audio=AudioMeta(
            source="upload",
            contentHash="abc123",
            duration=30.0,
            bpm=120.0,
        ),
        metadata=ChartMeta(
            generatedAt=datetime.now(timezone.utc),
            pipelineVersion="0.1.0",
            difficulty="normal",
            keyMode=4,
        ),
        notes=[
            Note(t=1.0, lane=0, type="tap"),
            Note(t=1.5, lane=2, type="hold", duration=0.5),
        ],
    )


def test_pydantic_chart_validates_against_json_schema() -> None:
    chart = _sample_chart()
    schema = _load_schema()
    # Match the on-the-wire shape: optional null fields are stripped before send.
    payload = json.loads(chart.model_dump_json(exclude_none=True))
    jsonschema.validate(payload, schema)


def test_hold_without_duration_rejected() -> None:
    with pytest.raises(ValueError):
        Note(t=1.0, lane=0, type="hold")  # missing duration


def test_tap_with_duration_rejected() -> None:
    with pytest.raises(ValueError):
        Note(t=1.0, lane=0, type="tap", duration=0.5)


def test_lane_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        Note(t=1.0, lane=4, type="tap")
