# Backend

FastAPI service. Receives audio or a YouTube videoId, runs the analysis pipeline, returns Chart JSON.

## Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env

uvicorn app.main:app --reload
```

Swagger at http://localhost:8000/docs.

## Endpoints

- `GET /healthz` — liveness
- `POST /charts/generate` — body: `{ "videoId": "...", "difficulty": "normal" }` or multipart audio upload. Returns Chart JSON.
- `GET /charts/{contentHash}` — fetch a cached chart by content hash.

## Tests

```powershell
pytest -q
```

## Layout

```
app/
  main.py             FastAPI app, CORS, middleware, route mounting.
  config.py           Settings via env vars.
  models.py           Pydantic Chart JSON models matching shared/chart-schema.json.
  middleware.py       Request ID + access logging.
  routes/
    health.py
    charts.py
  pipeline/
    beat_track.py
    onset_detect.py
    lane_assign.py
    difficulty.py
    chart_builder.py
  audio/
    ingest.py         Hashing, persistence, optional yt-dlp.
  cache.py            SQLite chart cache.
  tools/              CLI utilities for local pipeline runs.
tests/
```
