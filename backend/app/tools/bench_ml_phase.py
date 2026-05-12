"""Benchmark and A/B the ML phases described in docs/ML_PLAN.md.

Runs the full pipeline twice on the same audio:
  - Heuristic baseline (use_beat_this=False, use_demucs=False)
  - ML run with the flags chosen by --beat-this / --demucs

Reports per-stage timing, beat counts, note counts, chord rate, and
downbeat coverage. Prints PASS/FAIL against the ML_PLAN validation
criteria so a regression jumps out immediately.

Run:
  python -m app.tools.bench_ml_phase --wav path\\to\\song.wav
  python -m app.tools.bench_ml_phase --synth        # 4-min synthetic
  python -m app.tools.bench_ml_phase --beat-this    # turn Beat This! on for the ML run
  python -m app.tools.bench_ml_phase --baseline-only

Per-call backend overrides are passed directly to build_chart_from_audio
(no env-var juggling, no module reloads).
"""

from __future__ import annotations

import argparse
import io
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from app.pipeline.beat_track import detect_beats
from app.pipeline.chart_builder import build_chart_from_audio, load_audio_to_mono


@dataclass
class Result:
    label: str
    bpm: float
    n_beats: int
    n_downbeats: int
    n_notes: int
    n_chords: int  # notes that share their `t` with at least one other note
    cold_total_s: float
    warm_total_s: float
    beat_source: str


def _synth_mixed_audio(seconds: float = 240.0, sr: int = 22050) -> bytes:
    """Same synthetic mix used by bench_pipeline. 120 BPM clicks + drone."""
    n = int(seconds * sr)
    rng = np.random.default_rng(0)
    y = np.zeros(n, dtype=np.float32)
    period = 60.0 / 120.0
    click_len = int(0.02 * sr)
    env = np.exp(-np.linspace(0, 6, click_len)).astype(np.float32)
    t = 0.0
    while t < seconds:
        start = int(t * sr)
        end = min(start + click_len, n)
        y[start:end] += env[: end - start] * rng.standard_normal(end - start).astype(np.float32) * 0.6
        t += period
    t_axis = np.arange(n, dtype=np.float32) / sr
    y += 0.08 * np.sin(2 * np.pi * 220 * t_axis).astype(np.float32)
    y += 0.05 * np.sin(2 * np.pi * 880 * t_axis).astype(np.float32)
    y = np.clip(y, -1.0, 1.0)
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _load_audio(args: argparse.Namespace) -> bytes:
    if args.wav:
        return Path(args.wav).read_bytes()
    return _synth_mixed_audio(seconds=args.seconds)


def _run_once(
    label: str,
    audio_bytes: bytes,
    *,
    difficulty: str = "normal",
    use_beat_this: bool = False,
    use_demucs: bool = False,
) -> Result:
    t0 = time.perf_counter()
    chart = build_chart_from_audio(
        audio_bytes=audio_bytes,
        filename="bench.wav",
        difficulty=difficulty,
        use_beat_this=use_beat_this,
        use_demucs=use_demucs,
    )
    cold_total = time.perf_counter() - t0

    t1 = time.perf_counter()
    build_chart_from_audio(
        audio_bytes=audio_bytes,
        filename="bench.wav",
        difficulty=difficulty,
        use_beat_this=use_beat_this,
        use_demucs=use_demucs,
    )
    warm_total = time.perf_counter() - t1

    by_t: dict[float, int] = {}
    for n in chart.notes:
        by_t[n.t] = by_t.get(n.t, 0) + 1
    chord_count = sum(c for c in by_t.values() if c >= 2)

    # Pull beat info separately to report source / downbeat counts that
    # the Chart object doesn't carry.
    y, sr = load_audio_to_mono(audio_bytes)
    info = detect_beats(
        y=y, sr=sr,
        content_hash=chart.audio.contentHash,
        use_beat_this=use_beat_this,
    )

    return Result(
        label=label,
        bpm=info.bpm,
        n_beats=len(info.beats),
        n_downbeats=len(info.downbeats or []),
        n_notes=len(chart.notes),
        n_chords=chord_count,
        cold_total_s=cold_total,
        warm_total_s=warm_total,
        beat_source=info.source,
    )


def _print_result(r: Result) -> None:
    print(f"\n--- {r.label} ---")
    print(f"  beat tracker:   {r.beat_source}")
    print(f"  bpm:            {r.bpm:.1f}")
    print(f"  beats:          {r.n_beats}")
    print(f"  downbeats:      {r.n_downbeats}")
    print(f"  notes:          {r.n_notes}")
    rate = 100 * r.n_chords / max(r.n_notes, 1)
    print(f"  chord notes:    {r.n_chords}  ({rate:.1f}%)")
    print(f"  cold total:     {r.cold_total_s:.2f}s")
    print(f"  warm total:     {r.warm_total_s:.2f}s")


def _print_targets(r: Result) -> None:
    chord_rate = 100 * r.n_chords / max(r.n_notes, 1)
    cold_pass = "PASS" if r.cold_total_s <= 90 else "FAIL"
    # The 2s budget in CLAUDE.md / ML_PLAN refers to a chart-cache HIT at
    # the API layer (return existing chart from SQLite). The bench's
    # "warm" is a second full pipeline run, which is slower because the
    # chart cache isn't in play here. We compare warm vs cold to detect
    # caching regressions (beat cache, model warmup) without holding warm
    # to the 2s number.
    speedup = r.cold_total_s / max(r.warm_total_s, 1e-3)
    chord_pass = "PASS" if 3.0 <= chord_rate <= 8.0 else "WARN"
    print("\n--- ML_PLAN validation gates ---")
    print(f"  cold <= 90s     : {cold_pass} ({r.cold_total_s:.1f}s)")
    print(f"  warm vs cold    : {speedup:.1f}x speedup (warm {r.warm_total_s:.2f}s)")
    print(f"  chord rate 3-8% : {chord_pass} ({chord_rate:.1f}%)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--wav", type=str, default=None, help="Path to a WAV file.")
    p.add_argument("--seconds", type=float, default=240.0,
                   help="Length of synthetic audio if --wav is not given.")
    p.add_argument("--difficulty", default="normal")
    p.add_argument("--baseline-only", action="store_true",
                   help="Skip the ML run, only benchmark the heuristic baseline.")
    p.add_argument("--beat-this", action="store_true",
                   help="Turn USE_BEAT_THIS on for the ML run.")
    p.add_argument("--demucs", action="store_true",
                   help="Turn USE_DEMUCS on for the ML run.")
    args = p.parse_args()

    audio_bytes = _load_audio(args)
    print(f"audio size: {len(audio_bytes) / 1024:.0f} KB")

    baseline = _run_once(
        "heuristic baseline", audio_bytes,
        difficulty=args.difficulty,
        use_beat_this=False, use_demucs=False,
    )
    _print_result(baseline)

    if args.baseline_only:
        _print_targets(baseline)
        return

    if not (args.beat_this or args.demucs):
        print("\n(no --beat-this / --demucs flag set; skipping ML comparison)")
        _print_targets(baseline)
        return

    ml_run = _run_once(
        f"ML run (beat_this={args.beat_this} demucs={args.demucs})",
        audio_bytes,
        difficulty=args.difficulty,
        use_beat_this=args.beat_this,
        use_demucs=args.demucs,
    )
    _print_result(ml_run)
    _print_targets(ml_run)

    print("\n--- delta vs heuristic baseline ---")
    print(f"  notes:     {ml_run.n_notes - baseline.n_notes:+d}")
    print(f"  chords:    {ml_run.n_chords - baseline.n_chords:+d}")
    print(f"  cold:      {ml_run.cold_total_s - baseline.cold_total_s:+.2f}s")
    print(f"  warm:      {ml_run.warm_total_s - baseline.warm_total_s:+.2f}s")


if __name__ == "__main__":
    main()
