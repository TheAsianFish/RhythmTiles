# Architecture

A pointer to the canonical source plus diagrams that don't fit there. Treat [CLAUDE.md](../CLAUDE.md) as the source of truth for layered architecture, Chart JSON, and the timing model.

## Layer responsibilities

```
+----------------------------+         +-------------------------------+
|   Chrome extension (MV3)   |         |   Backend (FastAPI)           |
|                            |         |                               |
|  popup.tsx                 |         |  routes/health.py             |
|    Start Game, settings    |         |     GET /healthz (ml flags)   |
|                            |         |  routes/charts.py             |
|  content-script.ts         |  HTTP   |     POST /charts/generate     |
|    injects overlay         | <-----> |                               |
|    bridges video clock     |  Chart  |  pipeline/                    |
|    SPA-nav watcher         |  JSON   |     beat_track (dispatches)   |
|                            |         |     stems (optional Demucs)   |
|  overlay/canvas-renderer   |         |     onset_detect (full + stem)|
|    draws falling notes     |         |     beat_fill, lane_assign    |
|                            |         |     difficulty, hold_detect   |
|  overlay/MenuPanel         |         |     chart_builder             |
|    in-game settings + mode |         |                               |
|    chip (≋ ♫ ♫◓ ⚠)         |         |  ml/                          |
|                            |         |     beat_this (Phase 1)       |
|  game/                     |         |     beat_cache, onset_cache   |
|    clock, input,           |         |                               |
|    hit-detection, score,   |         |  cache.py                     |
|    state machine, loop     |         |     SQLite Chart cache        |
+----------------------------+         +-------------------------------+
            |                                       ^
            | tab capture audio (Option A)          |
            +---------------------------------------+
```

The ML modules under `app/ml/` are optional. They activate only when their
env flags are set AND their packages are importable; both Phase 1 (Beat
This!) and Phase 2 (Demucs) silently fall back to the librosa heuristic on
any failure. See `docs/ML_PLAN.md` and `docs/PIPELINE.md` for what each
stage produces in each mode.

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

1. User clicks Start Game in the popup (or the Start button in the
   in-overlay menu after switching songs or changing difficulty).
2. Popup messages the background service worker, which forwards to the content script.
3. Content script reads `video.duration` and `videoId` from the YouTube page.
4. Content script `POST /charts/generate` with that metadata.
5. Backend checks the cache. Hit -> returns Chart JSON immediately.
   Miss -> runs the pipeline (Baseline / ML-light / ML-full per env
   flags; per-stem onsets and beats cached separately by audio hash so
   repeat difficulties skip the expensive stages). Target cold-start
   60s baseline / 90s with ML; warm replays from chart cache are
   sub-second.
6. Extension renders the HUD overlay, listens for inputs, scores them against the chart.
7. On finish, the results screen shows score and accuracy. Best score
   persists in `chrome.storage.local`. Replay seeks the video to 0,
   resets score, and runs the 3-2-1 countdown.

## Overlay control surfaces

- **Header buttons:** drag handle, difficulty chip, Replay (results
  only), `☰` menu, Close.
- **In-overlay menu:** pause the game, change difficulty and other
  settings without reloading the extension, see which backend mode is
  active via a symbolic chip (`≋ Baseline`, `♫ ML-light`, `♫◓ ML-full`,
  plus warning/error states).
- **Keyboard:** D/F/J/K play notes (rebindable in popup or menu);
  `P` pauses the underlying video (with 3-2-1 on resume); any video
  seek triggers a countdown.
- **SPA navigation:** moving to the next YouTube video keeps the
  overlay open and drops into the menu so the user can pick a
  difficulty before generating the new chart.
