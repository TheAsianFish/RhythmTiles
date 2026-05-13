# Development

Setup and day-to-day commands. Tested on Windows 11 with PowerShell.

## Prereqs

- Python 3.11+ (3.13 is what this repo was bootstrapped on)
- Node 20+ (22 is what this repo was bootstrapped on)
- ffmpeg on PATH (librosa pulls it via audioread; yt-dlp also wants it)

## Backend

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e ".[dev]"

# Run
uvicorn app.main:app --reload

# Test
pytest -q

# Lint
ruff check .
```

Env vars (see `backend/.env.example`):

- `BACKEND_HOST`, `BACKEND_PORT`
- `BACKEND_ALLOW_YTDLP` (1 to enable yt-dlp ingest path; off by default)
- `USE_DEMUCS` (1 to enable source separation; off by default, slow on CPU).
  When on, per-stem onset detection runs (drums + vocals tagged onsets).
  Requires `pip install '.[demucs]'`. First run downloads ~80MB of model
  weights; per-song generation goes from ~10s to several minutes on CPU.
- `USE_BEAT_THIS` (1 to enable CPJKU Beat This! beat + downbeat detector;
  off by default. `pip install '.[ml]'` to install the optional deps).
- `USE_MERT` (1 to enable MERT audio embeddings for section detection;
  off by default. Drives per-section density and populates
  `Chart.sections`. `pip install '.[mert]'` to install.)
- `CACHE_DIR` (where SQLite + temp audio live; defaults to `backend/cache`).
  Beat-tracker output is cached under `<CACHE_DIR>/beats/`. Per-stem onset
  lists are cached under `<CACHE_DIR>/onsets/`. MERT section labels under
  `<CACHE_DIR>/sections/`. All three caches share the audio's content-hash
  key so re-generating the same song at a different difficulty doesn't
  re-pay any ML cost.

Production-hardening env vars (introduced 2026-05-12 audit pass):

- `MAX_CONCURRENT_CHARTS` (default `1`). How many chart-generate
  requests can run the heavy pipeline simultaneously. Default 1 is
  safe for any deploy because Demucs holds the torch lock; bump to
  2-4 only on GPU boxes with confirmed memory headroom.
- `CHART_QUEUE_TIMEOUT_S` (default `0`). How long a queued
  chart-generate request waits before the route returns HTTP 429.
  `0` means fail-fast - clients get a clear "busy" immediately
  instead of hanging behind a multi-minute Demucs job. Raise to
  30-60s on GPU deploys where the queue cycles fast.
- `CACHE_MAX_AGE_DAYS` (default `30`). Files under `<CACHE_DIR>/
  {audio,beats,onsets,sections}/` and SQLite chart rows older than
  this are pruned on backend startup. Drop to 7 if disk is tight;
  raise if cache hits are valuable longer.

## Four modes

The pipeline supports four modes, toggled entirely by env vars. The
default (everything off) is the librosa heuristic that shipped before
any ML landed. All flag combinations gracefully fall back if their
package isn't installed, so a misconfig never breaks chart generation.

| Mode | Flags | Install | First-song cost (CPU) |
|---|---|---|---|
| Baseline | (all off) | `pip install -e ".[dev]"` | ~10s |
| ML-light | `USE_BEAT_THIS=1` | `pip install -e ".[ml]"` | ~15-20s |
| ML-full | `USE_BEAT_THIS=1` + `USE_DEMUCS=1` | `pip install -e ".[ml,demucs]"` | 2-5 min |
| ML-max | + `USE_MERT=1` | `pip install -e ".[ml,demucs,mert]"` | 3-7 min (first song only) |

After the first chart per song, the chart cache returns it instantly
on replay. Per-stem onsets, Beat This! beats, AND MERT section labels
are cached separately by audio content hash, so re-generating at a
different difficulty doesn't re-pay the ML cost. MERT runs once per
song; the section labels apply to every difficulty.

## Extension

```powershell
cd extension
npm install
npm run build         # production bundle into extension/dist
npm run dev           # rebuild on change
npm test              # vitest
```

Load `extension/dist` as an unpacked extension at `chrome://extensions` with developer mode on.

The popup connects to `http://localhost:8000` by default. Override with `VITE_BACKEND_URL` in `extension/.env.local`.

## Useful one-offs

Generate a synthetic test chart locally:

```powershell
cd backend
python -m app.tools.fake_chart > sample_chart.json
```

Run a smoke test through the full pipeline on a local WAV:

```powershell
cd backend
python -m app.tools.run_pipeline path\to\song.wav
```
