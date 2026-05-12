# app/ml

Stub package. The first real modules land here when `docs/ML_PLAN.md` is
executed. Until then this directory exists so:

1. Imports of `app.ml.*` don't surprise the rest of the codebase when a
   module lands later.
2. The architecture is visible in the tree (you can see at a glance where
   ML lives, separate from the existing librosa pipeline).
3. Tests / CI hooks can be wired up incrementally without restructuring.

## Naming convention

Each module is named for the task it performs, not the model that
performs it. The plan calls out the chosen model per module so we can
swap implementations without renaming files. Examples:

- `stems_demucs.py` — source separation (currently planned: Demucs htdemucs)
- `transcribe.py` — polyphonic transcription (currently planned: Basic Pitch)
- `lane_model.py` — learned lane assignment (custom model; not planned for v1)

## Why this is separate from `app/pipeline/`

The librosa pipeline in `app/pipeline/` is the production path today. ML
modules are optional and gated behind env flags (`USE_DEMUCS`, future
`USE_TRANSCRIBE`, etc.) so they don't change deploy footprint until a
flag flips. Mixing the two would make the heuristic baseline harder to
preserve as a rollback.
