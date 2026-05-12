"""ML pipelines for BeatBridge chart generation.

Active modules (see docs/ML_PLAN.md for the full plan):
  beat_this.py     Beat + downbeat detector (CPJKU Beat This!, Phase 1).
                   Activated by USE_BEAT_THIS=1. Falls back to librosa.
  beat_cache.py    Disk cache for beat-tracker output, keyed by audio
                   content hash so regeneration at multiple difficulties
                   doesn't re-run the model.

Planned (not yet implemented):
  embeddings.py    MERT embeddings for section detection (Phase 3).
  lane_model.py    Learned lane assignment trained on osu!mania (Phase 5).

Source separation (Demucs) lives at app/pipeline/stems.py because it
predates this package; that path activates when USE_DEMUCS=1 and feeds
per-stem onset detection in app/pipeline/onset_detect.py.
"""

from app.ml import beat_cache, beat_this  # noqa: F401
