"""Prove that generated notes are synced to the source audio.

Produces three artifacts for a given audio source:

  1. A JSON report on stdout: bpm, content hash, first N notes with timestamps,
     lane distribution.
  2. clicks_only.wav: pure click track. Each chart note becomes a short tick.
     Play it side-by-side with the original to confirm timing.
  3. mix.wav: original audio at 30% volume mixed with the click track at full
     volume. The most convincing artifact. If the clicks land on perceived
     musical events, the chart is synced.

Usage:
    python -m app.tools.verify_sync --video-id dQw4w9WgXcQ --out ./verify_out/
    python -m app.tools.verify_sync --audio path\\to\\song.wav     --out ./verify_out/

The video-id path requires BACKEND_ALLOW_YTDLP=1 and yt-dlp + ffmpeg installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from app.pipeline.beat_track import detect_beats
from app.pipeline.chart_builder import build_chart_from_audio


def _click_buffer(sr: int, freq: float = 2000.0, duration_s: float = 0.04) -> np.ndarray:
    n = int(sr * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False, dtype=np.float32)
    env = np.exp(-np.linspace(0, 8, n)).astype(np.float32)
    return (np.sin(2 * np.pi * freq * t).astype(np.float32) * env * 0.6)


def render_click_track(times: list[float], sr: int, total_samples: int) -> np.ndarray:
    """Single channel click track at sample rate sr, length total_samples."""
    out = np.zeros(total_samples, dtype=np.float32)
    click = _click_buffer(sr)
    for t in times:
        s = int(round(t * sr))
        e = min(s + len(click), total_samples)
        if s < 0 or s >= total_samples:
            continue
        out[s:e] += click[: e - s]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--video-id", help="YouTube video id (requires BACKEND_ALLOW_YTDLP=1)")
    src.add_argument("--audio", type=Path, help="Local WAV/MP3 path")
    parser.add_argument("--difficulty", default="normal", choices=["easy", "normal", "hard"])
    parser.add_argument("--out", type=Path, default=Path("verify_out"))
    parser.add_argument("--n-preview", type=int, default=20, help="how many notes to dump as JSON")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    if args.video_id:
        os.environ.setdefault("BACKEND_ALLOW_YTDLP", "1")
        from app.audio.ingest import fetch_videoid

        ingested = fetch_videoid(args.video_id)
        audio_path = ingested.path
        audio_bytes = audio_path.read_bytes()
        source_label = "youtube"
    else:
        if not args.audio.exists():
            print(f"audio file not found: {args.audio}", file=sys.stderr)
            return 2
        audio_path = args.audio
        audio_bytes = args.audio.read_bytes()
        source_label = "upload"

    t0 = time.perf_counter()
    chart = build_chart_from_audio(
        audio_bytes=audio_bytes,
        filename=audio_path.name,
        difficulty=args.difficulty,
        audio_source=source_label,
        video_id=args.video_id,
    )
    pipeline_s = time.perf_counter() - t0

    # Decode audio for the click-mix render.
    y, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    total = len(y)

    note_times = [n.t for n in chart.notes]
    beat_info = detect_beats(y=y, sr=sr)

    clicks = render_click_track(note_times, sr=sr, total_samples=total)
    beat_clicks = render_click_track(beat_info.beats, sr=sr, total_samples=total)
    mix = np.clip(y * 0.30 + clicks, -1.0, 1.0)

    clicks_path = args.out / "clicks_only.wav"
    beats_path = args.out / "beats_only.wav"
    mix_path = args.out / "mix.wav"
    sf.write(clicks_path, clicks, sr, subtype="PCM_16")
    sf.write(beats_path, beat_clicks, sr, subtype="PCM_16")
    sf.write(mix_path, mix, sr, subtype="PCM_16")

    report = {
        "audio": {
            "source": source_label,
            "video_id": args.video_id,
            "duration_s": float(chart.audio.duration),
            "sample_rate": int(sr),
            "content_hash": chart.audio.contentHash,
        },
        "pipeline": {
            "version": chart.metadata.pipelineVersion,
            "elapsed_s": round(pipeline_s, 3),
            "bpm_detected": float(chart.audio.bpm),
            "beats_detected": len(beat_info.beats),
            "note_count": len(chart.notes),
            "lanes": {
                str(i): sum(1 for n in chart.notes if n.lane == i) for i in range(4)
            },
        },
        "first_notes": [
            {"t": n.t, "lane": n.lane, "type": n.type}
            for n in chart.notes[: args.n_preview]
        ],
        "first_beats_s": [round(b, 4) for b in beat_info.beats[: args.n_preview]],
        "artifacts": {
            "clicks_only_wav": str(clicks_path),
            "beats_only_wav": str(beats_path),
            "mix_wav": str(mix_path),
        },
        "how_to_verify": (
            "Play mix.wav. The original song at 30% volume is overlaid with "
            "click ticks at every chart note time. If the ticks line up with "
            "perceived musical events (kicks, snares, vocal hits), the chart "
            "is genuinely synced to the audio. beats_only.wav contains the "
            "BPM-tracker output; clicks_only.wav contains the note times."
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
