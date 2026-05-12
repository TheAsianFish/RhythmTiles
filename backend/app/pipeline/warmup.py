"""Pipeline warmup.

librosa lazy-imports several modules and numba JITs onset_strength helpers on
first call. The cold path of the pipeline pays ~3 seconds for that work; we
trigger it once at app startup against a tiny synthetic buffer so the first
real user request lands on the warm path.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("beatbridge.warmup")


def warm_pipeline() -> float:
    """Run a tiny pipeline pass to JIT-compile everything. Returns elapsed seconds.

    Also warms the Beat This! detector when USE_BEAT_THIS=1 so the first
    user request doesn't pay the ~1-3s weight-load cost. Safe to call from
    a background thread.
    """
    import numpy as np

    from app.config import settings
    from app.ml import beat_this
    from app.pipeline.beat_track import detect_beats
    from app.pipeline.onset_detect import detect_onsets

    sr = 22050
    # 2 seconds of noise + a couple of clicks. Enough to exercise beat tracking
    # and onset detection without spending real cold-start time.
    rng = np.random.default_rng(0)
    y = (rng.standard_normal(sr * 2) * 0.05).astype(np.float32)
    for k in (0, sr // 2, sr, (sr * 3) // 2):
        y[k : k + 256] += 0.6

    t0 = time.perf_counter()
    if settings.use_beat_this:
        beat_this.warm()  # no-op when the package isn't importable
    detect_beats(y=y, sr=sr)
    detect_onsets(y=y, sr=sr)
    elapsed = time.perf_counter() - t0
    logger.info("pipeline warmup complete in %.2fs", elapsed)
    return elapsed
