# Phase 5 follow-ups and architecture next steps

Companion to `docs/PHASE5_PLAN.md`. That doc is the source of truth for
the overall plan; this one records what we've learned post-v0.3 and
sketches the credible next moves.

## Status snapshot (2026-05-20)

**Shipped:**
- v0.1, v0.2, v0.3 learned-lane LightGBM models (33 features in v0.3, 26 in v0.1/v0.2)
- Hand-balance + lane-distribution postprocessor at inference (fights F/J bias)
- Time-windowed difficulty thinner (`SELECT_WINDOW_S=2.0`, no shortfall backfill)
- `PIPELINE_VERSION` 0.11.0 (invalidates pre-windowed-thinner cache rows)
- 117 backend tests passing including 12 new windowed-thinner tests

**Working but with known limits:**
- Test accuracy plateau at ~43% on LightGBM across v0.1/v0.2/v0.3. New
  flow features did not move the needle. Train/val/test are clustered
  tightly — underfitting, not overfitting. Architectural ceiling.
- Subjective playtest: v0.3 + postprocessor feels "better, not bad."
  Still some sync / timing room to improve. F/J bias largely fought
  by postprocessor.
- Genre-distribution mismatch is real: training corpus skews
  Touhou / anime / hardcore. Western pop charts feel less natural
  than electronic ones.

## What just landed: time-windowed thinner

### What changed in `app/pipeline/difficulty.py`

`shape_difficulty` previously did global top-K by score. A song with
one bright chorus and a quiet verse would keep all its allotment in
the chorus and leave verses empty. The new thinner:

1. Bucket notes into 2-second windows (`SELECT_WINDOW_S`)
2. In each window, keep the top-K notes by score, where `K = target_NPS_mid * SELECT_WINDOW_S`
3. Chord pairs are still promoted to inf-score by the caller, so they survive whole
4. No shortfall backfill. Sparse music → sparse chart. Honest.

### What we explicitly did NOT do

- **No global "reach target NPS at all costs" backfill.** That would
  re-introduce the bug we just fixed (over-filling dense regions to
  compensate for empty ones).
- **No note re-timing.** Notes stay at the times the onset detector
  put them; thinner only drops, never moves.
- **No subdivision changes.** `add_subdivision_onsets` still runs
  upstream per-difficulty (Hard adds half-beats, Expert adds quarters).
  Per-difficulty differentiation now happens at both the subdivision
  stage AND the windowed thinner stage.

### Validation

`backend/tests/test_windowed_thinner.py` (12 tests):

- Notes stay at original times (no re-timing)
- Chord partners preserved through inf-score promotion
- Spreading across windows: dense burst + uniform background → both survive
- Per-window cap is a hard ceiling (no chorus blow-out)
- Difficulty bands produce dramatically different totals
- Empty input → empty output
- Output is time-sorted regardless of input order
- Total NPS lands in or below the difficulty's band on uniform input
- Windowed distribution has strictly lower per-window standard deviation
  than simulated global top-K on uneven input
- Sparse song → sparse chart (no fake density)
- End-to-end shape_difficulty preserves chord pairs

## What we believe is wrong with the v0.3 LightGBM model

The metrics:

| | Train | Val | Test |
|---|---|---|---|
| v0.1 (30 charts, 26 features) | n/a | n/a | 38.6% |
| v0.2 (1000 charts, 26 features) | 42.0% | 46.0% | 43.2% |
| v0.3 (1000 charts, 33 features) | 46.6% | 42.8% | 43.2% |

Test accuracy doesn't budge despite 33x data and richer features. Train
moves up modestly (42→47%) but doesn't generalize. This is the
**architecture ceiling**: LightGBM gradient-boosted trees on per-event
features lack the inductive bias to model sequence-level patterns
("the lane I picked 3 events ago should influence this lane").

**More data won't help.** More features won't help. The model needs to
see SEQUENCES, not isolated events.

## Architecture next steps

In order of effort vs likely lift:

### Tier 1: easy wins (hours to a day)

**(A) Tune the postprocessor more aggressively.**
- `_DISTRIBUTION_PENALTY_SCALE` from 1.5 to 2.5-3.0
- `_MAX_SAME_HAND_STREAK` from 2 to 1 (forbid any 2-in-a-row same hand)
- Add a "force-K-once-every-N" rule: if K hasn't been picked in the
  last 8 events, force the next non-J non-F event to be K
- File: `app/ml/learned_lanes.py`. Cost: ~30 min code + immediate playtest.

**(B) Style-filter the training corpus.**
- Re-collect with `--max-star-rating 3.5 --min-star-rating 1.5`
- Trains on the "casual mania" subset that's closer to Fortnite Festival
- Cost: ~7 hr overnight (re-extract on filtered set + retrain). No
  code change beyond a CLI flag.

**(C) Difficulty band sanity tuning.**
- Currently Easy targets ~1 NPS (lo=0.74). May be too low; bump to
  ~1.5 NPS for less empty feel even on sparse songs.
- File: `app/pipeline/difficulty.py:TARGET_NOTES_PER_SEC`. Cost: minutes
  + playtest.

### Tier 2: real architectural lift (~1 week)

**(D) v2 sequence Transformer.**
- Small (~3M param) Transformer encoder over windows of N=32 onset events
- Output: per-position 4-way lane logits
- Same features, just consumed in sequence. Captures "I just played
  three streams on the left hand, time for a right-hand break."
- Likely test accuracy ceiling: 55-65% based on published rhythm-game
  chart-prediction papers (GOCT, ManiaArche).
- Cost: ~3-5 days code + overnight retrain. Heaviest single move on
  the table.
- File layout: new `backend/app/training/train_lane_v2.py`, new
  inference path `backend/app/ml/learned_lanes_v2.py`. Behind a
  `LEARNED_LANES_ARCH=v2` env var so v1 stays as fallback during
  validation.

**(E) Multi-task head: lane + chord + hold prediction.**
- Current v0.3 predicts only lane. Chord pairs, hold conversions all
  still rule-based downstream. A multi-task head shares representations
  and lets the model learn "this onset feels like a held vocal sustain,
  not a tap."
- Best stacked on (D) — single-task v1 already underfits, multi-task
  on v1 architecture would underfit harder.

### Tier 3: diversify training data (~1-2 weeks)

**(F) StepMania / BMS / IIDX charts.**
- osu!mania alone is genre-biased. StepMania has more Western pop +
  K-pop coverage; BMS / beatmania IIDX has different mapping conventions
  that may transfer better to "vocal-led" songs.
- Each format needs its own parser + chart-to-event normalization.
- Cost: ~1-2 weeks. Substantial.

**(G) Synthetic augmentation.**
- Pitch-shift and tempo-shift training audio by ±2 semitones / ±5% to
  teach genre invariance. Cheap if features cache by augmented-audio
  hash (so we recompute features but not download).
- Cost: ~2 days.

### Tier 4: Fortnite Festival inspiration in spirit (no implementation)

We can't access Fortnite Festival chart data — Harmonix doesn't publish
it. But we can borrow its design philosophy:

- **Melody-first lane assignment**: vocal-stem-loudest events bias toward
  lane 1 or 2 (inner). Drum-stem-loudest bias toward 0 or 3 (outer).
  Our v0.3 features have `dominant_stem_*` one-hots — if the model isn't
  learning this routing organically, we can add a HARD post-processing
  rule that overrides model output when `dominant_stem_vocals` is strongly
  set and the model picked an outer lane.
- **Difficulty-by-spam, not by speed**: FF makes Expert harder by adding
  CHORDS and HOLDS, not by raw NPS. Our `_DIFFICULTY_TUNING` chord_quantile
  + hold_ratio already does this; could push harder (Expert at
  chord_quantile=0.75, hold_ratio=0.15).
- **Beat-aligned snapping over rhythmic precision**: FF prioritizes
  notes landing on "music feel" beats over exact onset times. Our
  `snap_onsets_to_beats` does this within ±22ms. Could widen tolerance
  to ±50ms for "feels good" snap.

These three would land in `app/pipeline/lane_assign.py` (rule overrides),
`app/pipeline/chart_builder.py` (`_DIFFICULTY_TUNING`), and
`app/pipeline/onset_detect.py` (`snap_onsets_to_beats`) respectively.
All small, all reversible, all playable in an afternoon.

## Recommended order

For the next session or two:

1. **Playtest the windowed thinner** on the same songs that felt
   "spammy" or "empty" before. Confirm the fix.
2. **If still spammy**: Tier 1 (A) — bump the postprocessor scale.
3. **If feel improves but model still chases wrong lanes**: Tier 4
   (Fortnite spirit) — add the dominant-stem hard-override rule.
4. **If problem is truly in the model's intelligence**: Tier 2 (D)
   v2 Transformer. ~1 week of focused work.

Do NOT do Tier 3 (diversify data) until Tier 2 lands. Diverse data on
an underfit model gives the same 43% test accuracy.

## Decision log

- _2026-05-20 — Time-windowed thinner shipped at PIPELINE_VERSION 0.11.0.
  Replaces global top-K. Shortfall backfill removed (would have violated
  per-window cap that's the whole point)._
- _2026-05-20 — Postprocessor (hand-balance + lane-distribution correction)
  active in v0.3 inference. Was not active when v0.2 was playtested, so
  v0.3 vs v0.2 subjective comparison includes this delta even though
  raw test accuracy is identical._
- _2026-05-20 — Identified LightGBM architecture ceiling at ~43% test
  accuracy. Further iteration on the LightGBM model is not worthwhile;
  v2 Transformer is the credible next jump if Tier 1 + Tier 4 fail to
  reach acceptable feel._
