"""ML pipelines for BeatBridge chart generation.

Active modules (see docs/ML_PLAN.md for the full plan):
  beat_this.py     Beat + downbeat detector (CPJKU Beat This!, Phase 1).
                   Activated by USE_BEAT_THIS=1. Falls back to librosa.
  beat_cache.py    Disk cache for beat-tracker output by audio content
                   hash so regeneration at multiple difficulties
                   doesn't re-run the model.
  onset_cache.py   Disk cache for per-stem onset lists (Phase 2).
                   Makes Demucs a one-time-per-song cost.
  mert.py          MERT audio embeddings for section detection
                   (Phase 3). Activated by USE_MERT=1. Falls back to
                   RMS-based bucketing.
  sections.py      Pure section-clustering logic from MERT embeddings.
                   No ML deps; safe to import without transformers.
  section_cache.py Disk cache for MERT-derived section labels.

Planned (not yet implemented):
  lane_model.py    Learned lane assignment trained on osu!mania (Phase 5).

Source separation (Demucs) lives at app/pipeline/stems.py because it
predates this package; that path activates when USE_DEMUCS=1 and feeds
per-stem onset detection in app/pipeline/onset_detect.py.
"""

from app.ml import beat_cache, beat_this, mert, onset_cache  # noqa: F401
from app.ml import section_cache, sections  # noqa: F401
