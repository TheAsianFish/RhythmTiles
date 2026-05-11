"""Run the full pipeline against a local audio file. Prints chart stats.

Usage:
    python -m app.tools.run_pipeline path\\to\\song.wav [--difficulty normal]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio_path", type=Path)
    parser.add_argument("--difficulty", default="normal", choices=["easy", "normal", "hard"])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if not args.audio_path.exists():
        print(f"audio file not found: {args.audio_path}", file=sys.stderr)
        return 2

    from app.pipeline.chart_builder import build_chart_from_audio

    data = args.audio_path.read_bytes()
    start = time.perf_counter()
    chart = build_chart_from_audio(
        audio_bytes=data,
        filename=args.audio_path.name,
        difficulty=args.difficulty,
    )
    elapsed = time.perf_counter() - start

    print(f"chart: {len(chart.notes)} notes, bpm={chart.audio.bpm:.2f}, duration={chart.audio.duration:.2f}s")
    print(f"pipeline took {elapsed:.2f}s")

    if args.out:
        args.out.write_text(chart.model_dump_json(indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
