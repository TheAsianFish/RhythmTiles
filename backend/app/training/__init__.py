"""Offline training pipeline for the learned-lane model (Phase 5).

This package is NOT imported by the serving code path. The inference
side lives at `app.ml.learned_lanes` and only depends on the trained
artifact, not on this package's dependencies (osu! API client, parquet,
LightGBM trainer). Keeping serving and training import-isolated means
the production wheel can ship without dragging in training-only deps.

See docs/PHASE5_PLAN.md for the full plan.
"""
