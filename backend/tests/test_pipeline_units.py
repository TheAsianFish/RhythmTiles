"""Unit tests for the analysis pipeline.

We don't ship librosa-dependent tests in the skeleton; they require numpy/scipy
to be installed and on Windows that pulls a chunky wheel. Mark these so they're
optional during the initial setup.
"""

from __future__ import annotations

import pytest

from app.pipeline.difficulty import (
    approximate_density,
    shape_difficulty,
    target_density,
)
from app.pipeline.lane_assign import RawNote, assign_lanes
from app.pipeline.onset_detect import Onset


def test_target_density_known_values() -> None:
    assert target_density("easy") == 1.0
    assert target_density("normal") == 1.5
    assert target_density("hard") == 2.5
    # Expert is the new 4th tier. Strictly denser than hard so charts feel
    # genuinely harder rather than identical.
    assert target_density("expert") > target_density("hard")


def test_assign_lanes_routes_by_centroid() -> None:
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),    # low -> 0 or 1
        Onset(t=0.5, strength=1.0, centroid_hz=4000.0),   # high -> 2 or 3
        Onset(t=1.0, strength=1.0, centroid_hz=300.0),    # low again
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    assert len(notes) == 3
    assert notes[0].lane in (0, 1)
    assert notes[1].lane in (2, 3)
    assert notes[2].lane in (0, 1)


def test_assign_lanes_anti_clustering() -> None:
    # Two low onsets 10ms apart should land in different lanes.
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),
        Onset(t=0.01, strength=1.0, centroid_hz=200.0),
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    assert len(notes) == 2
    assert notes[0].lane != notes[1].lane


def test_assign_lanes_drops_when_all_lanes_too_recent() -> None:
    # 5 onsets within 10ms; only 4 lanes; one should be dropped.
    onsets = [
        Onset(t=i * 0.002, strength=1.0, centroid_hz=200.0 + i)
        for i in range(5)
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    assert len(notes) <= 4


def test_shape_difficulty_thins_above_target() -> None:
    # 60 notes over 10 beats = density 6.0. easy target 1.0 -> should thin hard.
    beats = [0.0 + i * 0.5 for i in range(11)]
    notes = [RawNote(t=i * 0.083, lane=i % 4) for i in range(60)]
    shaped = shape_difficulty(notes=notes, difficulty="easy", beats=beats)
    density = approximate_density(shaped, beats)
    assert density <= target_density("easy") * 1.5  # within 50% of target


def test_shape_difficulty_no_op_when_under_target() -> None:
    beats = [0.0 + i * 0.5 for i in range(11)]
    notes = [RawNote(t=i * 1.0, lane=i % 4) for i in range(4)]
    shaped = shape_difficulty(notes=notes, difficulty="hard", beats=beats)
    assert shaped == notes


def test_shape_difficulty_handles_empty() -> None:
    assert shape_difficulty(notes=[], difficulty="normal", beats=[]) == []


def test_shape_difficulty_preserves_chord_partners() -> None:
    # Two notes at exactly the same t (different lanes) are a chord and must
    # survive thinning even when the surrounding density would normally drop one.
    # Build a dense stream that triggers thinning, with a chord inside it.
    beats = [i * 0.5 for i in range(11)]
    notes: list[RawNote] = []
    for i in range(60):
        t = i * 0.083
        notes.append(RawNote(t=t, lane=i % 4))
        if i == 30:
            # Insert a chord partner immediately after note 30 at the same t.
            notes.append(RawNote(t=t, lane=(i + 2) % 4))
    shaped = shape_difficulty(notes=notes, difficulty="easy", beats=beats)
    # Find chord pair in the shaped output (two notes with the same t).
    times = [n.t for n in shaped]
    chord_t = notes[31].t
    same_t = [n for n in shaped if abs(n.t - chord_t) < 1e-6]
    assert len(same_t) == 2, f"chord partner was dropped (shaped times around chord: {times[:5]})"


def test_assign_lanes_emits_chord_on_strong_onset() -> None:
    # Build a stream where one onset is much stronger than the rest. Even with
    # only 24 onsets (the minimum), the top-quantile one should yield a chord.
    onsets: list[Onset] = []
    for i in range(40):
        onsets.append(
            Onset(t=0.25 * i, strength=0.2, centroid_hz=200.0 if i % 2 == 0 else 4000.0),
        )
    # Make onset index 20 the strongest (top quantile).
    onsets[20] = Onset(t=0.25 * 20, strength=1.0, centroid_hz=2000.0)
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    # Find two notes at t == 5.0 (20 * 0.25) -> chord
    chord_partners = [n for n in notes if abs(n.t - 5.0) < 1e-6]
    assert len(chord_partners) == 2, f"expected chord at t=5.0, got {chord_partners}"
    # One lane must be from the low band, the other from the high band.
    lanes = sorted(n.lane for n in chord_partners)
    assert lanes[0] in (0, 1) and lanes[1] in (2, 3)


def test_hold_detect_returns_taps_when_audio_is_short_silence() -> None:
    import numpy as np

    from app.pipeline.hold_detect import detect_holds

    sr = 22050
    y = np.zeros(sr // 2, dtype=np.float32)  # 0.5s of silence
    notes = [RawNote(t=0.1, lane=0), RawNote(t=0.3, lane=1)]
    out = detect_holds(notes=notes, y=y, sr=sr)
    # Silence -> no sustained energy -> all stay taps.
    assert all(n.type == "tap" for n in out)
    assert len(out) == 2


def test_hold_detect_promotes_sustained_note_to_hold() -> None:
    import numpy as np

    from app.pipeline.hold_detect import detect_holds

    sr = 22050
    # 2 seconds of audio. From t=0.2s to t=0.8s, a sustained sine. Everything
    # else is silent. The note at t=0.2 should become a hold of ~0.6s.
    duration_s = 2.0
    y = np.zeros(int(sr * duration_s), dtype=np.float32)
    start_idx = int(sr * 0.2)
    end_idx = int(sr * 0.8)
    t_axis = np.arange(end_idx - start_idx) / sr
    y[start_idx:end_idx] = 0.5 * np.sin(2 * np.pi * 440 * t_axis).astype(np.float32)

    notes = [RawNote(t=0.2, lane=0)]
    out = detect_holds(notes=notes, y=y, sr=sr)
    assert len(out) == 1
    assert out[0].type == "hold"
    # Duration should land within 150ms of the actual 0.6s sustain.
    assert out[0].duration is not None
    assert 0.4 <= out[0].duration <= 0.75, f"unexpected duration {out[0].duration}"


def test_hold_detect_respects_next_note_in_same_lane() -> None:
    import numpy as np

    from app.pipeline.hold_detect import detect_holds

    sr = 22050
    # 5 seconds of audio. Sustained tone from 0 to 1.0s (so the first note's
    # natural sustain extends well past its horizon), then silence (so the
    # second note has barely any sustain and isn't a candidate).
    y = np.zeros(int(sr * 5.0), dtype=np.float32)
    tone_end = int(sr * 1.0)
    t_axis = np.arange(tone_end) / sr
    y[:tone_end] = 0.5 * np.sin(2 * np.pi * 440 * t_axis).astype(np.float32)

    # First note at 0.1, second at 0.8 (both lane 0). Horizon for first is
    # 0.8 - 0.05 = 0.75, available = 0.65 (well above MIN_HOLD_S). Without
    # the horizon, natural sustain would reach 1.0s.
    notes = [RawNote(t=0.1, lane=0), RawNote(t=0.8, lane=0)]
    out = detect_holds(notes=notes, y=y, sr=sr)
    holds = [n for n in out if n.type == "hold"]
    assert len(holds) == 1
    assert holds[0].t == 0.1
    assert holds[0].duration is not None
    # Horizon cap: 0.65s (= 0.75 horizon - 0.1 start). Allow a small frame
    # quantization margin above.
    assert holds[0].duration < 0.7


def test_hold_detect_caps_duration_to_beat_grid() -> None:
    import numpy as np

    from app.pipeline.hold_detect import detect_holds

    sr = 22050
    # 5 seconds of sustained tone so the natural sustain would run very long.
    y = np.zeros(int(sr * 5.0), dtype=np.float32)
    t_axis = np.arange(int(sr * 5.0)) / sr
    y[:] = 0.5 * np.sin(2 * np.pi * 440 * t_axis).astype(np.float32)

    # 120 BPM -> beat_period = 0.5s, MAX_HOLD_BEATS=2.0 -> max hold = 1.0s.
    notes = [RawNote(t=0.5, lane=0)]
    beats = [0.5 * i for i in range(11)]  # beats every 0.5s out to 5.0s
    out = detect_holds(
        notes=notes,
        y=y,
        sr=sr,
        beat_period_s=0.5,
        beats_s=beats,
    )
    holds = [n for n in out if n.type == "hold"]
    assert len(holds) == 1
    assert holds[0].duration is not None
    # Hard cap on hold duration is 2 beats = 1.0s. The snap may round to the
    # nearest beat boundary so anything from ~0.5s to ~1.0s is acceptable.
    assert 0.4 <= holds[0].duration <= 1.05, f"unexpected duration {holds[0].duration}"


def test_hold_detect_snaps_end_to_beat_grid() -> None:
    import numpy as np

    from app.pipeline.hold_detect import detect_holds

    sr = 22050
    # Tone that decays right around t=0.93 (between beats at 0.75 and 1.0).
    y = np.zeros(int(sr * 3.0), dtype=np.float32)
    tone_end = int(sr * 0.93)
    t_axis = np.arange(tone_end) / sr
    y[:tone_end] = 0.5 * np.sin(2 * np.pi * 440 * t_axis).astype(np.float32)

    notes = [RawNote(t=0.1, lane=0)]
    beats = [0.25 * i for i in range(13)]  # 240 BPM grid for tight test resolution
    out = detect_holds(
        notes=notes,
        y=y,
        sr=sr,
        beat_period_s=0.25,
        beats_s=beats,
    )
    holds = [n for n in out if n.type == "hold"]
    assert len(holds) == 1
    end_t = holds[0].t + (holds[0].duration or 0)
    # End should be snapped to a beat or half-beat boundary (multiples of 0.125).
    snapped = round(end_t / 0.125) * 0.125
    assert abs(end_t - snapped) < 0.01, f"end {end_t} not snapped to {snapped}"


def test_hold_detect_caps_total_hold_rate() -> None:
    import numpy as np

    from app.pipeline.hold_detect import detect_holds

    sr = 22050
    # Tone for the full 6 seconds so every onset has theoretically sustainable
    # energy. Place 20 notes in alternating lanes far enough apart that none
    # of them would be blocked by same-lane horizon.
    y = np.zeros(int(sr * 6.0), dtype=np.float32)
    t_axis = np.arange(int(sr * 5.0)) / sr
    y[: int(sr * 5.0)] = 0.5 * np.sin(2 * np.pi * 440 * t_axis).astype(np.float32)
    # Cut a 50ms gap every 200ms so each onset's local baseline still differs
    # enough from peak that some sustain is measurable. Without this the
    # baseline check would zero out everything.
    for i in range(25):
        cut_start = int(sr * (i * 0.2 + 0.15))
        cut_end = int(sr * (i * 0.2 + 0.20))
        y[cut_start:cut_end] = 0

    notes = [RawNote(t=0.2 * i, lane=i % 4) for i in range(20)]
    out = detect_holds(notes=notes, y=y, sr=sr)
    hold_count = sum(1 for n in out if n.type == "hold")
    # MAX_HOLD_RATIO = 0.08 -> at most max(1, int(20 * 0.08)) = 1 hold.
    assert hold_count <= 2, f"too many holds: {hold_count}"


def test_assign_lanes_breaks_same_hand_streak_on_stream() -> None:
    # All-low stream: without hand balance, lanes would alternate 0,1,0,1,0,1...
    # all on the left hand. With hand balance, after 2-3 consecutive same-hand
    # notes the assigner should reach across to the right hand.
    onsets = [
        Onset(t=0.10 * i, strength=0.3, centroid_hz=200.0)
        for i in range(12)
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    assert len(notes) == 12
    # Count notes per hand. With hand balance, right hand should see some
    # action even though every onset is "low band" by centroid.
    right_hand_count = sum(1 for n in notes if n.lane in (2, 3))
    assert right_hand_count >= 3, (
        f"hand-balance failed to push notes onto right hand: lanes={[n.lane for n in notes]}"
    )


def test_assign_lanes_does_not_balance_when_gap_is_large() -> None:
    # Onsets spaced 1.0s apart are not a stream. Centroid alone should drive
    # placement so all stay in the low band.
    onsets = [
        Onset(t=1.0 * i, strength=0.3, centroid_hz=200.0)
        for i in range(6)
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    # Every note should be left-hand (lanes 0 or 1) because no stream override.
    assert all(n.lane in (0, 1) for n in notes), f"unexpected lanes: {[n.lane for n in notes]}"


def test_shape_difficulty_with_energy_buckets_keeps_more_in_high() -> None:
    # 60 notes alternating lanes. First half tagged low-energy (bucket 0),
    # second half tagged high-energy (bucket 2). With density target = easy
    # the shaper should keep more of the high-energy notes than the low.
    beats = [i * 0.5 for i in range(11)]
    notes = [RawNote(t=i * 0.083, lane=i % 4) for i in range(60)]
    buckets = [0] * 30 + [2] * 30
    shaped = shape_difficulty(
        notes=notes,
        difficulty="easy",
        beats=beats,
        energy_buckets=buckets,
    )
    low_kept = sum(1 for n in shaped if n.t < 30 * 0.083)
    high_kept = sum(1 for n in shaped if n.t >= 30 * 0.083)
    assert high_kept > low_kept, f"expected denser chorus: low={low_kept} high={high_kept}"


def test_shape_difficulty_uniform_when_no_buckets() -> None:
    # No energy_buckets parameter: behavior should match the old uniform thin.
    beats = [i * 0.5 for i in range(11)]
    notes = [RawNote(t=i * 0.083, lane=i % 4) for i in range(60)]
    without_buckets = shape_difficulty(notes=notes, difficulty="easy", beats=beats)
    with_uniform_buckets = shape_difficulty(
        notes=notes, difficulty="easy", beats=beats, energy_buckets=[1] * 60,
    )
    assert [n.t for n in without_buckets] == [n.t for n in with_uniform_buckets]


def test_beat_fill_inserts_synthetics_in_long_empty_runs() -> None:
    from app.pipeline.beat_fill import fill_empty_beats
    from app.pipeline.onset_detect import Onset

    # 10 beats every 0.5s. Real onsets only at beats 0 and 9. The 8-beat
    # empty run in the middle should get filled.
    beats = [i * 0.5 for i in range(10)]
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),
        Onset(t=4.5, strength=1.0, centroid_hz=2000.0),
    ]
    out = fill_empty_beats(onsets, beats)
    assert len(out) > len(onsets), "should have inserted synthetics"
    # New onsets land near beat times.
    new_ts = sorted(o.t for o in out if o not in onsets)
    for t in new_ts:
        nearest = min(beats, key=lambda b: abs(b - t))
        assert abs(t - nearest) < 0.01, f"synthetic {t} not on a beat (nearest {nearest})"


def test_beat_fill_preserves_short_empty_runs() -> None:
    from app.pipeline.beat_fill import fill_empty_beats
    from app.pipeline.onset_detect import Onset

    # 5 beats, with a single empty beat in the middle (beat 2). Short runs
    # should NOT be filled - they're musical breaks.
    beats = [i * 0.5 for i in range(5)]
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),
        Onset(t=0.5, strength=1.0, centroid_hz=200.0),
        Onset(t=1.5, strength=1.0, centroid_hz=200.0),
        Onset(t=2.0, strength=1.0, centroid_hz=200.0),
    ]
    out = fill_empty_beats(onsets, beats)
    assert len(out) == len(onsets), "1-beat empty run should not be filled"


def test_stems_pass_through_when_demucs_disabled() -> None:
    import numpy as np

    from app.pipeline.stems import separate_stems

    y = np.zeros(22050, dtype=np.float32)  # 1s silence
    sr = 22050
    s = separate_stems(y, sr, use_demucs=False)
    assert s.separated is False
    # All channels share the same identity as the mix when separation is off.
    assert s.drums is y
    assert s.vocals is y
    assert s.bass is y
    assert s.other is y


def test_stems_falls_back_gracefully_when_demucs_missing() -> None:
    import numpy as np

    from app.pipeline.stems import separate_stems

    y = np.zeros(22050, dtype=np.float32)
    sr = 22050
    # use_demucs=True but demucs probably isn't installed in CI. The fallback
    # path must return pass-through stems instead of raising.
    s = separate_stems(y, sr, use_demucs=True)
    # Whether or not demucs IS installed locally, this must return a Stems.
    # If real separation ran, separated=True; if it fell back, separated=False.
    assert s.sr == sr
    assert s.drums is not None and s.vocals is not None


def test_assign_lanes_no_chords_when_too_few_onsets() -> None:
    # Below MIN_ONSETS_FOR_CHORDS, the threshold is +inf so no chord can fire.
    onsets = [
        Onset(t=i * 0.5, strength=1.0, centroid_hz=200.0 if i % 2 == 0 else 4000.0)
        for i in range(10)
    ]
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    times = [n.t for n in notes]
    assert len(times) == len(set(times)), "no two notes should share a t when chord disabled"
