# Backend modes (a.k.a. "which ML model is running")

Quick-reference list of the modes you can boot the backend in. Toggle by
env var; all flags fall back to the librosa heuristic on package or
inference failure so a misconfig never breaks chart generation.

See `docs/PIPELINE.md` for what each stage actually does in each mode,
`docs/DEVELOPMENT.md` for install commands, and `docs/ML_PLAN.md` for
why each ML phase was chosen.

## The four modes

| # | Name | Flags | First-song cost (CPU) | What it adds |
|---|---|---|---|---|
| 1 | **Baseline** (v1) | none | ~10s | The original heuristic pipeline that shipped before any ML landed: librosa beats, full-mix onsets, centroid-based lane routing, hand-tuned chord emission. |
| 2 | **ML-light** (v2) | `USE_BEAT_THIS=1` | ~20s | Phase 1: replaces librosa beat tracker with Beat This! (CPJKU, ISMIR 2024 SOTA). Adds downbeat detection so chord stacks fire on real bar starts. Adds a real `bpmCurve` to the Chart JSON. |
| 3 | **ML-full** (v3) | `USE_BEAT_THIS=1` + `USE_DEMUCS=1` | 2-5 min | Phase 2: adds Demucs source separation (drums / vocals / bass / other). Onset detection runs per stem for cleaner timing. Note: as of 2026-05-12 stem tags do NOT decide lane placement (the earlier "left-hand-is-drums, right-hand-is-vocals" rule was reverted after playtesting). |
| 4 | **ML-max** (v4) | `+ USE_MERT=1` | 3-7 min | Phase 3: adds MERT audio embeddings for section detection. Density modulates per section (verses sparse, choruses dense). `Chart.sections` populated with labeled section records. |

## How costs scale

Each "first-song cost" is the wall-clock time the player waits on a
brand-new song with no cache. After the first chart for a song:

- **Same song, same difficulty**: chart cache hit, near-instant.
- **Same song, different difficulty**: all ML caches hit (beats /
  per-stem onsets / sections by audio content hash), only the
  difficulty-dependent stages re-run. Sub-second.
- **Different song**: pay the full mode cost again.

## Where the cost actually lives

| Stage | Baseline | ML-light | ML-full | ML-max |
|---|---|---|---|---|
| yt-dlp fetch | 5-15s | 5-15s | 5-15s | 5-15s |
| Beat tracking | 1s | ~3s | ~3s | ~3s |
| Stem separation | — | — | **2-5 min** | **2-5 min** |
| Onset detection | 2s | 2s | 3-5s | 3-5s |
| Section detection | — | — | — | ~5s |
| Lane assign + thin | <1s | <1s | <1s | <1s |

Demucs is the dominant cost in modes 3 and 4. On a GPU it drops to
~15s; on a CPU-only box it's the part players actually wait for.

## How to start each mode

In PowerShell, from `backend/`:

```powershell
# 1. Baseline
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 2. ML-light
$env:USE_BEAT_THIS = "1"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3. ML-full
$env:USE_BEAT_THIS = "1"
$env:USE_DEMUCS = "1"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4. ML-max
$env:USE_BEAT_THIS = "1"
$env:USE_DEMUCS = "1"
$env:USE_MERT = "1"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

All four also need `$env:BACKEND_ALLOW_YTDLP = "1"` for the
YouTube-fetch path; otherwise the backend returns a 20-note
placeholder chart.

## Watch out: ML-full was once silently degrading

Between 2026-05-12 and 2026-05-13 there was a quiet bug where ML-full
produced charts identical to ML-light no matter the song. Root cause:
`from demucs.api import Separator` always raised ImportError because
`demucs.api` is a planned 4.1 submodule that never reached PyPI; the
graceful-fallback path in `stems.py` swallowed the import and ran
the un-separated mix. Mode chip still showed `♫◓ ML-full` because the
env flags were set.

Fixed in commit `d96e1c4` by switching to the lower-level
`demucs.pretrained.get_model` + `demucs.apply.apply_model` API that
ships with `demucs 4.0.1`. If you see an old commit and wonder why
ML-full and ML-light produce the same output, this is why.

Combined with the chart-cache cross-pollination bug (also fixed
that day in `6870484`), it was effectively impossible to A/B modes on
real songs for the brief window the two bugs coexisted: even when
Demucs ran, the cache served back a stale-mode chart.

## Mode chip in the overlay

The overlay menu shows which mode the backend is in. Symbols:

| Chip | Mode |
|---|---|
| `≋ Baseline` | (1) baseline |
| `♫ ML-light` | (2) Beat This! only |
| `♫✦ ML-light + sections` | Beat This! + MERT, no Demucs |
| `◓ Demucs only` | Demucs without Beat This! (rare combo) |
| `♫◓ ML-full` | (3) Beat This! + Demucs |
| `♫◓✦ ML-max` | (4) all three |
| `⚠ ML flag, inactive` | Flag set but package not importable; silent fallback to librosa |
| `⌛ Restart needed` | Backend reachable but pre-mode-indicator build |
| `✕ Down` | Backend unreachable |
| `… Checking` | First ping in flight |

## Deferred / out-of-scope

| Phase | Why deferred |
|---|---|
| Phase 4: polyphonic transcription (MT3 / Basic Pitch) | Research showed real-pop-audio regressions; reframes the game as a piano-MIDI sim. Deferred indefinitely. |
| Phase 5: learned lane assignment (osu!mania-trained classifier) | 3-6 week project requiring data collection + training infra. Would qualitatively change feel across all modes since lane assignment is shared logic. Defer until heuristics provably fail. |
