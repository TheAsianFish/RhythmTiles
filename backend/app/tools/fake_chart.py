"""Print a sample Chart JSON to stdout. Used to seed the extension during dev."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.models import AudioMeta, Chart, ChartMeta, Note, PIPELINE_VERSION


def make_sample_chart(duration: float = 30.0) -> Chart:
    note_count = 30
    spacing = duration / (note_count + 1)
    notes = [
        Note(t=round(spacing * (i + 1), 3), lane=i % 4, type="tap")
        for i in range(note_count)
    ]
    return Chart(
        audio=AudioMeta(
            source="synthetic",
            duration=duration,
            bpm=120.0,
            contentHash="sample-0001",
        ),
        metadata=ChartMeta(
            generatedAt=datetime.now(timezone.utc),
            pipelineVersion=f"{PIPELINE_VERSION}-sample",
            difficulty="normal",
            keyMode=4,
            title="Sample",
        ),
        notes=notes,
    )


def main() -> None:
    chart = make_sample_chart()
    print(chart.model_dump_json(indent=2, exclude_none=True))


if __name__ == "__main__":
    main()
