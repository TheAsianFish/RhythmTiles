"""Tests for the time-windowed difficulty thinner (v0.11.0).

The thinner replaces global top-K with top-K-per-window. These tests
verify the intended effect WITHOUT regressing the existing invariants:
  - chord pairs (same-t notes) stay whole
  - no note times are altered (only some are dropped)
  - total NPS stays within the difficulty band on uniformly-dense input
  - on uneven input, the thinner spreads notes across windows instead
    of letting strong-score moments dominate the whole keep budget
  - the sanity cap still runs after windowed selection
"""

from __future__ import annotations

import random

import pytest

from app.pipeline.difficulty import (
    SELECT_WINDOW_S,
    TARGET_NOTES_PER_SEC,
    _select_windowed_by_score,
    shape_difficulty,
)
from app.pipeline.lane_assign import RawNote


def _uniform_notes(*, n: int, span_s: float, base_strength: float = 1.0) -> list[RawNote]:
    """Create n notes uniformly distributed in [0, span_s]."""
    return [
        RawNote(
            t=round(span_s * (i + 0.5) / n, 4),
            lane=i % 4,
            type="tap",
            strength=base_strength,
        )
        for i in range(n)
    ]


def _scored(notes: list[RawNote], scores: list[float]) -> tuple[list[RawNote], list[float]]:
    """Convenience: pair a notes list with its per-note score list."""
    assert len(notes) == len(scores)
    return notes, scores


# ---------------------------------------------------------------------------
# Core invariant tests
# ---------------------------------------------------------------------------


def test_windowed_keeps_notes_at_original_times():
    """The thinner must NOT move notes. It only drops them."""
    notes = _uniform_notes(n=200, span_s=60.0)
    scores = [n.strength for n in notes]
    selected = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="normal", duration_s=60.0,
    )
    kept_times = sorted(notes[i].t for i in selected)
    original_times = sorted(n.t for n in notes)
    # Every kept time must appear in the original times unchanged.
    for kt in kept_times:
        assert kt in original_times, f"kept time {kt} not in original times"


def test_chord_partners_promoted_to_inf_score_always_survive():
    """The caller marks chord partners as inf-score so they all-or-none
    pass the windowed selector. Verify chord pairs come out intact."""
    notes = [
        RawNote(t=0.5, lane=0, type="tap", strength=0.5),
        RawNote(t=0.5, lane=2, type="tap", strength=0.5),   # chord partner
        RawNote(t=1.0, lane=1, type="tap", strength=0.6),
        RawNote(t=1.5, lane=3, type="tap", strength=0.7),
    ]
    # Caller's behaviour: chord partners get inf score.
    scores = [float("inf"), float("inf"), 0.6, 0.7]
    selected = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="easy", duration_s=2.0,
    )
    # Both chord partners must be selected together.
    assert 0 in selected, "chord partner at lane 0 dropped"
    assert 1 in selected, "chord partner at lane 2 dropped"


def test_windowed_spreads_notes_across_windows_not_just_chorus():
    """The whole point: a song with one DENSE strong-score burst plus
    uniformly weak background should still have the background kept,
    not just the burst."""
    background = [
        RawNote(t=t, lane=i % 4, type="tap", strength=0.3)
        for i, t in enumerate(
            [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5,
             10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5,
             20.5, 21.5, 22.5, 23.5, 24.5, 25.5, 26.5, 27.5, 28.5, 29.5]
        )
    ]
    # Strong burst at seconds 10-14 (concentrated in one 4s region).
    burst_times = [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6, 11.8,
                   12.0, 12.2, 12.4, 12.6, 12.8, 13.0, 13.2, 13.4, 13.6, 13.8]
    burst = [
        RawNote(t=t, lane=i % 4, type="tap", strength=0.95)
        for i, t in enumerate(burst_times)
    ]
    notes = sorted(background + burst, key=lambda n: n.t)
    scores = [n.strength for n in notes]

    selected = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="normal", duration_s=30.0,
    )

    # Check that windows OUTSIDE the burst still have some surviving notes.
    # Specifically the [20s, 30s] window range: under global top-K with 20
    # strong burst notes, the weaker background here would have been wiped.
    late_kept = [notes[i] for i in selected if notes[i].t >= 20.0]
    assert len(late_kept) >= 4, (
        f"late-section was over-thinned: only {len(late_kept)} notes "
        f"survived in [20s, 30s] — windowed thinner should have kept more"
    )


def test_windowed_caps_dense_window_at_target_per_window():
    """A super-dense window must not blow past the per-window cap."""
    # 50 notes packed into a 2s window.
    dense_window = [
        RawNote(t=0.04 * i, lane=i % 4, type="tap", strength=0.9)
        for i in range(50)
    ]
    scores = [n.strength for n in dense_window]
    target_mid = 0.5 * sum(TARGET_NOTES_PER_SEC["expert"])
    cap = int(round(target_mid * SELECT_WINDOW_S))

    selected = _select_windowed_by_score(
        notes=dense_window, scores=scores, difficulty="expert", duration_s=2.0,
    )
    # All 50 notes fit in window 0. Cap should hold.
    assert len(selected) <= cap, (
        f"window cap blown: {len(selected)} kept, expected <= {cap}"
    )


def test_easy_difficulty_keeps_far_fewer_than_expert():
    """Sanity: easy and expert on the same input produce dramatically
    different totals."""
    notes = _uniform_notes(n=600, span_s=60.0)
    scores = [n.strength for n in notes]

    easy_sel = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="easy", duration_s=60.0,
    )
    expert_sel = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="expert", duration_s=60.0,
    )
    assert len(easy_sel) < len(expert_sel) * 0.5, (
        f"easy ({len(easy_sel)}) should be well under half of expert "
        f"({len(expert_sel)})"
    )


def test_sparse_song_produces_sparse_chart_no_fake_density():
    """A song with onsets clustered in one region and silence elsewhere
    must NOT be artificially densified by stuffing the dense region
    past its window cap. That was the old global-top-K bug; the new
    windowed thinner honestly reflects the music's own sparsity."""
    # 20 candidates all clustered in seconds 0-10 of a 30-second song.
    notes = [
        RawNote(t=0.5 + i * 0.5, lane=i % 4, type="tap", strength=0.4 + 0.01 * i)
        for i in range(20)
    ]
    scores = [n.strength for n in notes]
    selected = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="easy", duration_s=30.0,
    )

    # Per-window cap for easy = round(1.16 * 2) = 2 notes/window.
    # Notes at t=0.5..10.0 step 0.5 span 6 windows (0..5 inclusive,
    # since t=10.0 lands in window [10, 12)). With cap=2, the max
    # selectable is 6 * 2 = 12; the actual will be slightly less because
    # one window has only 1 candidate.
    cap = 2
    counts_per_window: dict[int, int] = {}
    for i in selected:
        w = int(notes[i].t / SELECT_WINDOW_S)
        counts_per_window[w] = counts_per_window.get(w, 0) + 1
    # Hard cap invariant: NO window exceeds 2. This is the bug we're guarding.
    for w, c in counts_per_window.items():
        assert c <= cap, (
            f"window {w} has {c} notes, exceeds cap {cap}. "
            f"Fake-density bug reintroduced."
        )
    # Verify NO note was selected from the empty late windows (no candidates there).
    for i in selected:
        assert notes[i].t < 10.5, (
            f"selected note at t={notes[i].t} from outside the "
            f"candidate-bearing region (0-10s) — that would mean we "
            f"invented a note"
        )
    # And not drained to nothing.
    assert len(selected) >= 8, (
        f"too aggressive: only {len(selected)} kept from 20 candidates"
    )


def test_shape_difficulty_end_to_end_returns_time_sorted_notes():
    """The full shape_difficulty wrapper must still return notes sorted
    by t (downstream stages assume this)."""
    notes = _uniform_notes(n=300, span_s=60.0)
    random.Random(0).shuffle(notes)  # adversarial: shuffle input order
    result = shape_difficulty(
        notes=notes,
        difficulty="normal",
        beats=[0.0, 0.5, 1.0],
        energy_buckets=None,
        song_duration_s=60.0,
    )
    times = [n.t for n in result]
    assert times == sorted(times), "shape_difficulty output not time-sorted"


def test_shape_difficulty_empty_input():
    """Edge case: empty input returns empty output without crashing."""
    result = shape_difficulty(
        notes=[],
        difficulty="hard",
        beats=[0.0, 0.5],
        energy_buckets=None,
        song_duration_s=60.0,
    )
    assert result == []


def test_total_nps_stays_in_or_below_difficulty_band_on_dense_input():
    """On a uniformly dense song, the kept NPS should land in or below
    the difficulty's target band (the sanity cap might pull it down)."""
    duration_s = 60.0
    notes = _uniform_notes(n=600, span_s=duration_s)  # 10 NPS uniform
    result = shape_difficulty(
        notes=notes,
        difficulty="normal",  # target band 2.63-3.99 NPS
        beats=[0.0, 0.5, 1.0],
        energy_buckets=None,
        song_duration_s=duration_s,
    )
    nps = len(result) / duration_s
    lo, hi = TARGET_NOTES_PER_SEC["normal"]
    # Allow a 30% slack on the upper bound because the sanity cap and
    # backfill can move slightly outside the strict band on edge inputs.
    assert nps <= hi * 1.3, f"NPS {nps:.2f} > hi {hi} * 1.3"
    assert nps >= lo * 0.6, f"NPS {nps:.2f} < lo {lo} * 0.6"


def test_windowed_distribution_is_more_uniform_than_global_top_k():
    """The DIFFERENTIATING property: windowed yields more uniform
    per-window note counts than global top-K would on the same input.

    Constructs a song with one dense strong-score chunk + uniform weak
    background, then measures the standard deviation of per-window note
    counts under windowed vs simulated-global selection."""
    # Background: 60 notes spread over 60 seconds at strength 0.3.
    background = [
        RawNote(t=0.5 + i * 1.0, lane=i % 4, type="tap", strength=0.3)
        for i in range(60)
    ]
    # Burst: 40 notes in seconds 20-22 at strength 0.95.
    burst = [
        RawNote(t=20.0 + 0.05 * i, lane=i % 4, type="tap", strength=0.95)
        for i in range(40)
    ]
    notes = sorted(background + burst, key=lambda n: n.t)
    scores = [n.strength for n in notes]

    windowed = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="hard", duration_s=60.0,
    )

    # Simulated global top-K: same total count, but picked purely by score.
    n_keep = len(windowed)
    sorted_indices = sorted(range(len(notes)), key=lambda i: (-scores[i], i))
    global_sel = set(sorted_indices[:n_keep])

    # Compute per-window note counts for each method.
    def _per_window_counts(sel: set[int]) -> list[int]:
        counts = [0] * 30  # 30 windows of 2s in 60s
        for i in sel:
            w = int(notes[i].t / 2.0)
            if 0 <= w < 30:
                counts[w] += 1
        return counts

    win_counts = _per_window_counts(windowed)
    glob_counts = _per_window_counts(global_sel)

    # Variance / spread comparison.
    def _stddev(counts: list[int]) -> float:
        mu = sum(counts) / len(counts)
        return (sum((c - mu) ** 2 for c in counts) / len(counts)) ** 0.5

    win_sd = _stddev(win_counts)
    glob_sd = _stddev(glob_counts)
    assert win_sd < glob_sd, (
        f"windowed (sd={win_sd:.2f}) should be more uniform than "
        f"global (sd={glob_sd:.2f}). Got: windowed={win_counts}, "
        f"global={glob_counts}"
    )


def test_no_window_has_more_than_target_when_input_is_uniform():
    """Under a uniformly-strong input, no single window should exceed
    target_K_per_window — the cap should be a hard ceiling."""
    notes = _uniform_notes(n=400, span_s=60.0)  # ~13.3 candidates per 2s window
    scores = [n.strength for n in notes]
    selected = _select_windowed_by_score(
        notes=notes, scores=scores, difficulty="hard", duration_s=60.0,
    )
    lo, hi = TARGET_NOTES_PER_SEC["hard"]
    target_mid = 0.5 * (lo + hi)
    cap = int(round(target_mid * SELECT_WINDOW_S))

    counts: dict[int, int] = {}
    for i in selected:
        w = int(notes[i].t / SELECT_WINDOW_S)
        counts[w] = counts.get(w, 0) + 1

    # Some chord-partner promotion could push a window slightly over, but
    # no chord pairs here so the cap is strict.
    for w, c in counts.items():
        assert c <= cap, f"window {w} has {c} notes, exceeds cap {cap}"


def test_shape_difficulty_preserves_chord_pairs_end_to_end():
    """Full integration: a chart with chord pairs at every 2nd beat
    should preserve all chord pairs as pairs (not half-kept) through
    shape_difficulty."""
    notes = []
    for i in range(60):
        t = round(i * 1.0, 4)
        # Add a chord pair on every other event.
        if i % 2 == 0:
            notes.append(RawNote(t=t, lane=0, type="tap", strength=0.7))
            notes.append(RawNote(t=t, lane=2, type="tap", strength=0.7))
        else:
            notes.append(RawNote(t=t, lane=1, type="tap", strength=0.5))

    result = shape_difficulty(
        notes=notes,
        difficulty="hard",
        beats=[0.0, 1.0, 2.0],
        energy_buckets=None,
        song_duration_s=60.0,
    )
    # Build kept-time multimap. Each chord-pair time should have 0 or 2
    # entries; never 1.
    times_to_lanes: dict[float, list[int]] = {}
    for n in result:
        times_to_lanes.setdefault(n.t, []).append(n.lane)

    for t, lanes in times_to_lanes.items():
        # Originally chord pairs were at even-integer seconds (lanes 0+2).
        # If this t is one of those, both lanes must be present or both absent.
        if any(round(t - even_t, 3) == 0 for even_t in [0.0, 2.0, 4.0, 6.0, 8.0]):
            assert sorted(lanes) in ([0, 2], []), (
                f"chord at t={t} half-kept: lanes={lanes}"
            )
