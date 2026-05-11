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
- `USE_DEMUCS` (1 to enable source separation; off by default, slow on CPU)
- `CACHE_DIR` (where SQLite + temp audio live; defaults to `backend/cache`)

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
