# Architecture

A pointer to the canonical source plus diagrams that don't fit there. Treat [CLAUDE.md](../CLAUDE.md) as the source of truth for layered architecture, Chart JSON, and the timing model.

## Layer responsibilities

```
+--------------------------+         +--------------------------+
|  Chrome extension (MV3)  |         |   Backend (FastAPI)      |
|                          |         |                          |
|  popup.tsx               |         |  routes/charts.py        |
|     opens game           |         |     POST /charts/generate|
|                          |  HTTP   |                          |
|  content-script.ts       | <-----> |  pipeline/               |
|     injects overlay      |  Chart  |     beat_track           |
|                          |  JSON   |     onset_detect         |
|  overlay/canvas-renderer |         |     lane_assign          |
|     draws falling notes  |         |     difficulty           |
|                          |         |     chart_builder        |
|  game/                   |         |                          |
|     clock, input,        |         |  cache.py                |
|     hit-detection, score |         |     SQLite (content hash)|
+--------------------------+         +--------------------------+
            |                                       ^
            | tab capture audio (Option A)          |
            +---------------------------------------+
```

## Where to start

- Chart contract: `shared/chart-schema.json`. Both sides import from this.
- Extension entry: `extension/src/popup/popup.tsx`.
- Backend entry: `backend/app/main.py`.

## Timing model

See [CLAUDE.md "Timing model"](../CLAUDE.md#timing-model-this-is-the-part-that-has-to-be-right). The short version:

- Audio clock is ground truth (`video.currentTime` for tab capture, `AudioContext.currentTime` for our own playback).
- Game clock derives each frame from the audio clock plus the user's calibration offset.
- `requestAnimationFrame` timestamp is only used to decide when to redraw, never to decide when a note should be hit.

## Data flow for a single play session

1. User clicks Start Game in the popup.
2. Popup messages the background service worker, which forwards to the content script.
3. Content script reads `video.duration` and `videoId` from the YouTube page.
4. Content script `POST /charts/generate` with that metadata.
5. Backend checks the cache. Hit -> returns Chart JSON immediately. Miss -> runs the pipeline (target under 60s) and persists the chart.
6. Extension renders the HUD overlay, listens for inputs, scores them against the chart.
7. On finish, results screen shows score and accuracy. Best score persists in `chrome.storage.local`.
