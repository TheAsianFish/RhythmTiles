# How a chart is generated

A note-by-note answer to "are these notes actually synced to the song?". Short
answer: yes, deterministically. Each note's time `t` is the time of a detected
audio event, optionally snapped to a real beat. No randomness.

The pipeline supports three modes (Baseline / ML-light / ML-full) selected
entirely by env vars. The baseline shipped first and is the rollback for any
ML failure; see `docs/DEVELOPMENT.md` for install and toggle commands and
`docs/ML_PLAN.md` for the model-choice rationale.

## End-to-end algorithm

Input: an audio file (WAV from yt-dlp, or a user upload).

```
audio bytes
   |
   | soundfile.read + librosa.resample to mono 22050 Hz
   v
mono float32 PCM samples (+ sha256 content_hash for caching)
   |
   | detect_beats()
   |   - librosa.beat.beat_track             (Baseline)
   |   - or Beat This! (Audio2Beats)         (ML-light/full)
   |   downbeat sanity filter drops the list when ratio > 0.55
   |   beat cache: <CACHE_DIR>/beats/<hash>-<source>.json
   v
BeatInfo { bpm, beats, downbeats?, bpmCurve? }
   |
   | separate_stems()                        (only if USE_DEMUCS=1)
   |   demucs htdemucs -> drums/vocals/bass/other
   v
Stems { drums, vocals, bass, other, separated }
   |
   | onset detection (branch on stems.separated)
   |   - Baseline / ML-light:
   |       detect_onsets() on the full mix.
   |       energy + CQT envelopes + HPSS-harmonic, merged with
   |       centroid-aware dedupe (close-but-different-pitch survives).
   |   - ML-full:
   |       detect_onsets_per_stem() tags drums + vocals onsets,
   |       merged with a full-mix residual pass for synth/bass.
   |       per-stem onset cache: <CACHE_DIR>/onsets/<hash>-per-stem.json
   v
[ Onset(t, strength, centroid_hz, stem?) ]
   |
   | snap_onsets_to_beats()
   |   onsets within ~22ms of a beat snap to the beat exactly
   v
[ snapped onsets ]
   |
   | fill_empty_beats()
   |   2+-beat runs with no onset get synthetic events. Any empty
   |   downbeat (when available) always gets one.
   v
[ filled onsets ]
   |
   | add_subdivision_onsets()
   |   half-beat / quarter-beat candidates inside dense or crescendo
   |   sections, gated on local RMS. Feeds the thinner so Hard/Expert
   |   stay denser than Normal.
   v
[ candidate onsets ]
   |
   | assign_lanes()
   |   stem tag wins routing when present:
   |     drums/bass -> low band (lanes 0,1)
   |     vocals     -> high band (lanes 2,3)
   |   no stem -> median centroid splits low vs high.
   |   anti-cluster: no two notes within 80ms in same lane.
   |   hand-balance: streams break a 3-in-a-row same-hand streak.
   |   accent chord: top-strength + high centroid, OR within 50ms
   |     of a real downbeat -> emit one note in each band at same t.
   v
[ RawNote(t, lane, type, strength) ]
   |
   | shape_difficulty()
   |   pick keep ratio that lands the chart in TARGET_NOTES_PER_SEC
   |   for the difficulty given candidate count and song duration.
   |   chord groups always survive (score=infinity).
   |   energy bucket multiplier (verse=0.90, chorus=1.15) biases keep.
   |   sanity cap: max 8 notes in any 1s window after selection.
   v
[ shaped notes ]
   |
   | detect_holds()
   |   walk RMS from each tap; sustains above 75% of onset peak.
   |   sort by sustain duration; promote top MAX_HOLD_RATIO.
   |   snap hold end to the nearest half-beat in the chart's grid.
   v
[ notes with holds ]
   |
   | wrap in Chart JSON (AudioMeta + ChartMeta + notes)
   v
Chart JSON returned to the extension
```

Every `t` in the final chart comes from a real audio event (onset detection or
beat-fill synthetic onset on a real beat). Lane assignment is deterministic
given the same inputs. The chart cache uses the audio's sha256 (first 16 hex
chars) as part of its key so re-generating the same audio at different
difficulties is fast.

## What "synced" means here

- A note appears at the time an onset was detected in the source audio, then
  snapped to the nearest beat if within ~22ms.
- Onset detection finds energy spikes in the spectrogram: drum hits, vocal
  attacks, synth stabs. Per-stem mode runs detection on isolated drum and
  vocal stems so the onset's source instrument is known, not guessed.
- Lane assignment routes by stem (drums + bass to the low band, vocals to the
  high band) when stems are available; otherwise it falls back to spectral
  centroid as the band-split heuristic. Within each band, alternation +
  hand-balance produces playable chains.
- The BPM does NOT drive note placement directly. Notes follow the onset
  stream, snapped to the beat where possible. Empty stretches of the beat
  grid get synthetic fillers so vocal-only choruses don't go dead.

## What changes per mode

| Stage | Baseline | ML-light | ML-full |
|---|---|---|---|
| Beat tracking | librosa | Beat This! | Beat This! |
| Downbeats | none | yes (if plausible) | yes (if plausible) |
| bpmCurve | derived | derived | derived |
| Onset source | full mix | full mix | per stem + residual |
| Lane routing | centroid | centroid | stem tag (centroid fallback) |
| Chord accents | strength + centroid | strength + centroid OR downbeat | strength + centroid OR downbeat |
| Cold-start cost | ~10s | ~15-20s | 2-5 min first song, cached after |

All ML failures (package missing, inference error, OOD downbeat ratio, etc.)
fall back to the baseline silently. The heuristic pipeline is always the
rollback.

## How to verify it yourself

Sync verification on a real song:

```powershell
cd backend
.venv\Scripts\Activate.ps1
$env:BACKEND_ALLOW_YTDLP="1"
python -m app.tools.verify_sync --video-id dQw4w9WgXcQ --out .\verify_out
```

That produces:

- `verify_out/mix.wav` original song at 30% volume with a click at every
  note time. Listen to it. If clicks land on perceived musical events, the
  chart is synced.
- `verify_out/clicks_only.wav` the click pattern alone.
- `verify_out/beats_only.wav` the beat-tracker output as clicks.
- A JSON report on stdout with bpm, content hash, first 20 note times,
  lane distribution.

Bench the ML phases against the heuristic baseline:

```powershell
python -m app.tools.bench_ml_phase --wav path\to\song.wav --beat-this
```

Prints per-mode timings, note counts, chord rate, downbeat coverage, and
PASS/FAIL against the ML_PLAN gates. `--baseline-only` skips the ML run.

## Libraries used and their exact roles

| Library | Function | Role here |
|---|---|---|
| `soundfile` | `sf.read` | WAV decode |
| `librosa` | `librosa.resample` | sample rate normalization to 22050 Hz |
| `librosa` | `librosa.beat.beat_track` | global BPM + beat times (autocorrelation + DP). Baseline path. |
| `beat_this` | `Audio2Beats` | SOTA beat + downbeat detector (ISMIR 2024). ML-light/full path. |
| `librosa` | `librosa.onset.onset_strength` | spectral flux envelope (per frame) |
| `librosa` | `librosa.onset.onset_detect` | peak picking on the envelope -> onset frames |
| `librosa` | `librosa.feature.spectral_centroid` | per-frame spectral centroid for lane routing fallback |
| `librosa` | `librosa.effects.hpss` | harmonic/percussive separation for the harmonic onset branch |
| `librosa` | `librosa.feature.rms` | sustain energy for hold detection + section bucketing |
| `demucs` | `demucs.api.Separator` | htdemucs source separation. ML-full path only. |
| `torch` | (transitive) | inference runtime for both Beat This! and Demucs |

The heuristic pipeline is what `pyproject.toml` installs by default. ML deps
live behind `[ml]` and `[demucs]` extras.

## Where randomness could leak in (and doesn't)

- Beat tracking: deterministic given audio + library version.
- Onset detection: deterministic given audio + parameters (we use defaults).
- Demucs: deterministic given input + model weights.
- Beat This!: deterministic given input + model weights.
- Lane assignment: deterministic. Order of onsets fully determines lanes.
- Difficulty shaping: deterministic; tied to candidate strength + bucket.

A given audio file produces the same chart byte-for-byte across runs on the
same machine in the same mode. The chart cache uses the sha256 of the audio
bytes as its key. The beat cache and per-stem onset cache reuse the same hash
so re-generating at a different difficulty is fast (and skips Demucs entirely
on cache hit).
