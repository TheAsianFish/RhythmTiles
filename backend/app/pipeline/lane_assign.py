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

# Minimum spectral centroid for an onset to qualify as a chord-accent.
# 2800 Hz puts the cutoff above most kick/snare fundamentals and into
# the cymbal/hi-hat range, where chord stacks feel musical (cymbal
# accents, bright downbeats). Tuned together with chord_quantile to
# land chord rate in 3-6% on typical pop tracks.
CHORD_ACCENT_MIN_CENTROID_HZ = 2800.0

# Maximum time gap between an onset and the nearest downbeat for the
# onset to count as a downbeat accent. Beat This!'s downbeat times are
# usually within a few ms of the true bar start, but our onsets can be
# backtracked ~10-30ms earlier; widen the window so we don't miss the
# chord on the very note that the downbeat names.
DOWNBEAT_ACCENT_TOLERANCE_S = 0.050

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
    # Carried from the source Onset so the difficulty filter can rank notes
    # by musical importance instead of by spacing alone. Default 1.0 keeps
    # synthetic notes (e.g. tests, beat-fill) on equal footing without
    # requiring callers to thread it through.
    strength: float = 1.0


def assign_lanes(
    *,
    onsets: list[Onset],
    y: "np.ndarray",
    sr: int,
    beat_period_s: float | None = None,
    chord_quantile: float = CHORD_STRENGTH_QUANTILE,
    downbeats: list[float] | None = None,
) -> list[RawNote]:
    """Greedy left-to-right assignment honouring frequency, anti-cluster,
    hand-balance, stem source (when present on the onset), and chord rules.

    `chord_quantile` lets the caller tune chord density per difficulty:
    higher values (closer to 1.0) emit fewer chords, lower values emit more.
    Default matches the module-level CHORD_STRENGTH_QUANTILE.

    `downbeats` is the list of bar-start times from Beat This! (Phase 1 of
    docs/ML_PLAN.md). When provided, any onset within
    DOWNBEAT_ACCENT_TOLERANCE_S of a downbeat is treated as a chord-accent
    regardless of strength/centroid: a downbeat is a real musical accent and
    a chord stack there matches what mappers do. Onsets away from downbeats
    still follow the strength+centroid accent rule.
    """
    if not onsets:
        return []

    cutoff = _median_centroid(onsets)
    if cutoff <= 0 or len(onsets) < 8:
        cutoff = FALLBACK_CUTOFF_HZ

    # Chord emission: only fires on ACCENT events - top-quantile strength
    # AND high spectral centroid (cymbal/snare/crash territory) OR a real
    # downbeat from Beat This! when available. The old rule fired on any
    # strong onset, which auto-paired single piano notes and drum hits
    # into fake chords. The new rule lands chord stacks mostly on cymbal
    # crashes, bright accent moments, and real bar starts. Target rate:
    # 3-8% of notes in chord groups. The caller can pass chord_quantile=1.0
    # to disable strength-based chord emission entirely; downbeat-based
    # chords still fire when downbeats are provided.
    chord_threshold = _chord_threshold(onsets, quantile=chord_quantile)
    downbeat_set = sorted(downbeats) if downbeats else None
    stream_gap_s = (beat_period_s * 0.5) if beat_period_s else DEFAULT_STREAM_GAP_S

    notes: list[RawNote] = []
    last_hit: list[float] = [-1e9, -1e9, -1e9, -1e9]
    low_toggle = 0
    high_toggle = 0
    same_hand_streak = 0
    last_hand = -1
    prev_t = -1e9

    # Index pointer into downbeat_set; we walk onsets in time order so
    # this only moves forward (avoids an O(n*m) scan per onset).
    db_idx = 0
    n_downbeats = len(downbeat_set) if downbeat_set else 0

    for onset in onsets:
        # Band routing is purely centroid-driven. Earlier versions routed
        # drum-stem onsets to the low band and vocal-stem onsets to the
        # high band, but that locked each hand to one instrument and
        # killed per-lane musical variety. Per-stem onset detection
        # still produces cleaner timing (Demucs is worth running), but
        # the stem tag does NOT decide which lane the note lands in.
        low = _band_from_onset(onset, cutoff=cutoff)
        t_rounded = round(float(onset.t), 4)
        in_stream = (onset.t - prev_t) < stream_gap_s

        # Walk db_idx forward to the nearest downbeat at or before this
        # onset; check both that and the next one for the smallest delta.
        on_downbeat = False
        if downbeat_set:
            while db_idx + 1 < n_downbeats and downbeat_set[db_idx + 1] <= onset.t:
                db_idx += 1
            cand = abs(downbeat_set[db_idx] - onset.t)
            if db_idx + 1 < n_downbeats:
                cand = min(cand, abs(downbeat_set[db_idx + 1] - onset.t))
            on_downbeat = cand <= DOWNBEAT_ACCENT_TOLERANCE_S

        # Accent-chord: top-quantile strength AND high centroid (bright
        # event = cymbal / crash / bright accent), OR a real downbeat from
        # the ML beat tracker. The strength/centroid path stays in for the
        # librosa fallback (no downbeats) and for accents between bars.
        is_accent = on_downbeat or (
            onset.strength >= chord_threshold
            and onset.centroid_hz >= CHORD_ACCENT_MIN_CENTROID_HZ
        )
        if is_accent:
            low_pick = _pick_band(0, 1, low_toggle, last_hit, onset.t)
            high_pick = _pick_band(2, 3, high_toggle, last_hit, onset.t)
            if low_pick is not None and high_pick is not None:
                low_lane, low_used_preferred = low_pick
                high_lane, high_used_preferred = high_pick
                notes.append(RawNote(t=t_rounded, lane=low_lane, type="tap", strength=float(onset.strength)))
                notes.append(RawNote(t=t_rounded, lane=high_lane, type="tap", strength=float(onset.strength)))
                last_hit[low_lane] = onset.t
                last_hit[high_lane] = onset.t
                if low_used_preferred:
                    low_toggle = 1 - low_toggle
                if high_used_preferred:
                    high_toggle = 1 - high_toggle
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

        notes.append(RawNote(t=t_rounded, lane=chosen, type="tap", strength=float(onset.strength)))
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


def _chord_threshold(
    onsets: list[Onset],
    *,
    quantile: float = CHORD_STRENGTH_QUANTILE,
) -> float:
    """Strength at the given quantile, or +inf if too few onsets."""
    if len(onsets) < MIN_ONSETS_FOR_CHORDS:
        return float("inf")
    strengths = sorted(o.strength for o in onsets)
    idx = int(len(strengths) * quantile)
    return strengths[min(idx, len(strengths) - 1)]


def _median_centroid(onsets: list[Onset]) -> float:
    vals = sorted(o.centroid_hz for o in onsets)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else 0.5 * (vals[mid - 1] + vals[mid])


def _band_from_onset(onset: Onset, *, cutoff: float) -> bool:
    """Return True for low-band (lanes 0/1), False for high-band (lanes 2/3).

    Pure centroid split across all four lanes regardless of which stem
    the onset came from. An earlier design routed drums/bass to the low
    band and vocals to the high band, which gave each hand a dedicated
    instrument; playtesting found that locked too much musical variety
    out of each lane (left hand became "the drum hand"). Now the stem
    tag is informational only - it survives on the Onset for future
    features (e.g. stem-aware chord-accent rules) but does NOT decide
    lane placement. Cleaner per-stem onset timing is still the main
    reason to run Demucs.
    """
    return onset.centroid_hz < cutoff
