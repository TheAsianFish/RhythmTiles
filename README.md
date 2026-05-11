# BeatBridge

A Chrome extension that turns any YouTube video into a 4-lane rhythm game (think osu!mania or Piano Tiles). Notes are auto-generated from the playing audio.

Working name. May rename before shipping.

## Status

Stages 0 through 4 are in place. End-to-end placeholder chart works; real librosa pipeline runs on synthetic audio. See [PROGRESS.md](PROGRESS.md) for what is done vs pending and [PLAN.md](PLAN.md) for the full roadmap.

Test counts at the latest commit:
- Backend: 24 pytest passing.
- Extension: 27 vitest passing.

## Repo layout

```
RhythmTiles/
  backend/        FastAPI service. Audio analysis pipeline. Returns Chart JSON.
  extension/      Manifest V3 Chrome extension. TS + React + Vite.
  shared/         Source-of-truth schemas shared across backend and extension.
  docs/           Architecture and decision logs.
  CLAUDE.md       Project context and working agreements.
  PLAN.md         Staged build plan with exit criteria.
  PROGRESS.md     Milestone log.
```

## Local dev

### Backend

```
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows PowerShell
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The API runs on `http://localhost:8000`. Swagger UI at `/docs`.

### Extension

```
cd extension
npm install
npm run build
```

Then load `extension/dist` as an unpacked extension in `chrome://extensions` with developer mode on.

## Architecture

See [CLAUDE.md](CLAUDE.md) for the layered architecture and the Chart JSON contract. Short version:

```
Chrome extension <-> Backend <-> Analysis pipeline
       |                              |
       v                              v
  HUD overlay                  beat + onset + lane
  Input + scoring              difficulty shaping
```

## License

TBD before public release.
