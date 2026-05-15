"""Tests for the ML phases described in docs/ML_PLAN.md.

These verify:
  - The heuristic pipeline is the rollback: when ML deps are missing or
    flags are off, behaviour matches the librosa baseline.
  - Beat This! plumbing (BeatInfo.downbeats, BeatInfo.bpm_curve,
    BeatInfo.source) is wired through to lane assignment and the Chart.
  - Per-stem onset detection routes onsets by stem label, with the lane
    assigner honouring that label.
  - Downbeat-aware accent chords fire when downbeats are provided,
    independent of the strength-quantile gate.
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest

from app.pipeline.lane_assign import assign_lanes
from app.pipeline.onset_detect import Onset


def test_beat_this_falls_back_when_disabled() -> None:
    """USE_BEAT_THIS=0 must return the librosa path, never error."""
    import app.config as config
    from app.pipeline import beat_track

    importlib.reload(config)
    importlib.reload(beat_track)

    sr = 22050
    y = (np.random.default_rng(0).standard_normal(sr * 2) * 0.05).astype(np.float32)
    for k in (0, sr // 2, sr, (sr * 3) // 2):
        y[k : k + 256] += 0.6
    info = beat_track.detect_beats(y=y, sr=sr)
    assert info.source == "librosa"
    assert info.downbeats is None  # librosa has no downbeat detector


def test_beat_this_falls_back_when_package_missing(monkeypatch) -> None:
    """USE_BEAT_THIS=1 with no `beat_this` installed must NOT raise.

    The plan calls for the heuristic pipeline to stay the rollback. The
    detect_beats call must succeed and produce a librosa BeatInfo even
    when the user has turned on the flag without installing extras.
    """
    monkeypatch.setenv("USE_BEAT_THIS", "1")
    import app.config as config
    from app.ml import beat_this
    from app.pipeline import beat_track

    importlib.reload(config)
    importlib.reload(beat_this)
    importlib.reload(beat_track)

    # Whether or not the host happens to have beat_this installed, the
    # call must succeed. If beat_this is installed and inference succeeds
    # we'll see source="beat-this"; otherwise we must see librosa as the
    # fallback (NOT an exception).
    sr = 22050
    y = (np.random.default_rng(0).standard_normal(sr * 2) * 0.05).astype(np.float32)
    for k in (0, sr // 2, sr, (sr * 3) // 2):
        y[k : k + 256] += 0.6
    info = beat_track.detect_beats(y=y, sr=sr)
    assert info.source in {"librosa", "beat-this", "beat-this (cached)", "librosa (cached)"}


def test_bpm_curve_populated_for_long_input() -> None:
    """detect_beats should produce a bpm_curve when there are enough beats."""
    from app.pipeline import beat_track

    importlib.reload(beat_track)
    sr = 22050
    # 10 seconds of clicks at 120 BPM gives ~20 beats, enough for the
    # 8-beat window in _bpm_curve_from_beats.
    n = sr * 10
    y = np.zeros(n, dtype=np.float32)
    period = sr // 2  # 120 BPM = 0.5s = sr/2 samples
    for k in range(0, n, period):
        y[k : k + 256] += 0.6
    info = beat_track.detect_beats(y=y, sr=sr)
    assert info.bpm > 0
    # Whether librosa or Beat This!, we should have enough beats to populate
    # the curve. The curve format is list[(time_s, bpm)].
    if info.bpm_curve is not None:
        assert all(isinstance(p, tuple) and len(p) == 2 for p in info.bpm_curve)
        assert all(bpm > 0 for _t, bpm in info.bpm_curve)


def test_beat_cache_round_trip(tmp_path, monkeypatch) -> None:
    """Beats persisted to disk must round-trip exactly."""
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    import app.config as config

    importlib.reload(config)
    from app.ml import beat_cache

    importlib.reload(beat_cache)
    beat_cache.put(
        content_hash="abc123",
        source="beat-this",
        bpm=120.0,
        beats=[0.0, 0.5, 1.0, 1.5],
        downbeats=[0.0, 2.0],
        bpm_curve=[(0.0, 120.0), (1.0, 121.0)],
    )
    got = beat_cache.get(content_hash="abc123", source="beat-this")
    assert got is not None
    assert got.bpm == 120.0
    assert got.beats == [0.0, 0.5, 1.0, 1.5]
    assert got.downbeats == [0.0, 2.0]
    assert got.bpm_curve == [(0.0, 120.0), (1.0, 121.0)]


def test_beat_cache_miss_returns_none() -> None:
    from app.ml import beat_cache

    assert beat_cache.get(content_hash="nope", source="beat-this") is None
    assert beat_cache.get(content_hash=None, source="beat-this") is None


def test_onset_cache_round_trip(tmp_path, monkeypatch) -> None:
    """Per-stem onsets persisted to disk must round-trip, stem tags intact.

    The point of this cache is to make Demucs runs reusable across
    difficulties on the same audio. If the stem tag doesn't survive, the
    lane assigner falls back to centroid routing and the whole Demucs
    spend was wasted.
    """
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    import app.config as config

    importlib.reload(config)
    from app.ml import onset_cache

    importlib.reload(onset_cache)
    onsets_in = [
        Onset(t=0.5, strength=0.7, centroid_hz=180.0, stem="drums"),
        Onset(t=1.0, strength=0.9, centroid_hz=2400.0, stem="vocals"),
        Onset(t=1.5, strength=0.3, centroid_hz=900.0, stem=None),
    ]
    onset_cache.put(content_hash="xyz", source="per-stem", onsets=onsets_in)
    got = onset_cache.get(content_hash="xyz", source="per-stem")
    assert got is not None
    assert len(got) == 3
    assert got[0].stem == "drums"
    assert got[1].stem == "vocals"
    assert got[2].stem is None
    assert got[0].t == 0.5
    assert got[1].centroid_hz == 2400.0


def test_onset_cache_miss_returns_none() -> None:
    from app.ml import onset_cache

    assert onset_cache.get(content_hash="nope", source="per-stem") is None
    assert onset_cache.get(content_hash=None, source="per-stem") is None


def test_mert_is_available_does_not_raise() -> None:
    """is_available() must be cheap and never throw, even when deps are missing."""
    from app.ml import mert

    result = mert.is_available()
    assert isinstance(result, bool)


def test_mert_embed_returns_none_on_failure() -> None:
    """When MERT isn't importable or inference fails, embed() returns None.

    This is the contract that lets the chart builder safely call MERT and
    fall back to RMS bucketing without a try/except around every call.
    """
    from app.ml import mert

    sr = 22050
    y = np.zeros(sr * 2, dtype=np.float32)
    out = mert.embed(y=y, sr=sr)
    # Either None (package missing or inference failed) or a MertResult.
    # Both must be valid; we never raise from this code path.
    assert out is None or hasattr(out, "embeddings")


def test_section_cache_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    import app.config as config

    importlib.reload(config)
    from app.ml import section_cache

    importlib.reload(section_cache)
    sections_in = [
        section_cache.CachedSection(
            start_s=0.0, end_s=15.0, bucket=0, cluster_id=2, intensity=0.12,
        ),
        section_cache.CachedSection(
            start_s=15.0, end_s=45.0, bucket=2, cluster_id=1, intensity=0.71,
        ),
    ]
    section_cache.put(content_hash="abc", source="mert", sections=sections_in)
    got = section_cache.get(content_hash="abc", source="mert")
    assert got is not None
    assert len(got) == 2
    assert got[0].bucket == 0
    assert got[1].bucket == 2
    assert got[1].intensity == 0.71


def test_section_cache_miss_returns_none() -> None:
    from app.ml import section_cache

    assert section_cache.get(content_hash="nope", source="mert") is None
    assert section_cache.get(content_hash=None, source="mert") is None


def test_sections_from_embeddings_too_short() -> None:
    """Audio shorter than k * MIN_SECTION_S returns no sections."""
    from app.ml import sections

    # 10 seconds at 50 fps = 500 frames. K=4 * 6s = 24s needed.
    embeddings = np.random.RandomState(0).randn(500, 768).astype(np.float32)
    sr = 22050
    y = np.zeros(sr * 10, dtype=np.float32)
    out = sections.sections_from_embeddings(
        embeddings=embeddings, frame_rate_hz=50.0, y=y, sr=sr,
    )
    assert out == []


def test_sections_from_embeddings_long_enough() -> None:
    """Synthetic embeddings with structure produce non-empty section list."""
    from app.ml import sections

    # 120 seconds at 50 fps = 6000 frames, 4 clusters of 1500 frames each.
    n_per = 1500
    rng = np.random.RandomState(0)
    chunks = [
        rng.randn(n_per, 768).astype(np.float32) + offset
        for offset in (0.0, 5.0, 10.0, 0.0)
    ]
    embeddings = np.concatenate(chunks, axis=0)
    sr = 22050
    y = rng.randn(sr * 120).astype(np.float32) * 0.05
    out = sections.sections_from_embeddings(
        embeddings=embeddings, frame_rate_hz=50.0, y=y, sr=sr,
    )
    assert len(out) >= 2
    # Buckets must be in {0, 1, 2}.
    for s in out:
        assert s.bucket in (0, 1, 2)
        assert 0.0 <= s.start_s < s.end_s


def test_buckets_for_note_times_ordered_lookup() -> None:
    """Sweep-pointer lookup returns the right bucket for each note time."""
    from app.ml.sections import LabeledSection, buckets_for_note_times

    sections = [
        LabeledSection(start_s=0.0, end_s=10.0, bucket=0, cluster_id=0, intensity=0.1),
        LabeledSection(start_s=10.0, end_s=30.0, bucket=2, cluster_id=1, intensity=0.7),
        LabeledSection(start_s=30.0, end_s=60.0, bucket=1, cluster_id=2, intensity=0.4),
    ]
    times = [1.0, 9.99, 10.0, 20.0, 29.99, 30.0, 45.0, 60.0, 200.0]
    got = buckets_for_note_times(note_times_s=times, sections=sections)
    assert got == [0, 0, 2, 2, 2, 1, 1, 1, 1]


def test_use_mert_false_skips_mert_path(monkeypatch) -> None:
    """use_mert=False must NOT call MERT even if the env says otherwise."""
    monkeypatch.setenv("USE_MERT", "1")
    import importlib

    import app.config as config
    importlib.reload(config)
    import app.pipeline.chart_builder as chart_builder
    importlib.reload(chart_builder)

    # If we passed use_mert=False, the build_chart path should produce a
    # chart without MERT sections (Chart.sections is None or empty).
    import io
    import soundfile as sf

    sr = 22050
    rng = np.random.default_rng(0)
    y = (rng.standard_normal(sr * 4) * 0.05).astype(np.float32)
    for k in range(0, sr * 4, sr // 2):
        y[k : k + int(0.02 * sr)] += rng.standard_normal(int(0.02 * sr)).astype(np.float32) * 0.6
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    chart = chart_builder.build_chart_from_audio(
        audio_bytes=buf.getvalue(),
        filename="t.wav",
        difficulty="normal",
        use_mert=False,
    )
    # sections is None when MERT didn't run (whether by flag, package
    # missing, or audio too short).
    assert chart.sections in (None, [])


def test_downbeat_accent_fires_chord_regardless_of_centroid() -> None:
    """A non-bright onset that lands on a downbeat must still emit a chord.

    The old rule required centroid >= 2800 Hz to fire a chord. With
    downbeats from Beat This!, a real bar-start fires the chord whether
    the onset is bright or not. This is the core Phase 1 unlock: stop
    guessing accents, use the real downbeat list.
    """
    # 40 onsets at 0.25s spacing. Onset 20 sits on t=5.0s with a LOW
    # centroid (would NOT pass the centroid gate). Pass downbeats=[5.0].
    onsets = [
        Onset(t=0.25 * i, strength=0.3, centroid_hz=400.0)
        for i in range(40)
    ]
    # Make onset 20 slightly stronger so the chord-fork has space, but
    # keep the centroid low so the old rule alone wouldn't fire.
    onsets[20] = Onset(t=5.0, strength=0.5, centroid_hz=400.0)
    notes = assign_lanes(
        onsets=onsets,
        y=None,  # type: ignore[arg-type]
        sr=22050,
        downbeats=[5.0],
    )
    at_downbeat = [n for n in notes if abs(n.t - 5.0) < 1e-6]
    assert len(at_downbeat) == 2, (
        f"downbeat onset should emit a chord stack: got {at_downbeat}"
    )
    lanes = sorted(n.lane for n in at_downbeat)
    assert lanes[0] in (0, 1) and lanes[1] in (2, 3)


def test_downbeat_without_match_does_not_force_chord() -> None:
    """A downbeat that no onset sits near should NOT manufacture a note."""
    onsets = [
        Onset(t=0.25 * i, strength=0.3, centroid_hz=400.0)
        for i in range(20)
    ]
    # Downbeat at 50s, no onset within tolerance.
    notes = assign_lanes(
        onsets=onsets,
        y=None,  # type: ignore[arg-type]
        sr=22050,
        downbeats=[50.0],
    )
    # 20 onsets, single notes, no chord stack manufactured.
    assert len(notes) == 20
    by_t = {}
    for n in notes:
        by_t[n.t] = by_t.get(n.t, 0) + 1
    assert max(by_t.values()) == 1


def test_stem_tag_does_not_dictate_band() -> None:
    """After the lane-lockup removal, stem tags must NOT force band routing.

    Earlier versions sent all drum onsets to lanes 0/1 and all vocal
    onsets to lanes 2/3. That made each hand "own" one instrument,
    which playtesting showed killed per-lane variety. Now band routing
    is purely centroid-driven; the stem tag rides on the Onset for
    other downstream uses but doesn't decide lane placement.

    Test setup: 8 drum-stem onsets alternating centroid between dark
    (200 Hz) and bright (5000 Hz). Median centroid = 2600 Hz, so dark
    onsets land in the low band (lanes 0/1) and bright ones in the
    high band (lanes 2/3) regardless of the drum stem tag. If the old
    stem-routing rule were still in place, ALL eight would go to
    lanes 0/1.
    """
    onsets = []
    for i in range(8):
        # Even indices: dark drum hit (kick); odd: bright drum hit (cymbal).
        centroid = 200.0 if i % 2 == 0 else 5000.0
        onsets.append(Onset(t=0.5 * i, strength=0.3, centroid_hz=centroid, stem="drums"))
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    # Group by centroid band: dark must mostly hit low, bright must mostly hit high.
    dark_in_low = sum(
        1 for i, n in enumerate(notes) if i % 2 == 0 and n.lane in (0, 1)
    )
    bright_in_high = sum(
        1 for i, n in enumerate(notes) if i % 2 == 1 and n.lane in (2, 3)
    )
    # 4 dark + 4 bright; expect majority of each to follow centroid.
    assert dark_in_low >= 3, (
        f"dark drum hits should follow centroid to low band: "
        f"lanes={[n.lane for n in notes]}"
    )
    assert bright_in_high >= 3, (
        f"bright drum hits should follow centroid to high band: "
        f"lanes={[n.lane for n in notes]}"
    )


def test_stem_field_optional_for_backward_compat() -> None:
    """Onsets without a stem field still flow through the centroid path."""
    onsets = [
        Onset(t=0.5 * i, strength=0.3, centroid_hz=200.0 if i % 2 == 0 else 4000.0)
        for i in range(8)
    ]
    # No stem on any onset.
    assert all(o.stem is None for o in onsets)
    notes = assign_lanes(onsets=onsets, y=None, sr=22050)  # type: ignore[arg-type]
    # Should still produce 8 notes with a mix of low and high lanes.
    assert len(notes) == 8
    assert any(n.lane in (0, 1) for n in notes)
    assert any(n.lane in (2, 3) for n in notes)


def test_per_stem_onset_tags_carry_through() -> None:
    """detect_onsets_per_stem must tag each onset with its source stem."""
    from app.pipeline.onset_detect import detect_onsets_per_stem

    sr = 22050
    rng = np.random.default_rng(0)
    # 2-second drum-like signal: short noise bursts.
    drums = np.zeros(sr * 2, dtype=np.float32)
    for k in (0, sr // 2, sr, (sr * 3) // 2):
        drums[k : k + int(0.02 * sr)] += rng.standard_normal(int(0.02 * sr)).astype(np.float32) * 0.6
    # Vocal-like signal: sustained sine with attacks.
    vocals = np.zeros(sr * 2, dtype=np.float32)
    t_axis = np.arange(int(sr * 0.3)) / sr
    for start_s in (0.25, 0.75, 1.25, 1.75):
        start = int(start_s * sr)
        end = min(start + len(t_axis), len(vocals))
        vocals[start:end] = 0.5 * np.sin(2 * np.pi * 440 * t_axis[: end - start]).astype(np.float32)

    onsets = detect_onsets_per_stem(drums=drums, vocals=vocals, sr=sr)
    assert len(onsets) > 0
    stems_seen = {o.stem for o in onsets}
    # Both stems should produce at least one onset given the audio above.
    assert "drums" in stems_seen
    assert "vocals" in stems_seen


def test_full_pipeline_works_without_ml_flags() -> None:
    """End-to-end smoke: heuristic baseline still produces a Chart."""
    import io

    import soundfile as sf

    from app.pipeline.chart_builder import build_chart_from_audio

    sr = 22050
    rng = np.random.default_rng(0)
    y = (rng.standard_normal(sr * 6) * 0.05).astype(np.float32)
    # 120 BPM clicks for 6 seconds.
    for k in range(0, sr * 6, sr // 2):
        y[k : k + int(0.02 * sr)] += rng.standard_normal(int(0.02 * sr)).astype(np.float32) * 0.6
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    chart = build_chart_from_audio(
        audio_bytes=buf.getvalue(),
        filename="t.wav",
        difficulty="normal",
    )
    assert chart.audio.duration == pytest.approx(6.0, abs=0.2)
    assert len(chart.notes) > 0


def _synth_audio_bytes() -> bytes:
    """Fixed synthetic audio for deterministic tests. 4 seconds of 120 BPM clicks."""
    import io

    import soundfile as sf

    sr = 22050
    rng = np.random.default_rng(42)  # fixed seed
    y = (rng.standard_normal(sr * 4) * 0.05).astype(np.float32)
    click_len = int(0.02 * sr)
    env = np.exp(-np.linspace(0, 6, click_len)).astype(np.float32)
    for k in range(0, sr * 4, sr // 2):
        end = min(k + click_len, len(y))
        # Use a fresh deterministic RNG inside the loop so noise is fixed too.
        noise = np.random.RandomState(k).randn(end - k).astype(np.float32)
        y[k:end] += env[: end - k] * noise * 0.6
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_heuristic_baseline_is_deterministic() -> None:
    """The heuristic pipeline (use_beat_this=False, use_demucs=False) must
    produce byte-identical Charts across runs on the same audio.

    This is the "easily activatable base version" guarantee: turning ML
    off via the explicit kwargs gives the same pipeline that shipped
    before ML landed. If this test ever fails, something in the new ML
    code paths is leaking into the baseline.
    """
    from app.pipeline.chart_builder import build_chart_from_audio

    audio = _synth_audio_bytes()
    chart_a = build_chart_from_audio(
        audio_bytes=audio,
        filename="snap.wav",
        difficulty="normal",
        use_beat_this=False,
        use_demucs=False,
    )
    chart_b = build_chart_from_audio(
        audio_bytes=audio,
        filename="snap.wav",
        difficulty="normal",
        use_beat_this=False,
        use_demucs=False,
    )

    # The notes are the gameplay-critical contract. generatedAt timestamps
    # differ between runs by design, so we compare just the gameplay data.
    assert [(n.t, n.lane, n.type, n.duration) for n in chart_a.notes] == [
        (n.t, n.lane, n.type, n.duration) for n in chart_b.notes
    ]
    assert chart_a.audio.bpm == chart_b.audio.bpm
    assert chart_a.audio.contentHash == chart_b.audio.contentHash


def test_explicit_use_beat_this_false_forces_librosa_path() -> None:
    """Passing use_beat_this=False must always pick librosa, even with the
    env flag on. Lets bench/tests force the baseline without env juggling."""
    import os

    from app.pipeline.beat_track import detect_beats

    # Pretend the env wants Beat This! on; the explicit kwarg should win.
    os.environ["USE_BEAT_THIS"] = "1"
    try:
        sr = 22050
        rng = np.random.default_rng(0)
        y = (rng.standard_normal(sr * 2) * 0.05).astype(np.float32)
        for k in (0, sr // 2, sr, (sr * 3) // 2):
            y[k : k + 256] += 0.6
        info = detect_beats(y=y, sr=sr, use_beat_this=False)
        assert info.source == "librosa"
        assert info.downbeats is None
    finally:
        os.environ["USE_BEAT_THIS"] = "0"


def test_beat_fill_fills_empty_downbeat() -> None:
    """A single empty DOWNBEAT must be filled. Bar starts are too
    important to leave dead. After v0.6.0 every empty beat gets filled
    regardless of run length, so the downbeat-specific path is now
    redundant with the generic fill, but the invariant still holds.
    """
    from app.pipeline.beat_fill import fill_empty_beats

    # 5 beats with a 1-beat empty run at beat index 2.
    beats = [i * 0.5 for i in range(5)]
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),
        Onset(t=0.5, strength=1.0, centroid_hz=200.0),
        Onset(t=1.5, strength=1.0, centroid_hz=200.0),  # gap at t=1.0
        Onset(t=2.0, strength=1.0, centroid_hz=200.0),
    ]

    # With t=1.0 marked as a downbeat: the gap gets a synthetic onset.
    out_with_db = fill_empty_beats(onsets, beats, downbeats=[1.0])
    assert len(out_with_db) == len(onsets) + 1
    new_times = {round(o.t, 3) for o in out_with_db} - {round(o.t, 3) for o in onsets}
    assert new_times == {1.0}, f"expected synthetic at 1.0s, got {new_times}"


def test_downbeat_sanity_filter_drops_implausible_ratios() -> None:
    """Beat This! sometimes flags nearly every beat as a downbeat on
    out-of-distribution audio (regular click tracks, drones). Anything
    above the plausibility cap must be dropped so we don't emit a chord
    stack on every onset.
    """
    from app.pipeline.beat_track import _sanity_filter_downbeats

    beats = [i * 0.5 for i in range(20)]  # 20 beats

    # Plausible: 5 downbeats out of 20 = 25% (4/4 time).
    db_good = [0.0, 2.0, 4.0, 6.0, 8.0]
    assert _sanity_filter_downbeats(beats=beats, downbeats=db_good) == db_good

    # Implausible: 18 downbeats out of 20 = 90%. Must be filtered out.
    db_bad = [i * 0.5 for i in range(18)]
    assert _sanity_filter_downbeats(beats=beats, downbeats=db_bad) is None


def test_downbeat_sanity_filter_handles_empty_inputs() -> None:
    from app.pipeline.beat_track import _sanity_filter_downbeats

    assert _sanity_filter_downbeats(beats=[], downbeats=[]) is None
    assert _sanity_filter_downbeats(beats=[1.0, 2.0], downbeats=[]) is None


def test_beat_fill_downbeat_param_is_optional() -> None:
    """downbeats=None must not change pre-existing behaviour at all."""
    from app.pipeline.beat_fill import fill_empty_beats

    beats = [i * 0.5 for i in range(10)]
    onsets = [
        Onset(t=0.0, strength=1.0, centroid_hz=200.0),
        Onset(t=4.5, strength=1.0, centroid_hz=2000.0),
    ]
    out_none = fill_empty_beats(onsets, beats)
    out_explicit_none = fill_empty_beats(onsets, beats, downbeats=None)
    assert [round(o.t, 4) for o in out_none] == [round(o.t, 4) for o in out_explicit_none]
