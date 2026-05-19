# ML Plan (architecture only — do not execute yet)

This document is the agreed plan for bringing ML into BeatBridge. It is
deliberately not implemented in code; the goal is to make sure that
when we commit time to ML, it pays off and doesn't regress the
heuristic baseline.

## Status

Phases 1, 2, and 3 are all ACTIVE behind their env flags. The
heuristic pipeline remains the production default; ML is opt-in per
request via `USE_BEAT_THIS=1`, `USE_DEMUCS=1`, and `USE_MERT=1`. All
three flags fall back to their baseline on package or runtime failure.

  - `app/ml/beat_this.py` wraps the Beat This! detector behind the
    same BeatInfo interface as librosa, plus a downbeat list and a
    derived bpmCurve. Beat This! has a known failure mode on
    out-of-distribution audio (regular click tracks, drones) where
    it flags nearly every beat as a downbeat. A sanity filter in
    `_detect_with_beat_this` drops the downbeat list when the
    downbeat/beat ratio exceeds 0.55 (above every plausible time
    signature; the lane assigner falls back to strength/centroid
    accents when this happens).
  - `app/ml/beat_cache.py` persists beat-tracker output keyed by
    audio content hash so regeneration at multiple difficulties
    doesn't re-run the model.
  - `app/ml/onset_cache.py` persists per-stem onset lists by audio
    content hash. Demucs is the slowest stage by an order of
    magnitude; this cache makes its cost a one-time-per-song
    payment rather than once-per-difficulty.
  - `app/pipeline/stems.py` activates Demucs htdemucs separation
    when `USE_DEMUCS=1`. Falls through to a pass-through Stems
    object otherwise.
  - `app/pipeline/onset_detect.py` exposes `detect_onsets_per_stem`
    that tags each onset with `stem="drums"` / `"vocals"`.
  - `app/pipeline/lane_assign.py` routes by stem when available
    (drums -> low band, vocals -> high band) and emits chord stacks
    on real downbeats regardless of strength/centroid.
  - `app/ml/mert.py` wraps the HuggingFace MERT-v1-95M model with
    module-level caching, thread lock, and graceful fallback.
    Resamples mono 22050Hz audio to MERT's native 24kHz, mean-pools
    the 13 transformer hidden states into a single per-frame
    embedding.
  - `app/ml/sections.py` clusters MERT embeddings into intensity-
    labeled sections (no ML deps; pure numpy + scikit-learn).
    Coarsens to ~1 fps, KMeans K=4, smooths, merges short runs,
    labels each section by mean RMS into 3 intensity buckets.
  - `app/ml/section_cache.py` persists section labels by audio hash
    so MERT runs once per song no matter how many difficulties.
  - `app/pipeline/chart_builder.py` uses MERT section buckets to drive
    `_energy_buckets_for_notes` when active, and populates the
    previously-empty `Chart.sections` field on the wire JSON.
  - `app/tools/bench_ml_phase.py` runs the ML_PLAN validation gates
    against any WAV with `--baseline-only`, `--beat-this`,
    `--demucs`, or `--mert` flags.

All ML phases (1, 2, 3) are now flag-gated and shipping. Phase 4
(polyphonic transcription) remains DEFERRED. Phase 5 (learned lane
assignment) remains DEFERRED.

## Honest reframe from research

The instinct "ML for chart generation = polyphonic transcription" is
WRONG for this project. Two reasons surfaced by research:

1. **Polyphonic transcription on YouTube audio underperforms.** Basic
   Pitch's frame accuracy drops on dense pop mixes (~63% on vocals,
   worse on full-band). MT3 is JAX/T5X-only and shows real-audio
   regressions in published evals. Turning every song into a MIDI
   piano roll makes mania charts feel like a different game.
2. **Lane assignment is genuinely unsolved by ML.** No off-the-shelf
   model produces rhythm-game lane assignments. Even Mapperatorinator
   (the only direct "audio → osu! tokens" model published, Whisper-
   based, ~219M params) is reported by the osu! community itself as
   "not a replacement for mappers." Rule-based assignment on stem-
   labeled onsets is what shipped community charts effectively use.

The biggest no-pushback quality jump available right now is replacing
`librosa.beat.beat_track` with **Beat This!** (CPJKU, ISMIR 2024). It
is SOTA F1 on beat + downbeat detection, pip-installable, tens of MB,
and a drop-in functional replacement.

Sources: [Beat This! repo](https://github.com/CPJKU/beat_this),
[2025 AMT Challenge](https://ai4musicians.org/transcription/2025transcription.html),
[Spotify Basic Pitch engineering blog](https://engineering.atspotify.com/2022/6/meet-basic-pitch),
[Mapperatorinator](https://github.com/OliBomby/Mapperatorinator),
[osu! community thread on AI beatmaps](https://osu.ppy.sh/community/forums/topics/1899264).

## Why ML now

The heuristic pipeline has hit two real ceilings:

1. **Beat-grid quality on tempo-shifting songs.** librosa.beat is good
   at steady-tempo pop but stumbles on tempo curves and on
   non-percussive-driven music. Beat This! fixes this.
2. **Section detection by RMS alone is crude.** Chorus vs verse vs
   bridge classification from full-mix RMS misses obvious cases
   (vocal-driven choruses with quiet drums). MERT embeddings or
   stem-aware energy curves would do this properly.

Polyphonic transcription is *not* on this list. The research convinced
me that going to MIDI-style per-pitch events would hurt mania feel.

## Goals

- **Beat grid accuracy** at parity with hand-mapped charts.
- **Section detection** that distinguishes chorus from verse on songs
  the current RMS-based logic misclassifies.
- **Per-stem onset detection** so drum hits drive lanes 0/3 and vocal
  attacks drive 1/2 naturally, instead of all signal coming from the
  full mix's spectral centroid.
- **Preserve the 60s cold-start / 2s warm budgets** — model warmup is
  the long tail risk; budget +30s grace.
- **Heuristic pipeline stays the rollback.** Every ML module is gated
  behind an env flag and falls back to the heuristic on error / not
  installed (same pattern as the existing Demucs scaffold).

## Non-goals (v1)

- **Polyphonic transcription** (Basic Pitch, MT3, etc.). Research
  pushed back: dense pop audio underperforms and the output reframes
  the game incorrectly. Reconsider only if a specific failure case
  surfaces that the per-stem onset pipeline can't address.
- **End-to-end audio → chart model** (Mapperatorinator and the like).
  Community feedback indicates it doesn't replace mappers; we'd
  inherit its baked-in style choices we can't tune.
- **On-device inference.** All ML runs server-side.
- **madmom dependency.** It's effectively unmaintained on Python 3.10+
  ([beat_this issue #9](https://github.com/CPJKU/beat_this/issues/9),
  [Snyk advisor](https://snyk.io/advisor/python/madmom)). Hard avoid.

## Phased plan

Each phase is independently shippable, env-flag-gated, and rollback-
safe. A phase only progresses when the previous one passes validation.

---

### Phase 1: Beat This! upgrade (highest priority)

**What changes.** Replace `app/pipeline/beat_track.py`'s
`librosa.beat.beat_track` call with Beat This! inference. Returns
beats AND downbeats (we don't have downbeat detection today). Drop in
behind a `USE_BEAT_THIS=1` env flag; falls back to librosa when off.

**Model.** [CPJKU/beat_this](https://github.com/CPJKU/beat_this).
Pure neural, no DBN post-processing. License: MIT. Weights tens of
MB. CPU is borderline-realtime; GPU comfortable.

**Why first.** Research called this the single biggest jump for
"hand-mapped feel" because the whole chart's musicality is anchored
to the beat grid. Every other stage (subdivision, density bucketing,
beat-fill, snap-to-beat) compounds beat-grid errors. Fixing the root
fixes everything downstream.

**New capability unlocked.** Downbeat detection lets us:
- Tag the bpmCurve field with a tempo curve, not just a single bpm.
- Emit "accent" chord stacks on actual downbeats instead of
  guessing from strength quantile + centroid.
- Use measure boundaries for difficulty pacing (verses on the and-
  count, choruses on the downbeat).

**Latency cost.** First call: model load ~3-5s + ~1-3s inference on
a 4-minute song (CPU). Warm: <1s. Cache the beat output by audio
hash; reuse on chart regeneration.

**Validation.**
- Synthetic click-track test: Beat This! BPM within 2% of ground
  truth on 120/140/172/95 BPM tracks.
- Hand-tap A/B: tap to 5 real songs; Beat This! deltas < 30ms on
  4 of 5. librosa baseline today: ~80ms on tempo-shifting tracks.
- No regression on `tests/test_pipeline_synth.py`.

**Rollback.** `USE_BEAT_THIS=0`. Fall back to librosa.

**Effort.** ~2 days. Install, wrap the call, threading dropout
handling, cache integration.

---

### Phase 2: Activate Demucs (already scaffolded)

**What changes.** Flip `USE_DEMUCS=1` from "scaffolded path that
no-ops" to "real htdemucs_ft inference." onset detection routes to
the drum stem (already wired); add vocal stem for melody onsets.

**Model.** htdemucs_ft (Demucs v4 fine-tuned variant) via the
`demucs` pip package. MIT license, ~80MB weights, auto-download on
first call.
[GitHub](https://github.com/facebookresearch/demucs)

**Why second.** Research recommended this paired with Beat This! as
the no-regret combo. Drum-stem onsets give cleaner kick/snare
timing than the full-mix multi-onset path. Vocal-stem onsets fix the
remaining vocal-pickup gaps that HPSS doesn't fully cover.

**Considered alternatives:**
- BS-RoFormer (2 dB better SDR than Demucs but no clean pip package,
  weights community-distributed with unclear license).
- Mel-RoFormer (similar to BS-RoFormer).
- Spleeter (older, lighter, but worse SDR than Demucs).
[BS-RoFormer paper](https://arxiv.org/abs/2310.01809),
[2026 benchmark](https://dev.to/codesugar_lin_037a57b06a4/htdemucs-vs-bs-roformer-vs-spleeter-a-2026-audio-source-separation-benchmark-2ll8)

**Latency cost.** CPU: ~0.1x realtime (4-minute song = ~3 minutes).
GPU: ~3x realtime (~15s on RTX 3060 Ti). Aggressive caching mandatory.

**Storage cost.** ~80MB model + cached stems per song. Stems can be
discarded after onset extraction; only the cached onset list needs
to persist.

**Validation.**
- A/B chart generation on 5 songs of varied genre. Listen with
  `verify_sync`; Demucs onsets feel cleaner on 3 of 5 vocal-heavy.
- Chord-rate stays in target band (3-8%).
- Cold start: +15-30s acceptable on GPU; defer to async stem job on
  CPU-only deploy.

**Rollback.** `USE_DEMUCS=0`. Pipeline falls back to full-mix +
HPSS onsets.

**Effort.** ~3 days. Real Demucs wiring (the scaffold is a stub),
cold-start cache layer, per-stem onset detection paths.

---

### Phase 3: MERT for section detection

**What changes.** Replace the RMS-based energy bucketing in
`app/pipeline/chart_builder.py:_energy_buckets_for_notes` with MERT
embeddings of the audio. Cluster embeddings to label sections (intro,
verse, chorus, bridge, outro). Use labels to drive density variation
more precisely than RMS quantile.

**Model.** [MERT-v1-95M](https://huggingface.co/m-a-p/MERT-v1-95M)
or 330M variant. CC-BY-NC (verify if commercial). HuBERT-style.
1024-d embeddings.
[MERT paper](https://arxiv.org/abs/2306.00107)

**Why third.** Energy curves miss vocal-driven choruses with quiet
drums. MERT embeddings encode musical structure (genre, mood,
section) and segmenting them produces real section labels. Phase 2's
stems also help here (vocal-stem energy is the cleanest chorus proxy
on vocal-led music).

**Latency cost.** ~380MB model load (~5s warmup), then ~0.5s for a
4-minute song on CPU. Faster on GPU. Embeddings cache by audio hash.

**Validation.**
- Manual section labels on 3 test songs (intro/verse/chorus/etc.
  with timestamps). MERT clustering must match >70% by time.
- Chart density variation in choruses must be visibly higher than
  verses on songs that currently fail the RMS-only check.

**Rollback.** Flag-gated; falls back to RMS bucketing.

**Effort.** ~5 days. MERT integration (HF Hub, `trust_remote_code`),
clustering + label assignment, integration into density bucket logic.

---

### Phase 4 (deferred): polyphonic transcription

Not planned. Research recommended AGAINST. Reopen only if a specific
song-class emerges that Phase 1-3 can't handle. The condition for
reopening: a real song where the per-stem-onset path consistently
fails on polyphonic events the player expects.

If we reopen, candidates would be:
- Basic Pitch (light, ONNX, but flops on dense pop)
- MT3 (heavy, JAX-only, real-audio regression)
- An MT3 fine-tune on mania charts (multi-week effort)

---

### Phase 5: learned lane assignment

ACTIVE. See `docs/PHASE5_PLAN.md` for the full execution roadmap.

Summary: train a per-onset LightGBM classifier (4-way, lane 0-3) on
osu!mania 4K Ranked + Approved community charts. Trained at the
chart's own hit times so onset-detection-to-chart alignment never
becomes a failure mode. Inference applies the same feature schema
at our detector's onset times. Falls back to the rule-based
assigner on missing model / inference error / unsupported
features. Gated behind `USE_LEARNED_LANES=1`.

Research corrections vs the original Phase 5 sketch:

- No clean CC-BY-NC license on osu!mania charts; community operates
  on research-fair-use posture with explicit no-redistribution.
- `.osz` (audio + chart) requires user-grant OAuth; `.osu` (chart
  only) is unauth via `osu.ppy.sh/osu/{id}`. Use community mirrors
  (nerinyan.moe) for audio bundle.
- Prior art to borrow from: GOCT (ISMIR 2023) tokenization,
  Mania Archetype (2024) evaluation methodology, BeatLearning's
  mania encoding. Mapperatorinator is reference, not a fine-tune
  target.

Effort: ~7 working days for v1 LightGBM classifier. v2 sequence
model is another ~7 days if v1 plateaus below human-feel target.

---

## Validation methodology (locked)

The heuristic baseline must not regress at any phase. Every ML
addition runs in parallel for one phase before replacing.

Per-phase A/B:
1. **Sync drift.** `verify_sync` produces `mix.wav` with click on
   each note. Listener A/B picks the better-synced mix on 3 songs
   across 5 listeners. Phase must win 3 of 5.
2. **Chord rate.** Stays in 3-8% target band.
3. **Note count.** Within ±15% of heuristic baseline at each
   difficulty.
4. **Cold-start time.** Must not exceed 90s on the test box (60s
   target, 50% slack for ML overhead).
5. **Cached-generation time.** Unchanged (cache downstream of ML).

Add to `app/tools/`:
- `bench_ml_phase.py` runs the validation checklist for each phase
  across the test-song corpus and prints pass/fail per criterion.

## Data + licensing

- **Demucs** (Phase 2): MIT license; weights from `demucs` pip
  package on first inference.
- **Beat This!** (Phase 1): MIT license; weights via the GitHub repo
  release artifacts. Pin the commit.
- **MERT** (Phase 3): CC-BY-NC. Acceptable for non-commercial demo
  but flag for relicensing review before shipping commercial.
- **osu!mania charts** (Phase 5 only): CC-BY-NC for ranked maps.
  Honor per-chart terms. Defer data collection until Phase 5 is
  approved.
- **Audio rights.** Demucs MIT covers the *code*, not the audio we
  feed it. The "user's-tab-audio-not-redistribution" stance in
  CLAUDE.md is correct; preserve it.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Models flop on real YouTube audio (Basic Pitch precedent) | Validate each on ≥5 real songs before committing; rollback flag preserved per phase |
| Cold-start budget blown | Per-audio-hash cache for stems, beats, embeddings; pre-warm Modal containers |
| GPU required at scale | Phases 1+2+3 chosen for CPU-feasibility; GPU optional accelerator |
| License surprises (BS-RoFormer, MERT-NC) | Stick to MIT/Apache for v1; relicense MERT before shipping commercial |
| madmom transitive dep | Avoid madmom entirely; only Beat This! and direct librosa allowed |
| Model API drift (HF, package updates) | Pin all model versions / commit hashes; vendor critical code |
| ONNX/TRT export reduces inference cost but adds dev cost | Skip in v1; revisit when deploy hosting is fixed and per-call cost is known |

## Open decisions (need answers before Phase 1 starts)

1. **Where does inference run?** Local GPU box, Modal, Replicate, or
   self-hosted? Affects cold-start latency assumptions and recurring
   cost.
2. **Async or sync chart generation?** Demucs at CPU is too slow for
   sync; we'd need a 2-stage UX (chart "ready in 30s, here's a
   loading screen") if no GPU.
3. **Cache key**: audio content hash (canonical, survives YouTube
   re-encodes) or videoId (fast, no audio re-fetch)? Lean toward both
   keys pointing at the same chart.
4. **Per-stem onset detection**: drums + vocals only, or all four
   stems merged? Affects compute cost in Phase 2.
5. **MERT 95M or 330M?** 95M fits in 380MB / fast inference; 330M is
   1.3GB / slower but stronger embeddings. Default to 95M unless
   section detection underperforms.

## Effort estimate (revisit at each phase)

| Phase | Effort | Latency cost (cold) | Quality jump | Risk |
|---|---|---|---|---|
| 1: Beat This! | ~2 days | +5-10s | Large (rhythm anchor) | Low |
| 2: Demucs activate | ~3 days | +15-30s (GPU) / +180s (CPU) | Medium-large | Medium (latency) |
| 3: MERT sections | ~5 days | +5-10s | Medium | Low |
| 4: Transcription | DEFERRED | +20-60s | Probably negative | High |
| 5: Lane model | DEFERRED | <1s inference, weeks training | Medium-large | High (data, training) |

## Decision log (filled as decisions land)

- _2026-05-12 — Polyphonic transcription DEFERRED based on research
  showing Basic Pitch dense-pop regressions and MT3 real-audio
  underperformance. Reopen only on specific failure case._
- _2026-05-12 — madmom DECLINED as a dep due to Python 3.10+
  maintenance status._
- _2026-05-12 — Beat This! CHOSEN as Phase 1 over librosa upgrade or
  BeatNet, based on ISMIR 2024 SOTA results and clean pip integration._
