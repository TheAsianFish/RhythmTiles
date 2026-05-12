# Extension

Manifest V3 Chrome extension. TypeScript + React + Vite.

## Build and load

```powershell
npm install
npm run build
```

Then open `chrome://extensions`, flip developer mode on, click "Load
unpacked", and pick `extension/dist`.

The repo includes a committed `dist/` so another machine can clone and load
unpacked without installing Node. After you change extension source, run
`npm run build` and commit the updated `dist/` if you want that build tracked.

For active dev:

```powershell
npm run dev
```

This rebuilds on save into `dist/`. Click the refresh icon on the
extension card in `chrome://extensions` after each rebuild (Chrome does
not hot-reload MV3 service workers).

## Controls

- **D F J K** play notes (rebindable in the popup or the in-overlay menu).
- **P** pauses the underlying YouTube video. Resume runs a 3-2-1 countdown.
- **YouTube scrub** any video seek (rewind, fast-forward, click on the
  timeline) re-runs the 3-2-1 countdown at the new position.
- **☰ menu button** opens the in-overlay menu mid-game. Change difficulty
  or any setting, click Start to regenerate. No extension reload needed.
- **Replay button** appears on the results card. Seeks to 0, resets the
  score, runs the countdown.
- **Mode chip** (in the menu) shows which backend pipeline is active:
  `≋ Baseline`, `♫ ML-light`, `♫◓ ML-full`, with warning/error variants.

When YouTube auto-advances to the next song, the overlay stays open and
drops into the menu so the user can pick a difficulty before the new
chart generates.

## Layout

```
src/
  popup/                Popup that opens on the toolbar action.
  background/           MV3 service worker. Bridges popup <-> content script.
  content/              Injected on youtube.com/watch*. Reads the video,
                        mounts the overlay, intercepts D/F/J/K, watches
                        SPA navigation, routes new-chart requests.
  overlay/
    overlay-main.tsx    Iframe React app. Header, menu, results, mode chip.
    canvas-renderer.ts  Canvas 2D HUD (notes, hit line, score, accuracy XX.XX).
    sfx.ts              Synthesized hit click (tick + body + noise).
    overlay.css         Panel + menu + chip theming.
  game/
    clock.ts            BridgedClock interface + gameTimeMs helper.
    input.ts            Keyboard capture, bindings, injectKey.
    hit-detection.ts    Window scoring, advanceCursor, candidate lookup.
    scoring.ts          Score state, combo + multiplier, accuracy.
    loop.ts             GameLoop: tick rAF, onFrame/onFinish callbacks,
                        seek + pause + resume + startPaused option.
    state-machine.ts    High-level game phases.
    types.ts            NoteRuntime, ScoreState, HitWindowsMs, OD curve.
  api/
    backend-client.ts   /healthz + /charts/generate + HealthPing.ml info.
  types/
    chart.ts            TS types mirroring shared/chart-schema.json.
  utils/
    storage.ts          UserSettings + best-score round trips via
                        chrome.storage.local (localStorage fallback).
tests/
```

## Tests

```powershell
npm test
```
