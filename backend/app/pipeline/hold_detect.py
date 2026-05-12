"""Hold detection: convert taps to holds when audio energy sustains.

The lane assigner emits everything as `type="tap"`. For songs with sustained
vocals or held synth pads, a hold note is the correct osu!mania convention.

Two-pass approach:
  1. For each tap, walk RMS forward and measure how long energy stays above
     SUSTAIN_THRESHOLD of the onset's own peak RMS. Bounded by the beat-aware
     hold-length cap and the next same-lane note's time (with a safety margin).
  2. Sort candidates by sustain duration descending. Promote only the top
     MAX_HOLD_RATIO of total notes. The cap is what actually keeps charts
     playable on continuous music; on a song where everything sustains for
     hundreds of ms, the cap picks the longest-sustaining ones.
  3. Snap each accepted hold's end time to the nearest beat or half-beat in
     the chart's beat grid. Holds that land on a beat read "cleanly" in
     gameplay; non-snapped ends feel arbitrary and disconnected from the
     music.

Hold-duration sanity (the recent user complaint):
  - Max hold = MAX_HOLD_BEATS beats (default 2.0), so at 172 BPM a hold
    cannot exceed ~0.7s rather than the previous fixed 2.0s.
  - Min hold = max(MIN_HOLD_S, 0.5 * beat_period). Anything shorter wouldn't
    feel like a hold during play.

Earlier attempt: a local-baseline check that demanded onset peak exceed
the surrounding RMS by a margin. That over-filtered on pop music (where
the song is loud everywhere) and produced 0 holds. The pure rate cap
gives the desired ~5% density without that pathology.

False-negatives (real holds left as taps) feel like a missing feature.
False-positives (taps wrongly promoted) interrupt finger play and crowd
the screen, which is what just bit us. Bias toward false-negatives.
"""

from __future__ import annotations

from bisect import bisect_left
from typing import TYPE_CHECKING

from app.pipeline.lane_assign import RawNote

if TYPE_CHECKING:
    import numpy as np


# Absolute floor on hold duration regardless of beat math. A hold shorter
# than this would feel like a normal tap during play.
MIN_HOLD_S = 0.20
# Absolute hard cap on hold duration even if the music has very slow beats.
MAX_HOLD_S = 1.5
# Beat-relative cap: a hold can be at most this many beats long. At 120 BPM
# this gives 1.0s; at 172 BPM it gives ~0.7s; at 60 BPM it gives 2.0s
# (further clamped by MAX_HOLD_S above).
MAX_HOLD_BEATS = 2.0
# Beat-relative floor: a hold must be at least this many beats long. At
# higher tempos this can be slightly shorter than MIN_HOLD_S, in which case
# MIN_HOLD_S wins.
MIN_HOLD_BEATS = 0.5
# Fraction of the onset's peak RMS that the sustain must clear.
SUSTAIN_THRESHOLD = 0.75
# Cap total hold rate. Sorted by sustain duration descending; only the top
# MAX_HOLD_RATIO of taps become holds, regardless of how many would otherwise
# qualify. Real osu!mania charts run roughly 5-15% holds; we bias low so
# charts feel mostly-taps with rare meaningful holds.
MAX_HOLD_RATIO = 0.05
# Safety margin before the next same-lane note so the tail doesn't visually
# overlap the next head.
LANE_NEXT_SAFETY_S = 0.05


def detect_holds(
    *,
    notes: list[RawNote],
    y: "np.ndarray",
    sr: int,
    hop_length: int = 512,
    beat_period_s: float | None = None,
    beats_s: list[float] | None = None,
) -> list[RawNote]:
    """Return a new list with up to MAX_HOLD_RATIO of taps promoted to holds.

    When `beat_period_s` is provided, the min and max hold duration are
    derived from MIN_HOLD_BEATS and MAX_HOLD_BEATS. When `beats_s` is
    provided too, accepted hold ends are snapped to the nearest beat or
    half-beat boundary in that list.
    """
    if not notes:
        return notes

    import librosa  # noqa: WPS433

    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    n_frames = int(len(rms))
    if n_frames == 0:
        return notes

    next_in_lane = _next_time_in_same_lane(notes)

    frames_per_second = sr / hop_length
    min_hold_s, max_hold_s = _bounded_hold_window(beat_period_s)
    min_hold_frames = max(1, int(min_hold_s * frames_per_second))
    peak_window_frames = max(1, int(0.05 * frames_per_second))

    # Pre-compute the half-beat snap grid if we have beats. We use eighth
    # notes (half-beats) as the snap resolution because quarter-note hold
    # ends are too coarse and miss obvious off-beat releases.
    snap_grid: list[float] | None = None
    if beats_s and len(beats_s) >= 2:
        snap_grid = _half_beat_grid(beats_s)

    candidates: list[tuple[int, float]] = []  # (index_into_notes, duration_s)
    for idx, n in enumerate(notes):
        if n.type != "tap":
            continue

        horizon = min(n.t + max_hold_s, next_in_lane[idx] - LANE_NEXT_SAFETY_S)
        if horizon - n.t < min_hold_s:
            continue

        start_frame = int(n.t * frames_per_second)
        end_frame = min(int(horizon * frames_per_second), n_frames - 1)
        if end_frame - start_frame < min_hold_frames:
            continue

        peak_rms = float(rms[start_frame : start_frame + peak_window_frames].max())
        if peak_rms <= 0:
            continue

        threshold = peak_rms * SUSTAIN_THRESHOLD

        sustain_end_frame = start_frame
        for f in range(start_frame + 1, end_frame + 1):
            if rms[f] >= threshold:
                sustain_end_frame = f
            else:
                break

        duration = (sustain_end_frame - start_frame) / frames_per_second
        if duration >= min_hold_s:
            candidates.append((idx, duration))

    if not candidates:
        return notes

    max_holds = max(1, int(len(notes) * MAX_HOLD_RATIO))
    candidates.sort(key=lambda c: c[1], reverse=True)
    keep_indices = {idx for idx, _ in candidates[:max_holds]}
    durations = dict(candidates)

    out: list[RawNote] = []
    for idx, n in enumerate(notes):
        if idx in keep_indices:
            raw_duration = durations[idx]
            duration = _snap_duration(
                start_t=n.t,
                raw_duration=raw_duration,
                snap_grid=snap_grid,
                min_hold_s=min_hold_s,
                max_hold_s=max_hold_s,
                horizon=next_in_lane[idx] - LANE_NEXT_SAFETY_S,
            )
            out.append(
                RawNote(
                    t=n.t,
                    lane=n.lane,
                    type="hold",
                    duration=round(duration, 3),
                ),
            )
        else:
            out.append(n)
    return out


def _bounded_hold_window(beat_period_s: float | None) -> tuple[float, float]:
    """Compute the per-song min and max hold duration."""
    if beat_period_s is None or beat_period_s <= 0:
        return MIN_HOLD_S, MAX_HOLD_S
    min_hold = max(MIN_HOLD_S, MIN_HOLD_BEATS * beat_period_s)
    max_hold = min(MAX_HOLD_S, MAX_HOLD_BEATS * beat_period_s)
    # Guard against pathological tempos (very slow songs) where the beat-
    # derived min could exceed the beat-derived max.
    if max_hold < min_hold:
        max_hold = min_hold * 1.5
    return min_hold, max_hold


def _half_beat_grid(beats_s: list[float]) -> list[float]:
    """Insert a half-beat point between each consecutive pair of beats."""
    grid: list[float] = []
    for i in range(len(beats_s) - 1):
        grid.append(beats_s[i])
        grid.append((beats_s[i] + beats_s[i + 1]) / 2.0)
    grid.append(beats_s[-1])
    return grid


def _snap_duration(
    *,
    start_t: float,
    raw_duration: float,
    snap_grid: list[float] | None,
    min_hold_s: float,
    max_hold_s: float,
    horizon: float,
) -> float:
    """Snap a hold's end time to the nearest grid point INSIDE the allowed window.

    The window is [start + min_hold_s, min(start + max_hold_s, horizon)].
    Picking a grid point that's outside the window and then clamping would
    defeat the snap. We instead consider only grid points within the window
    and pick the one closest to the natural end. If none exist, fall back to
    the natural end clamped to the window.
    """
    clamped_max = min(start_t + max_hold_s, horizon)
    window_lo = start_t + min_hold_s
    window_hi = max(clamped_max, window_lo)
    natural_end = max(window_lo, min(start_t + raw_duration, window_hi))

    if not snap_grid:
        return natural_end - start_t

    lo_pos = bisect_left(snap_grid, window_lo)
    hi_pos = bisect_left(snap_grid, window_hi)
    if hi_pos < len(snap_grid) and snap_grid[hi_pos] == window_hi:
        hi_pos += 1
    in_window = snap_grid[lo_pos:hi_pos]
    if not in_window:
        return natural_end - start_t

    snapped_end = min(in_window, key=lambda x: abs(x - natural_end))
    return snapped_end - start_t


def _next_time_in_same_lane(notes: list[RawNote]) -> list[float]:
    """For each note, the t of the next note in the same lane (or +inf)."""
    next_t: list[float] = [1e9] * len(notes)
    last_seen: dict[int, int] = {}
    for i in range(len(notes) - 1, -1, -1):
        n = notes[i]
        if n.lane in last_seen:
            next_t[i] = notes[last_seen[n.lane]].t
        last_seen[n.lane] = i
    return next_t
