# Backend

FastAPI service. Receives audio or a YouTube videoId, runs the analysis
pipeline (Baseline / ML-light / ML-full per env flags), returns Chart JSON.

## Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env

uvicorn app.main:app --reload
```

Swagger at http://localhost:8000/docs.

Optional installs for ML:

```powershell
pip install -e ".[ml]"      # Beat This! beat + downbeat detection
pip install -e ".[demucs]"  # Demucs source separation
pip install -e ".[ytdlp]"   # yt-dlp + bundled ffmpeg for videoId fetch
```

Toggle modes with `USE_BEAT_THIS=1` and `USE_DEMUCS=1`. Either flag falls
back to the librosa heuristic on import or inference failure, so a
misconfig never breaks chart generation. See `docs/DEVELOPMENT.md` for
the three-mode table.

## Endpoints

- `GET /healthz` liveness + active ML flags (`ml.beatThisFlag`,
  `ml.beatThisActive`, `ml.demucsFlag`). The extension's menu reads
  these to render the mode chip.
- `POST /charts/generate` body: `{ "videoId": "...", "difficulty": "normal" }`.
  Returns Chart JSON.
- `POST /charts/generate-from-audio` multipart audio upload + difficulty
  query param. Returns Chart JSON.
- `GET /charts/{contentHash}` fetch a cached chart by content hash.

## Tests

```powershell
pytest -q
```

## Layout

```
app/
  main.py             FastAPI app, CORS, middleware, route mounting.
  config.py           Settings via env vars (USE_BEAT_THIS, USE_DEMUCS, ...).
  models.py           Pydantic Chart JSON models matching shared/chart-schema.json.
  middleware.py       Request ID + access logging.
  routes/
    health.py         /healthz with ml flag snapshot.
    charts.py         /charts/* endpoints.
  pipeline/
    beat_track.py     librosa or Beat This! dispatch + BeatInfo.
    stems.py          Demucs htdemucs separation (optional).
    onset_detect.py   Full-mix + per-stem onset detection, merge.
    beat_fill.py      Empty-beat synthetic onsets, subdivisions, crescendos.
    lane_assign.py    Band routing (stem-aware + centroid fallback),
                      chord-accent rule (downbeat or strength + centroid).
    difficulty.py     Per-song calibrated keep ratio, sanity cap.
    hold_detect.py    Sustain detection + beat-grid hold snap.
    chart_builder.py  Wires the stages, plumbs caches + flags.
    warmup.py         librosa numba JIT + optional Beat This! warmup.
  ml/
    beat_this.py      Beat This! wrapper, sanity filter, thread lock.
    beat_cache.py     Disk cache for BeatInfo by audio content hash.
    onset_cache.py    Disk cache for per-stem onsets by audio content hash.
  audio/
    ingest.py         Hashing, persistence, optional yt-dlp.
  cache.py            SQLite chart cache.
  tools/
    run_pipeline.py   Smoke-run the full pipeline on a local WAV.
    verify_sync.py    Generate click-track overlay for ear-checking sync.
    bench_pipeline.py 4-minute synthetic bench against cold-start budget.
    bench_ml_phase.py A/B baseline vs ML run, prints PASS/FAIL gates.
    fake_chart.py     Emit the placeholder chart for offline testing.
tests/
```
