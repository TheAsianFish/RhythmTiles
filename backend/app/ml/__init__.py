"""ML pipelines for BeatBridge chart generation.

This package is intentionally empty at the architecture-only stage.
Modules will land here when the ML plan in docs/ML_PLAN.md is executed.

Expected layout (see ML_PLAN.md for what each does):
  stems_demucs.py      Source separation via Demucs (real, not the scaffold)
  transcribe.py        Polyphonic transcription (Basic Pitch / MT3)
  lane_model.py        Learned lane assignment (osu!mania-trained classifier)
  embeddings.py        Audio embeddings for downstream tasks
"""
