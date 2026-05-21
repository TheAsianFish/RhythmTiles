"""Tests for learned-lane integration's fallback behaviour.

Validates the non-negotiable property: turning on USE_LEARNED_LANES
when no trained model exists must NOT break chart generation. The
pipeline silently falls back to the rule-based assigner.

Smoke-tests use synthetic audio (a few hundred ms of sine wave) so the
suite runs without external deps or network.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from app.ml import learned_lanes


@pytest.fixture(autouse=True)
def _reset_learned_lanes_singleton():
    """Each test starts with a fresh module-level cache so state from a
    prior test (model loaded / load failed) doesn't bleed in."""
    learned_lanes.reset_for_tests()
    yield
    learned_lanes.reset_for_tests()


def test_is_available_returns_false_when_no_artifact(tmp_path, monkeypatch):
    """Without a trained model on disk, is_available() returns False AND
    the failure is latched so subsequent calls don't re-attempt the load."""
    monkeypatch.setenv("LEARNED_LANES_MODEL_DIR", str(tmp_path / "nonexistent"))
    assert learned_lanes.is_available() is False
    # Second call is cached as failed; still False, no exception.
    assert learned_lanes.is_available() is False


def test_assign_lanes_learned_returns_none_when_model_missing(tmp_path, monkeypatch):
    """The pipeline's caller treats None as "fall back to rule-based"."""
    from app.pipeline.onset_detect import Onset
    from app.pipeline.stems import Stems

    monkeypatch.setenv("LEARNED_LANES_MODEL_DIR", str(tmp_path / "nonexistent"))

    # Stub the rest. None of these get exercised because we bail at model load.
    fake_audio = np.zeros(22050, dtype=np.float32)
    fake_stems = Stems(
        drums=fake_audio,
        vocals=fake_audio,
        bass=fake_audio,
        other=fake_audio,
        sr=22050,
        separated=False,
    )

    class _FakeBeatInfo:
        beats = [0.5, 1.0, 1.5]
        downbeats: list[float] = []
        bpm = 120.0
        bpm_curve = None
        source = "fake"

    onsets = [Onset(t=0.5, strength=1.0, centroid_hz=1000.0)]
    result = learned_lanes.assign_lanes_learned(
        onsets=onsets,
        y=fake_audio,
        sr=22050,
        beat_info=_FakeBeatInfo(),
        stems=fake_stems,
        mert_sections=[],
    )
    assert result is None


def test_assign_lanes_learned_returns_empty_for_empty_onsets():
    """Empty input -> empty output, regardless of model availability."""
    from app.pipeline.stems import Stems

    fake_audio = np.zeros(22050, dtype=np.float32)
    fake_stems = Stems(
        drums=fake_audio,
        vocals=fake_audio,
        bass=fake_audio,
        other=fake_audio,
        sr=22050,
        separated=False,
    )

    class _FakeBeatInfo:
        beats = [0.5]
        downbeats: list[float] = []
        bpm = 120.0

    result = learned_lanes.assign_lanes_learned(
        onsets=[],
        y=fake_audio,
        sr=22050,
        beat_info=_FakeBeatInfo(),
        stems=fake_stems,
        mert_sections=[],
    )
    assert result == []


def test_settings_default_keeps_learned_lanes_off(monkeypatch):
    """Default-off contract: USE_LEARNED_LANES unset => settings.use_learned_lanes is False."""
    monkeypatch.delenv("USE_LEARNED_LANES", raising=False)
    # Reload settings to pick up the cleared env.
    from importlib import reload

    from app import config

    reload(config)
    assert config.settings.use_learned_lanes is False


def test_settings_honour_truthy_strings(monkeypatch):
    monkeypatch.setenv("USE_LEARNED_LANES", "1")
    from importlib import reload

    from app import config

    reload(config)
    assert config.settings.use_learned_lanes is True


def test_cache_key_unchanged_when_learned_lanes_off(monkeypatch):
    """Backward compat: with USE_LEARNED_LANES=0 the cache key is byte-identical
    to the pre-Phase-5 format so old cache rows still match."""
    monkeypatch.delenv("USE_LEARNED_LANES", raising=False)
    from importlib import reload

    from app import config
    from app.routes import charts as charts_route

    reload(config)
    reload(charts_route)
    key = charts_route._mode_key()
    assert "-ln" not in key, f"learned-lanes suffix must not appear when off, got {key!r}"


def test_cache_key_appends_ln1_when_learned_lanes_on(monkeypatch):
    monkeypatch.setenv("USE_LEARNED_LANES", "1")
    from importlib import reload

    from app import config
    from app.routes import charts as charts_route

    reload(config)
    reload(charts_route)
    key = charts_route._mode_key()
    assert key.endswith("-ln1"), f"expected -ln1 suffix, got {key!r}"


def test_distribution_correction_penalizes_over_represented_lanes():
    """When the recent window is F/J-heavy, F/J logits get penalized so a
    D or K that was close to winning now wins. This is the postprocessor
    that fights the v0.1/v0.2 F/J bias on Western pop."""
    import numpy as np

    # Recent window: 12 F/J notes, 0 D/K notes. Maximum imbalance.
    recent = [1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2]
    # Raw logits: lane 1 (F) slightly wins, lane 0 (D) close behind.
    raw_logits = np.array([1.0, 1.2, 0.4, 0.3], dtype=np.float32)
    adjusted = learned_lanes._apply_distribution_correction(
        logits=raw_logits, recent_lanes=recent,
    )
    # F and J each got ~50% of recent, so each is penalized by
    # (0.5 - 0.25) * 1.5 = 0.375. D and K were 0% so they get 0 penalty.
    # After: lane 0 (D) at 1.0 should now beat lane 1 (F) at 1.2 - 0.375 = 0.825.
    assert int(np.argmax(adjusted)) == 0


def test_distribution_correction_noop_when_window_too_small():
    """Don't correct during the first few notes — signal isn't stable."""
    import numpy as np

    raw_logits = np.array([0.1, 0.9, 0.0, 0.0], dtype=np.float32)
    adjusted = learned_lanes._apply_distribution_correction(
        logits=raw_logits, recent_lanes=[1, 2, 1],  # below _RECENT_WINDOW / 2
    )
    np.testing.assert_array_equal(adjusted, raw_logits)


def test_pick_lane_with_constraints_blocks_third_same_hand():
    """After 2 left-hand picks, the postprocessor blocks a 3rd left-hand
    pick even if the model's argmax says so."""
    import numpy as np

    # Model wants lane 1 (F = left hand) again. recent_lanes ends with two
    # left-hand picks already.
    logits = np.array([0.2, 1.0, 0.5, 0.3], dtype=np.float32)
    last_hit = [-1e9, -1e9, -1e9, -1e9]
    recent = [0, 1]  # D then F = two left-hand
    lane = learned_lanes._pick_lane_with_constraints(
        logits=logits, last_hit=last_hit, onset_t=10.0, recent_lanes=recent,
    )
    # Should NOT be 0 or 1 (left hand); must be 2 or 3 (right hand). The
    # second-best right-hand logit is lane 2 at 0.5, so it should win.
    assert lane in (2, 3)


def test_pick_lane_relaxes_hand_balance_when_anti_cluster_blocks_other_hand():
    """If both right-hand lanes are too recent, fall back to a left-hand
    pick even though it violates the streak rule, rather than drop the note."""
    import numpy as np

    logits = np.array([0.2, 1.0, 0.5, 0.3], dtype=np.float32)
    # Both right-hand lanes used VERY recently (less than HIT_WINDOW_S ago).
    last_hit = [-1e9, -1e9, 9.99, 9.98]
    recent = [0, 1]  # two left-hand in streak
    lane = learned_lanes._pick_lane_with_constraints(
        logits=logits, last_hit=last_hit, onset_t=10.0, recent_lanes=recent,
    )
    # Right hand blocked by anti-cluster, hand-balance forces left.
    # Pass 2 picks the best left-hand lane, which is lane 1 (F) at 1.0.
    assert lane == 1
