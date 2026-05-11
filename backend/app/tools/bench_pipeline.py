"""Benchmark the audio pipeline against CLAUDE.md non-negotiables.

Targets:
  - First-time generation under 60s for a 4-minute song.
  - Cached generation under 2s.

Synthesizes ~4 minutes of mixed content (clicks at 120 BPM plus two background
tones) so beat tracking and onset detection have real work to do, then runs
the full pipeline cold and warm and prints per-stage timing.
"""

from __future__ import annotations

import io
import time

import numpy as np
import soundfile as sf

from app.pipeline.beat_track import detect_beats
from app.pipeline.chart_builder import build_chart_from_audio, load_audio_to_mono
from app.pipeline.difficulty import shape_difficulty
from app.pipeline.lane_assign import assign_lanes
from app.pipeline.onset_detect import detect_onsets


def synth_mixed_audio(seconds: float, sr: int = 22050) -> bytes:
    n = int(seconds * sr)
    rng = np.random.default_rng(0)
    y = np.zeros(n, dtype=np.float32)
    # Backbeat at 120 BPM with mild swing.
    period = 60.0 / 120.0
    click_len = int(0.02 * sr)
    env = np.exp(-np.linspace(0, 6, click_len)).astype(np.float32)
    t = 0.0
    while t < seconds:
        start = int(t * sr)
        end = min(start + click_len, n)
        y[start:end] += env[: end - start] * rng.standard_normal(end - start).astype(np.float32) * 0.6
        t += period
    # Two background tones to give the spectral centroid something to work with.
    t_axis = np.arange(n, dtype=np.float32) / sr
    y += 0.08 * np.sin(2 * np.pi * 220 * t_axis).astype(np.float32)
    y += 0.05 * np.sin(2 * np.pi * 880 * t_axis).astype(np.float32)
    # Clip to safe range.
    y = np.clip(y, -1.0, 1.0)
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def stage_timed(label: str, fn):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    print(f"  {label:24s} {dt*1000:8.1f} ms")
    return out, dt


def main() -> None:
    print("Synthesizing 4-min mixed audio at 22050 Hz...")
    audio_bytes = synth_mixed_audio(seconds=240.0)
    print(f"  audio size: {len(audio_bytes) / 1024:.0f} KB")

    print("\n--- cold call (first import incurs numba JIT) ---")
    t0 = time.perf_counter()
    (y, sr), _ = stage_timed("decode + resample", lambda: load_audio_to_mono(audio_bytes))
    beats, _ = stage_timed("beat tracking", lambda: detect_beats(y=y, sr=sr))
    onsets, _ = stage_timed("onset detection", lambda: detect_onsets(y=y, sr=sr))
    raw_notes, _ = stage_timed("lane assignment", lambda: assign_lanes(onsets=onsets, y=y, sr=sr))
    notes, _ = stage_timed(
        "difficulty shaping",
        lambda: shape_difficulty(notes=raw_notes, difficulty="normal", beats=beats.beats),
    )
    cold_total = time.perf_counter() - t0
    print(f"  COLD total              {cold_total*1000:8.1f} ms")
    print(f"  produced {len(notes)} notes, bpm {beats.bpm:.1f}")

    print("\n--- warm call (numba already compiled) ---")
    t0 = time.perf_counter()
    _ = build_chart_from_audio(audio_bytes=audio_bytes, filename="bench.wav", difficulty="normal")
    warm_total = time.perf_counter() - t0
    print(f"  WARM total              {warm_total*1000:8.1f} ms")

    print("\n--- targets ---")
    print(f"  cold <= 60000 ms ? {'PASS' if cold_total <= 60 else 'FAIL'} ({cold_total:.1f}s)")
    print(f"  warm <=  2000 ms ? {'PASS' if warm_total <= 2 else 'FAIL'} ({warm_total*1000:.0f}ms)")


if __name__ == "__main__":
    main()
