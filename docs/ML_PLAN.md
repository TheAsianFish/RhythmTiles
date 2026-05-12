# ML Plan (architecture only — do not execute yet)

This document is the agreed plan for bringing ML into BeatBridge. It is
deliberately not implemented in code; the goal here is to make sure that
when we commit time to ML, it pays off and doesn't regress the heuristic
baseline.

## Status

Not started. Stubs exist at `backend/app/ml/`. The heuristic pipeline
under `backend/app/pipeline/` remains the production path.

## Why ML now

The heuristic pipeline has hit two ceilings:

1. **Polyphonic events.** We cannot tell a single loud note from a
   simultaneous chord without polyphonic transcription. The centroid-
   aware merge gets close on adjacent-pitch events 20-30ms apart but
   misses truly simultaneous chord strikes.

2. **Lane-assignment quality.** The current rule-based assigner
   (centroid band + hand balance) is fine but predictable. A human
   mapper would route specific instruments to specific lanes in ways
   we can't easily codify (kick on outer-left, snare on outer-right,
   hat alternating inner) — but a model trained on community charts
   could learn this.

Both ceilings genuinely require ML. The heuristic version is "good
enough to play"; ML is the path to "feels hand-mapped."

## Goals

- **Match hand-mapped feel.** Per-note granularity on dense polyphonic
  sections. Lane choices that respect instrument identity.
- **Preserve the 60s cold-start / 2s warm budgets.** Either model
  inference fits or we accept higher cold-start with stronger caching.
- **Keep the heuristic pipeline as the rollback.** Every ML module is
  gated behind an env flag and falls back to the heuristic if it errors
  or isn't installed (same pattern as the existing Demucs scaffold).

## Non-goals (v1)

- Real-time inference on the user's machine. All ML runs server-side.
- Full end-to-end "audio → osu!mania chart" model. We compose smaller
  models, each replacing a specific heuristic stage.
- Self-trained models for everything. We use pretrained models off-the-
  shelf wherever possible. Training is reserved for the lane-assignment
  classifier where pretrained models don't exist.

## Phased plan

Each phase is independently shippable and gated behind its own env flag.
A phase only progresses when the previous one is validated against the
heuristic baseline.

### Phase 1: Activate source separation (Demucs)

**Status:** scaffolded behind `USE_DEMUCS=1`; needs real activation +
validation.

**What changes:** onset detection runs on the drum stem (already wired);
add separate paths for vocal-stem onset detection and bass-stem energy
curve for chorus detection.

**Models considered:** _(filled by research findings)_

**Choice + rationale:** _(filled by research findings)_

**Validation:** generate the same song heuristic-vs-Demucs side by side,
listen with verify_sync. Pass if Demucs produces cleaner onset times on
3 of 4 vocal-heavy test tracks.

**Latency cost:** _(filled by research findings)_

**Rollback:** flip USE_DEMUCS=0. Pipeline falls back to full-mix onsets.

### Phase 2: Polyphonic transcription

**What changes:** replace heuristic onset detection on the vocal/melody
stem with a polyphonic transcriber that returns (time, pitch, duration)
events. Onset times become more precise; lane assignment can use pitch
to route by melodic contour.

**Models considered:** _(filled by research findings)_

**Choice + rationale:** _(filled by research findings)_

**Validation:** chord-rate stays in target band (3-8%) but more chord
events are real polyphonic strikes rather than detection coincidences.
A/B against Phase 1 baseline on 5 songs across genres.

**Latency cost:** _(filled by research findings)_

**Rollback:** flag-gated. Falls back to Phase 1 onset detection.

### Phase 3: Learned lane assignment

**What changes:** replace the rule-based `lane_assign.py` with a
classifier trained on osu!mania community charts. Each candidate note
gets features (audio centroid, pitch from Phase 2, beat position, local
density, stem source from Phase 1) and the model predicts the lane.

**Models considered:** small gradient-boosted decision trees (xgboost),
2-layer MLP, transformer-encoder over the local-window of notes.

**Choice (provisional):** gradient boost first (interpretable,
fast inference, doesn't need GPU). Transformer if GB caps out.

**Training data:** osu!mania Ranked + Approved + Loved charts. Licensed
under Creative Commons. Pipeline: download from `osu!`'s public Beatmap
listing API, render each chart's audio via the bundled .osu + .mp3
files, extract per-note features, save as Parquet.

**Validation:**
- Hold-out 20% of charts as test set; model accuracy must beat
  rule-based assignment on lane-prediction (target: 65%+ exact match).
- Subjective playtest: 5 songs at "expert" tier with model lanes vs
  rule-based lanes. Players prefer model in 3 of 5 blind comparisons.

**Rollback:** flag-gated. Falls back to Phase 2 onset times + rule-based
lane assignment.

**Effort:** multi-week. Data collection + training + serving. Don't
start until Phase 1 and 2 are stable.

### Phase 4 (future): End-to-end model

Reserved for after Phase 3 is shipped and validated. Would replace the
entire `chart_builder.py` pipeline with an audio → chart transformer.
Probably not worth attempting until we have user-rating data to use as
the training signal. Stage 6 territory.

## Data + licensing

- **Demucs**: MIT license; weights downloadable via the `demucs` package.
- **Basic Pitch / MT3 / Onsets+Frames**: _(filled by research)_
- **osu!mania community charts**: Creative Commons (CC BY-NC for most
  ranked maps, check per-map). Audio extracted from `.osz` archives.
  Storage: ~10K charts at ~30MB each = ~300GB. Use a subset (~1K) for
  initial training; expand if model underfits.

## Validation methodology

The heuristic baseline must not regress. Every ML addition runs in
parallel with the heuristic for a phase before replacing. Compare:

1. **Sync drift**: `verify_sync` tool generates `mix.wav` with clicks at
   each note time. ML and heuristic both produce a mix; a blind A/B
   listening test (3 songs, 5 listeners) picks the better-synced one.
2. **Note count**: should stay within ±15% of the heuristic baseline at
   each difficulty.
3. **Chord rate**: should stay within 3-8% target band.
4. **Cold-start time**: must not exceed 90s on the test box (was 60s
   target, allow 50% slack for ML overhead).
5. **Cached-generation time**: unchanged (cache is downstream of ML).

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Models that look great in papers flop on real YouTube audio | Validate each on 5+ real songs before committing |
| Cold-start budget blown | Cache stems + transcriptions per audio hash, just like charts |
| License issues on osu!mania charts | Use ranked maps only; honor per-chart CC terms; cite contributors |
| GPU required at scale | Phase 1 + Phase 2 chosen for CPU viability; Phase 3 is the GPU question |
| Model output is worse than heuristic | A/B testing required at each phase; rollback flag preserved |
| Drift in upstream model APIs (HF, package updates) | Pin versions; vendor where critical |

## Open decisions

These need answers before Phase 1 starts:

1. **Where does inference run?** Local GPU box, Modal, Replicate, or
   something else? Affects cold-start latency assumptions.
2. **Stem caching key**: by audio hash (canonical) or by videoId
   (faster but doesn't survive re-uploads)?
3. **Per-stem onset detection**: run on all four stems and merge, or
   only drums + vocals?
4. **Polyphonic transcription strictness**: emit every detected note,
   or filter by confidence? Confidence cutoff is a chord-rate knob.

## Effort estimate (revisit at each phase)

| Phase | Effort | Latency cost | Quality jump |
|---|---|---|---|
| 1: Demucs | ~2 days (already scaffolded) | +5-15s cold start | Medium |
| 2: Transcription | ~3-5 days | +5-30s cold start | Large for polyphonic |
| 3: Lane model | 2-4 weeks (data + training + serving) | <1s inference | Medium-large |
| 4: End-to-end | Reserved | Unknown | Unknown |

## Decision log (filled as decisions land)

- _(empty)_
