"""Rule-based lane assignment.

Signals used:
  1. Spectral centroid: low onsets -> lanes 0-1, high onsets -> lanes 2-3.
     The split point is the MEDIAN centroid across the song so half of the
     onsets land on each side regardless of genre. A fixed cutoff (e.g. 1500
     Hz) drowns out one side on most pop music where the average centroid is
     well above any reasonable static threshold.
  2. Anti-clustering: don't put two notes in the same lane within HIT_WINDOW_S.
  3. Alternation: within a frequency half, alternate lanes so chains feel natural.
  4. Hand balance on streams: when consecutive onsets are tight (a "stream"
     in osu!mania terminology), prevent 3+ same-hand notes in a row by
     overriding the centroid band when needed. Lanes 0-1 are left hand,
     2-3 are right hand. See docs/DECISIONS.md.
  5. Chord notes: when an onset is in the top strength quantile and both bands
     have lane capacity, emit two simultaneous notes (one from each band).
     Same-`t` chord partners just work in the game loop because hit detection
     scopes by lane.

This is intentionally simple. Stage 2 playtesting decides if we need stems.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.pipeline.onset_detect import Onset

if TYPE_CHECKING:
    import numpy as np


# Don't place two notes within this window into the same lane.
HIT_WINDOW_S = 0.080

# Fallback cutoff used when there are too few onsets to compute a stable median.
FALLBACK_CUTOFF_HZ = 1500.0

# Onsets whose normalized strength is at or above this quantile become chord
# candidates. 0.88 produces roughly 10-12% chord rate after lane-capacity
# filtering on typical pop music; tune by playtesting.
CHORD_STRENGTH_QUANTILE = 0.88

# Below this onset count we never emit chords (the strength distribution is
# too noisy to pick a stable threshold).
MIN_ONSETS_FOR_CHORDS = 24

# Onsets closer than this in time count as part of a stream. Stream onsets
# get hand-balanced. The default 0.25s corresponds to 1/2 beat at 120 BPM;
# callers with a known beat period can override via beat_period_s.
DEFAULT_STREAM_GAP_S = 0.25

# After this many consecutive notes on the same hand, force a hand swap on
# the next stream onset if the other band has capacity.
MAX_SAME_HAND_STREAK = 2

# Maps lane index to hand. 0 = left hand (D/F), 1 = right hand (J/K).
_HAND_OF_LANE = (0, 0, 1, 1)


@dataclass
class RawNote:
    t: float
    lane: int
    type: str = "tap"
    duration: float | None = None


def assign_lanes(
    *,
    onsets: list[Onset],
    y: "np.ndarray",
    sr: int,
    beat_period_s: float | None = None,
) -> list[RawNote]:
    """Greedy left-to-right assignment honouring frequency, anti-cluster,
    hand-balance, and chord rules."""
    if not onsets:
        return []

    cutoff = _median_centroid(onsets)
    if cutoff <= 0 or len(onsets) < 8:
        cutoff = FALLBACK_CUTOFF_HZ

    chord_threshold = _chord_threshold(onsets)
    stream_gap_s = (beat_period_s * 0.5) if beat_period_s else DEFAULT_STREAM_GAP_S

    notes: list[RawNote] = []
    last_hit: list[float] = [-1e9, -1e9, -1e9, -1e9]
    low_toggle = 0
    high_toggle = 0
    same_hand_streak = 0
    last_hand = -1
    prev_t = -1e9

    for onset in onsets:
        low = onset.centroid_hz < cutoff
        t_rounded = round(float(onset.t), 4)
        in_stream = (onset.t - prev_t) < stream_gap_s

        # Try chord first when strong enough and both bands have capacity.
        if onset.strength >= chord_threshold:
            low_pick = _pick_band(0, 1, low_toggle, last_hit, onset.t)
            high_pick = _pick_band(2, 3, high_toggle, last_hit, onset.t)
            if low_pick is not None and high_pick is not None:
                low_lane, low_used_preferred = low_pick
                high_lane, high_used_preferred = high_pick
                notes.append(RawNote(t=t_rounded, lane=low_lane, type="tap"))
                notes.append(RawNote(t=t_rounded, lane=high_lane, type="tap"))
                last_hit[low_lane] = onset.t
                last_hit[high_lane] = onset.t
                if low_used_preferred:
                    low_toggle = 1 - low_toggle
                if high_used_preferred:
                    high_toggle = 1 - high_toggle
                # Chord uses both hands; reset streak.
                same_hand_streak = 0
                last_hand = -1
                prev_t = onset.t
                continue

        # Hand-balance: during a stream, if the natural band would make 3+
        # same-hand notes in a row, try the other band first.
        natural_hand = 0 if low else 1
        prefer_other = (
            in_stream
            and same_hand_streak >= MAX_SAME_HAND_STREAK
            and natural_hand == last_hand
        )
        prefer_low = (not prefer_other) if low else prefer_other

        primary = (0, 1) if prefer_low else (2, 3)
        primary_toggle = low_toggle if prefer_low else high_toggle
        pick = _pick_band(primary[0], primary[1], primary_toggle, last_hit, onset.t)
        if pick is None:
            other = (2, 3) if prefer_low else (0, 1)
            other_toggle = high_toggle if prefer_low else low_toggle
            pick = _pick_band(other[0], other[1], other_toggle, last_hit, onset.t)
            if pick is None:
                continue  # all four lanes too recent, drop this onset
            chosen, used_preferred = pick
            if prefer_low:
                if used_preferred:
                    high_toggle = 1 - high_toggle
            else:
                if used_preferred:
                    low_toggle = 1 - low_toggle
        else:
            chosen, used_preferred = pick
            if prefer_low:
                if used_preferred:
                    low_toggle = 1 - low_toggle
            else:
                if used_preferred:
                    high_toggle = 1 - high_toggle

        notes.append(RawNote(t=t_rounded, lane=chosen, type="tap"))
        last_hit[chosen] = onset.t

        chosen_hand = _HAND_OF_LANE[chosen]
        if chosen_hand == last_hand:
            same_hand_streak += 1
        else:
            same_hand_streak = 1
        last_hand = chosen_hand
        prev_t = onset.t

    return notes


def _pick_band(
    lane_a: int,
    lane_b: int,
    toggle: int,
    last_hit: list[float],
    onset_t: float,
) -> tuple[int, bool] | None:
    """Pick a lane from a 2-lane band. Returns (chosen, used_preferred) or None.

    Preferred is the toggle-selected lane; used_preferred=False means we had to
    fall back to the alternate. Caller uses this flag to decide whether to flip
    the band's toggle for the next onset.
    """
    preferred = lane_a if toggle == 0 else lane_b
    alternate = lane_b if toggle == 0 else lane_a
    if onset_t - last_hit[preferred] >= HIT_WINDOW_S:
        return preferred, True
    if onset_t - last_hit[alternate] >= HIT_WINDOW_S:
        return alternate, False
    return None


def _chord_threshold(onsets: list[Onset]) -> float:
    """Strength at the CHORD_STRENGTH_QUANTILE percentile, or +inf if too few onsets."""
    if len(onsets) < MIN_ONSETS_FOR_CHORDS:
        return float("inf")
    strengths = sorted(o.strength for o in onsets)
    idx = int(len(strengths) * CHORD_STRENGTH_QUANTILE)
    return strengths[min(idx, len(strengths) - 1)]


def _median_centroid(onsets: list[Onset]) -> float:
    vals = sorted(o.centroid_hz for o in onsets)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else 0.5 * (vals[mid - 1] + vals[mid])
