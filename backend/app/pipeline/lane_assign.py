"""Rule-based lane assignment.

Signals used:
  1. Spectral centroid: low onsets -> lanes 0-1, high onsets -> lanes 2-3.
     The split point is the MEDIAN centroid across the song so half of the
     onsets land on each side regardless of genre. A fixed cutoff (e.g. 1500
     Hz) drowns out one side on most pop music where the average centroid is
     well above any reasonable static threshold.
  2. Anti-clustering: don't put two notes in the same lane within HIT_WINDOW_S.
  3. Alternation: within a frequency half, alternate lanes so chains feel natural.

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


@dataclass
class RawNote:
    t: float
    lane: int
    type: str = "tap"
    duration: float | None = None


def assign_lanes(*, onsets: list[Onset], y: "np.ndarray", sr: int) -> list[RawNote]:
    """Greedy left-to-right assignment honouring frequency and anti-cluster rules."""
    if not onsets:
        return []

    # Pick the cutoff so roughly half the onsets land on each side. Falls back
    # to a static value when the median itself would be degenerate (very few
    # onsets or a song with a tiny centroid spread).
    cutoff = _median_centroid(onsets)
    if cutoff <= 0 or len(onsets) < 8:
        cutoff = FALLBACK_CUTOFF_HZ

    notes: list[RawNote] = []
    # Per-lane last-hit time, indexed by lane number.
    last_hit: list[float] = [-1e9, -1e9, -1e9, -1e9]
    # Alternation toggles per band.
    low_toggle = 0   # 0 -> lane 0, 1 -> lane 1
    high_toggle = 0  # 0 -> lane 2, 1 -> lane 3

    for onset in onsets:
        low = onset.centroid_hz < cutoff
        candidates = (0, 1) if low else (2, 3)
        toggle = low_toggle if low else high_toggle
        preferred = candidates[toggle]
        alternate = candidates[1 - toggle]

        chosen: int | None = None
        for lane in (preferred, alternate):
            if onset.t - last_hit[lane] >= HIT_WINDOW_S:
                chosen = lane
                break

        if chosen is None:
            # Both candidates too recent. Try the other band as a relief valve.
            other_band = (2, 3) if low else (0, 1)
            for lane in other_band:
                if onset.t - last_hit[lane] >= HIT_WINDOW_S:
                    chosen = lane
                    break

        if chosen is None:
            # All four lanes too recent. Drop this onset; we'd clip a real hit otherwise.
            continue

        notes.append(RawNote(t=round(float(onset.t), 4), lane=chosen, type="tap"))
        last_hit[chosen] = onset.t

        if low and chosen in (0, 1):
            low_toggle = 1 - low_toggle
        elif not low and chosen in (2, 3):
            high_toggle = 1 - high_toggle

    return notes


def _median_centroid(onsets: list[Onset]) -> float:
    vals = sorted(o.centroid_hz for o in onsets)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else 0.5 * (vals[mid - 1] + vals[mid])
