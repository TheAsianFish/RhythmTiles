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
