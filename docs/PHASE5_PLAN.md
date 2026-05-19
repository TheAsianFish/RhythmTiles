# Phase 5 plan: learned lane assignment from osu!mania charts

This is the actionable, fool-proof roadmap for ML_PLAN.md's deferred
Phase 5. It supersedes the brief sketch in ML_PLAN.md sections labeled
"Phase 5 (deferred)." Once Phase 5 lands, ML_PLAN.md's Phase 5 section
should be replaced with a pointer to this doc.

The goal is to make BeatBridge charts feel more human-mapped by
replacing the rule-based lane assignment (centroid split + hand-balance
heuristic + stem hints) with a model trained on community-charted
osu!mania 4K ranked maps.

## Scope decision: lane assignment only, not end-to-end generation

The plan is **one trained classifier that decides which of the 4 lanes
an onset goes into**. It does NOT replace:

- Onset detection (still Beat This! + per-stem Demucs detector)
- Beat tracking (still Beat This!)
- Section detection (still MERT + RMS fallback)
- Hold detection (still RMS sustain + rate cap)
- Difficulty shaping (still candidate selection + sanity cap)
- Chord pairing (still strength-quantile + lane-capacity gate)

Why this scope and not "audio -> chart, end-to-end":

1. **Tractable.** A 4-way classifier on per-onset features trains in
   hours on CPU. An end-to-end model is a multi-month research project.
2. **Composes with what works.** Our onset detector + Beat This! beats
   are already accurate. The pipeline's weak point on real songs is
   lane assignment feeling mechanical. Target the weakness, leave the
   rest.
3. **Aligns with research consensus.** Mapperatorinator
   (audio -> osu! tokens end-to-end, 219M params, ~2500 GPU-hours)
   community-reported as "not yet ranked-quality" for mania.
   GenerationMania (2018, IIDX) framed lane assignment as
   classification and that's the lineage we're inheriting.
4. **Failure mode is graceful.** If the model is unsure, fall back to
   the rule-based assigner per-onset. End-to-end models can't do that.

Three things the plan is honest about being uncertain on, with
research notes attached:

- **License**: no clean CC license exists on osu!mania charts. The
  community operates on de-facto research-fair-use posture (same as
  Mapperatorinator, GOCT, OsuLearn, GenerationMania). We adopt the
  same posture and add explicit non-redistribution measures (see
  License + ethics section).
- **Audio source for training**: `.osu` (chart only) is fetchable
  unauthenticated from `osu.ppy.sh/osu/{id}`, but `.osz` (chart +
  audio) requires user-grant OAuth (NOT client_credentials).
  Workaround: use a community mirror (nerinyan.moe, chimu.moe,
  catboy.best) for `.osz` until a self-hosted user-grant flow lands.
- **Sequence model vs per-onset classifier**: per-onset is the v1.
  Sequence-aware (transformer over onset stream) is a v2 if the
  per-onset baseline plateaus below human-feel target.

## Architectural decision: train at the chart's own onset times

Critical insight to avoid alignment hell.

The naive flow is:

```
[1] download audio + .osu
[2] run our onset detector on audio -> our_onsets[]
[3] align our_onsets to .osu hit times by nearest-neighbour
[4] train model on (features(our_onsets[i]), nearest_osu_lane)
```

Step [3] is brittle: our detector finds onsets the human mapper
ignored, and the human mapper places notes where there is no clean
onset (held vocal tail, anticipation, syncopation). Hungarian
matching gets fragile.

The non-naive flow we use instead:

```
[1] download audio + .osu
[2] extract features AT the .osu hit times directly (no detector run)
[3] train model on (features(osu_time[i]), osu_lane[i])

# At inference:
[4] run our detector on YouTube audio -> our_onsets[]
[5] extract the SAME features at each our_onset[i] time
[6] predict lane per our_onset
```

Training and inference now share the same feature schema computed at
specified times. The model never sees a misaligned label. Inference
preserves our existing detector as the source of "when is a note."

## Phase 5.1: data collection

### Two-path API access

There are two collectors in `app.training.osu_api`:

- **`NerinyanClient` (NO-AUTH, default for in-session runs)**: uses
  nerinyan.moe for BOTH search AND .osz download. Zero OAuth required.
  Drives `collect.py --no-auth`. The chart file is extracted from the
  .osz at collect time by matching `[Metadata] BeatmapID` (canonical),
  not the display Version string (which the mirror rewrites with a
  `[4K]` prefix on convert diffs).
- **`OsuClient` (OAuth, optional)**: official osu! API v2 with
  client_credentials grant for canonical search. Useful when freshness
  vs the mirror matters. Requires `OSU_CLIENT_ID` / `OSU_CLIENT_SECRET`
  env vars; the registration is a human-only web form at
  https://osu.ppy.sh/home/account/edit#oauth so this path can't be
  bootstrapped from a CI agent.

### CRITICAL filter: drop converts

osu!standard maps are auto-converted to mania at play time. They appear
in mania search results (mode_int==3, cs==4) but the `.osz` only ships
the original standard chart, so chart-extraction-by-BeatmapID fails for
every one. Filter `convert==True` at search-flatten time. Without this
filter, ~70% of a search page goes to waste downloading 5-50MB archives
that produce zero training data.

### Throttling

- API v2 hard cap: 1200/min, but anything over 60/min is "elevated"
  per ppy. Design for 60/min sustained, with exponential backoff on
  429.
- Mirror sites: 1 req/sec sustained, respect any 429.

### Target corpus

- All Ranked + Approved 4K mania difficulties (~8-12K difficulties
  across ~3-5K beatmapsets, exact unknown until we page).
- v1 training: 1000 difficulties sampled uniformly across mappers
  (to avoid overfitting to one mapper's style). This is sufficient
  for an XGBoost / LightGBM baseline.
- v2 expansion to full corpus if v1 underperforms.

### Storage layout

```
cache/training/
  manifest.json              # list of (beatmap_id, beatmapset_id, fetched_at)
  osu/{beatmap_id}.osu       # chart files
  osz/{beatmapset_id}.osz    # original archives (deletable after audio extract)
  audio/{beatmapset_id}.mp3  # extracted audio (deletable after feature extract)
  features/{beatmap_id}.parquet  # per-onset features + lane labels
```

The training cache is `.gitignore`d. The `manifest.json` is the only
file tracked in repo (small, no copyright risk, lets a teammate
re-fetch).

### License + ethics layer

- **Manifest header**: top of `manifest.json` records the date, our
  position ("research, non-commercial, non-redistributing"), and
  links to the osu! Content Usage Permissions page.
- **No redistribution**: training data never ships. Audio bytes are
  deleted after feature extraction. Trained model weights ship; raw
  data does not.
- **No memorization audit**: spot-check the trained model on songs
  in and out of training; if a model trained on chart X reproduces
  chart X note-for-note when given X's audio (rather than producing
  a plausible lane sequence), that is memorization and we cut it
  with regularization / dropout. Per-onset classifier on
  spectrogram features is extractive enough that this is unlikely.
- **About-page credit**: extension's about page acknowledges that
  the lane model was trained on community osu!mania charts and
  credits the mapping community at large (per-mapper attribution
  is infeasible at 1000-chart scale).

## Phase 5.2: .osu parsing

Roll our own parser. Existing Python libraries
(`osu-beatmap-parser`, `python-osu-parser`, `Renondedju/Osu.py`) are
stale or API-only. ~80 lines of code, no dependency risk.

### Sections we need

- `[General]`: `AudioFilename`, `Mode` (must == 3 for mania), `AudioLeadIn`
- `[Metadata]`: `BeatmapID`, `Title`, `Artist`, `Creator` (for
  attribution; not used as features)
- `[Difficulty]`: `CircleSize` (must == 4 for 4K)
- `[TimingPoints]`: BPM + offsets (optional for v1; we don't snap)
- `[HitObjects]`: the note list

### Lane formula

`lane = clamp(floor(x * column_count / 512), 0, column_count - 1)`

For 4K: `x=64 -> lane 0`, `x=192 -> lane 1`, `x=320 -> lane 2`,
`x=448 -> lane 3`.

### Note type bitfield

- `type & 1` = tap (hit circle)
- `type & 128` = hold (mania-only)
- `type & 2` = slider (osu!standard; reject)
- `type & 8` = spinner (osu!standard; reject)
- `type & 4` = new combo marker (informational; can coexist with
  tap/hold)

### Hold note quirk

For a hold note, the line has 6 commas plus a colon-separated
`endTime:hitSample` in the last position. Split on commas for the
first 6 fields, then split the last token on `:` exactly once.

### Filtering rules

- `Mode == 3` (mania)
- `CircleSize == 4` (strict 4K)
- Exclude `Mode == 0` converts even when `CircleSize == 4`
- Drop notes with `time < 0` or `time > AudioDurationMs + 5000` (defensive)
- Sort by `time` after parsing (not guaranteed in older maps)
- Encoding: `utf-8-sig` (BOM tolerated)

### Output schema

```python
@dataclass
class OsuHitEvent:
    t_s: float        # seconds from audio t=0
    lane: int         # 0-3
    type: str         # "tap" or "hold"
    duration_s: float # 0.0 for tap, positive for hold
```

## Phase 5.3: feature engineering

For each hit event at time `t_s`, produce a feature vector with the
same schema at training time and inference time.

### Audio-side features (computed at hit time `t_s`)

| Feature | Source | Why |
|---|---|---|
| `spectral_centroid_hz` | `librosa.feature.spectral_centroid` at frame containing t_s | Existing lane assigner's primary signal. Baseline. |
| `spectral_flux` | onset strength envelope | Energy "punch" at this moment |
| `rms_full_mix` | `librosa.feature.rms` | Overall loudness |
| `rms_drums` | Demucs stem -> RMS at t_s | Drum presence (snare / kick) |
| `rms_vocals` | Demucs stem -> RMS at t_s | Vocal presence |
| `rms_bass` | Demucs stem -> RMS at t_s | Bass presence |
| `rms_other` | Demucs stem -> RMS at t_s | Synth / guitar / pad |
| `chroma_max_bin` | `librosa.feature.chroma_stft` at t_s, argmax | Pitch class (0-11) |
| `chroma_strength` | chroma max value | How tonal vs noisy |
| `mfcc_1..mfcc_5` | first 5 MFCC coeffs at t_s | Timbre proxy |
| `mert_section_bucket` | `Chart.sections` lookup at t_s | 0=low / 1=mid / 2=high intensity |
| `mert_section_label_id` | enum 0..7 | Intro/verse/chorus/bridge/outro |

### Chart-side features (require sequence context)

| Feature | Computation | Why |
|---|---|---|
| `delta_t_prev` | t_s - prev_event.t_s | Inter-onset gap |
| `delta_t_next` | next_event.t_s - t_s | Forward-looking gap |
| `local_density_500ms` | count of events in [t-0.5, t+0.5] / 1.0 | NPS over a 1s window |
| `beat_phase` | (t_s - nearest_beat) / beat_period | Where in the beat (0=on, 0.5=off) |
| `bar_phase` | (t_s - nearest_downbeat) / bar_period | Where in the bar |
| `is_on_downbeat` | abs(t_s - nearest_downbeat) < tolerance | Bar start? |
| `last_3_lanes` | one-hot encoded prev 3 lanes (12 bools) | Pattern memory |
| `same_hand_streak_prev` | how many of prev 3 used same hand | Hand-balance pressure |

### Inference-time alignment

At inference, we extract these same features at OUR detected onset
times (not at .osu times). Audio-side features are well-defined at
any timestamp. Chart-side features use the SAME running window
computed from our detected onset sequence.

### Output schema

```python
# One row per onset; saved as parquet per beatmap_id at training time.
columns = [
    *audio_features,      # ~15 cols
    *chart_features,      # ~6 scalar + 12 one-hot = 18 cols
    "lane",               # 0-3 (target at training; predicted at inference)
    "type",               # "tap" or "hold" (target for hold-prediction sub-model)
    "duration_s",         # 0.0 or positive (target for hold-duration regression)
]
```

## Phase 5.4: model architecture

### v1: gradient boost classifier

- **Library**: LightGBM (faster than XGBoost on this feature scale,
  better categorical handling).
- **Target**: `lane` (4-class, multiclass-softmax objective).
- **Loss**: cross-entropy with optional class weighting if lane
  distribution is heavily skewed (early eyeballing of osu!mania
  charts: lanes 1+2 = F+J tend to get ~55% of notes vs 45% for
  D+K, but not catastrophically imbalanced).
- **Hyperparameters**: start with defaults (num_leaves=31,
  learning_rate=0.05, n_estimators=500 with early stopping).
- **Cross-validation**: 5-fold BY BEATMAP_ID, not by row. Notes
  from the same chart cannot be in both train and val.
- **Compute**: CPU, hours not days. No GPU needed.

### v2: sequence model (only if v1 plateaus)

- **Architecture**: small Transformer encoder over windows of N=32
  onset events. Output a 4-way logit per position.
- **Compute**: still CPU-feasible for this scale (~100K-1M events
  total in the training corpus); GPU optional.
- **Triggered when**: v1 top-1 accuracy is stuck below ~55% on
  held-out songs after hyperparameter search. (Above this, lane
  assignment is partly subjective and the model has hit the
  inherent label noise ceiling.)

### What we are NOT doing (and why)

- **No end-to-end audio -> chart**: scoped out above.
- **No fine-tuning Mapperatorinator**: 219M-param Whisper-based
  model whose mania output is community-reported as not yet
  ranked-quality. Fine-tuning a model already known to be weak
  inherits its weakness.
- **No pretrained audio embeddings (CLAP, MuLan)**: our existing
  features (centroid, per-stem RMS, MFCC, MERT bucket) already
  cover the embedding signal. Adding 768-d CLAP embeddings would
  10x the model size for marginal gain on a 4-way classification.

## Phase 5.5: training methodology

### Data split

- 70% train / 15% val / 15% test, split by beatmap_id.
- Hold OUT entire mappers from val/test if the corpus is dominated
  by ~10 prolific mappers (otherwise the model just learns
  "predict like this mapper" instead of generalizing).

### Metrics

Top-line:
- **Top-1 lane accuracy** on held-out test songs.

Diagnostic (must be in target band, not optimized as loss):
- **Lane distribution KL-divergence** from osu!mania community
  charts (model should produce 25/25/25/25 +/- 5%, not collapse
  onto one lane).
- **Hand-balance compliance**: model's same-hand-streak distribution
  should match osu!mania community distribution (rare to see 4+
  same-hand in a row; verify model respects this).
- **Anti-cluster rate**: how often the model places two notes in
  the same lane within 50ms of each other (should be near 0).

### Iteration loop

1. Train on 100 charts; smoke-test the pipeline end-to-end.
2. Train on 1000 charts; first real metrics.
3. Evaluate on 5 BeatBridge test songs (the ones we already use for
   playtest). Eyeball: does it feel more human?
4. If yes, push to 5000 charts.
5. If no, root-cause the failure (data quality? feature missing?
   model capacity?) before scaling data.

### Reproducibility

- Pin all dataset, library versions in `pyproject.toml` `[training]`
  extra.
- Set deterministic seeds; log to `models/lane_v{N}/training_log.json`.
- Save the feature schema alongside the model so inference can
  validate input shape.

## Phase 5.6: integration

### Env flag

`USE_LEARNED_LANES=1` activates the model. Default off. Falls back
to the rule-based assigner on:
- Model artifact not found at `backend/models/lane_v1/`
- Inference error
- Missing feature (e.g. Demucs stems unavailable and the model
  was trained with stem features)

### Code layout

```
backend/app/training/         # offline-only, NOT imported by serving code
  __init__.py
  osu_api.py
  osu_parse.py
  features.py
  train_lane.py

backend/app/ml/
  learned_lanes.py            # inference: load model, predict_lane(features)

backend/app/pipeline/
  lane_assign.py              # adds a `learned_lane_predictor` injection point;
                              # when present, predictor decides band+lane,
                              # rule-based stays as fallback

backend/models/lane_v1/       # artifacts (gitignored; documented to fetch from S3 or Release asset)
  model.lgb
  feature_schema.json
  metadata.json               # training run details + metrics
```

### Mode chip extension

The overlay menu's mode chip currently has flags for Beat This!,
Demucs, MERT. Add a fourth: `🅛` for "learned lanes." So ML-max +
learned-lanes = `♫◓✦🅛`. `/healthz` exposes `learnedLanesActive`.

### Cache key

The mode-aware chart cache key in `routes/charts.py` already encodes
the three ML flags. Extend it to include `ln1` / `ln0`:
`<base>|v0.2-bt1-dm1-mt1-ln1`. Same auto-invalidation behavior.

### Inference cost budget

- Per-onset lane prediction: ~0.1ms for LightGBM with ~30 features.
  At 1000 onsets/song that's 0.1s. Negligible.
- Model load: ~50ms on first request, cached for process lifetime.
- Feature extraction reuses already-cached Demucs stems + MERT
  sections, so the marginal cost is just per-frame lookups.

Total: pipeline cold-start budget unaffected (the existing
heavyweight stages — Demucs, Beat This!, MERT — dominate).

## Phase 5.7: validation

### A/B against existing modes

For each of the 5 BeatBridge test songs, generate charts under:
- ML-light (Beat This! only, rule-based lanes)
- ML-max (everything, rule-based lanes)
- ML-max + learned lanes

Score on:
- Patrick's blind A/B preference (listen, play, rank)
- Lane distribution histogram
- Hand-balance distribution
- Density profile per section

### Listener test

3-5 testers play 30s clips of each chart. They rank by "feels human-
mapped vs feels auto-generated." Learned-lanes mode must win 3 of 5
on at least 3 of 5 songs to ship.

### Non-regression

- All existing pipeline tests still pass.
- The flag-off path (default) produces byte-identical charts to
  current main.
- Cold-start time on the test song must not exceed 90s (60s target
  + 50% slack, same as MERT phase).

## Risks + mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| osu! API throttles bulk collection | Medium | 60/min cap, exponential backoff, resumable manifest |
| Mirror sites disappear or rate-limit | Medium | Multiple mirrors, fall back to user-OAuth osu! direct |
| License pushback from osu!/ppy | Low | Non-redistribution posture, About-page credit, mirror others' fair-use precedent |
| Model overfits to a few mappers | Medium | Sample uniformly across mappers; hold mappers out of val/test |
| Lane distribution collapses | Medium | KL-divergence diagnostic gates ship decision; class weighting if needed |
| v1 accuracy plateaus below human-feel | Medium | v2 sequence model is the escalation; v3 = add more features |
| Audio in `.osz` is mp3 with timing offsets vs our mp3 | Low | Train at .osu times against THAT audio; inference at our onset times against OUR audio; no cross-audio alignment needed |
| Trained model memorizes training charts | Low | Per-onset classifier on local features is extractive; spot-check at eval; regularize if seen |
| Demucs feature unavailable at inference | Medium | Model trained with stem features falls back to rule-based when stems absent (USE_DEMUCS=0 path) |

## Open decisions

These are flagged for resolution before the relevant phase starts;
each has a sensible default if Patrick doesn't weigh in.

1. **Default mirror**: nerinyan.moe (most actively maintained as of
   May 2026). Fallback: chimu.moe, catboy.best.
2. **First training run corpus size**: 1000 difficulties (~30
   minutes of API time + ~2 hours of audio download). Cheap enough
   that we can iterate; large enough to be more than memorizable.
3. **Hold prediction**: v1 predicts lane only. Type (tap/hold) and
   duration come from existing detect_holds. v2 may extend to a
   multi-task head.
4. **Per-difficulty training**: train one model on ALL difficulties
   pooled, or per-difficulty (one model for Insane, one for Hard,
   etc.)? Default: pool, with difficulty as a feature. Per-
   difficulty models if pool quality is poor.
5. **Audio source**: nerinyan mirror first; user-OAuth fallback. If
   neither path lands cleanly we can also skip audio entirely for
   a chart-pattern-only model (last resort, would lose audio-aware
   lane decisions which is much of the point).

## Effort estimate

| Phase | Effort | Compute | Risk |
|---|---|---|---|
| 5.1 data collection scaffolding | 1 day | trivial | Low |
| 5.2 .osu parser | 0.5 day | trivial | Low |
| 5.3 feature extraction | 1.5 days | hours per 1000 charts (audio decode + Demucs + MERT) | Medium (caching correctness) |
| 5.4 + 5.5 train v1 | 1 day | hours on CPU | Low |
| 5.6 integration | 1 day | none | Low |
| 5.7 validation | 1-2 days (playtest gated) | minutes | Medium (gating signal is subjective) |

Total: ~7 working days for v1. v2 sequence model adds another
~7 days if needed.

## Concrete next steps (this order)

**Status as of 2026-05-18:** steps 1-5 completed in-session. v0.1 model
trained on 30 charts (no Demucs / no Beat This! / no MERT for speed)
landed at `backend/models/lane_v1/`. Pipeline integration validated
end-to-end: `USE_LEARNED_LANES=1` produces visibly different charts
from the rule-based baseline. v0.1 test accuracy 38.6% (random=25%);
lane bias toward F. The model is NOT shippable; it's proof the
end-to-end pipeline works.

**To reach a shippable v1:** Patrick or a longer agent session:

1. Re-collect with a larger corpus:
   `python -m app.training.collect --target 1000 --no-auth`
   (3-5 hours of mirror downloads at 1 req/sec).
2. Re-extract WITH heavy features:
   `python -m app.training.train_lane extract --use-demucs --use-beat-this --use-mert`
   (~2 hours per 100 songs on CPU because of Demucs; budget a full day
   for 1000 songs OR run on a GPU box).
3. Re-train: `python -m app.training.train_lane train --num-rounds 500`
   (~30 min CPU on 1000-chart corpus).
4. Validate via the methodology in section "Phase 5.7: validation":
   A/B against ML-light/ML-max on the 5 test songs, blind listener
   rank, target win-rate >= 3/5 on >= 3/5 songs.
5. If accuracy plateaus below ~55%, escalate to v2 sequence model
   (small Transformer over onset windows; same data, different
   architecture).

## Decision log (filled as decisions land)

- _2026-05-18 — Phase 5 RESCOPED as lane-only classification, not
  end-to-end generation. Rationale: tractability, composability
  with working pipeline, research-consensus alignment._
- _2026-05-18 — Training pairs (audio, .osu_times) with features
  extracted AT .osu times, not at our detector's onsets, to
  sidestep alignment failures. Inference applies the same feature
  schema at our detector's onset times._
- _2026-05-18 — License posture: research fair use, no
  redistribution. About-page credit to the mapping community._
- _2026-05-18 — No-auth mirror path (NerinyanClient) added so the
  collector runs without OAuth registration. Mirror-only matched by
  [Metadata] BeatmapID, not display Version string. Convert filter
  (convert==True) added to drop osu!standard auto-converts that have
  no real mania chart in the .osz._
- _2026-05-18 — v0.1 model trained end-to-end on 30 charts,
  librosa-only features (no Demucs/Beat-This/MERT). Test accuracy
  38.6% vs 25% random. Underperforms target by design — proof of
  pipeline, not a shippable model. Scale-up plan in "Concrete next
  steps."_

## References

- [osu! API v2 docs](https://osu.ppy.sh/docs/)
- [.osu file format wiki](https://osu.ppy.sh/wiki/en/Client/File_formats/osu_(file_format))
- [.osz file format wiki](https://osu.ppy.sh/wiki/en/Client/File_formats/osz_(file_format))
- [osu! Content Usage Permissions](https://osu.ppy.sh/wiki/en/Rules/Content_usage_permissions)
- [GOCT: Beat-Aligned Spectrogram-to-Sequence (ISMIR 2023)](https://arxiv.org/abs/2311.13687)
- [Mania Archetype eval methodology (2024)](https://openaccess-api.cms-conferences.org/articles/download/978-1-964867-13-7_19)
- [GenerationMania (arxiv 1806.11170)](https://arxiv.org/pdf/1806.11170)
- [Mapperatorinator (OliBomby)](https://github.com/OliBomby/Mapperatorinator)
- [BeatLearning](https://github.com/sedthh/BeatLearning)
- [nerinyan mirror API](https://nerinyan.moe/docs)
