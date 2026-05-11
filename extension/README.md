# Extension

Manifest V3 Chrome extension. TypeScript + React + Vite.

## Build and load

```powershell
npm install
npm run build
```

Then open `chrome://extensions`, flip developer mode on, click "Load unpacked", and pick `extension/dist`.

For active dev:

```powershell
npm run dev
```

This rebuilds on save into `dist/`. Click the refresh icon on the extension card in `chrome://extensions` after each rebuild (Chrome does not hot-reload MV3 service workers).

## Layout

```
src/
  popup/                Popup that opens on the toolbar action.
  background/           MV3 service worker. Bridges popup <-> content script.
  content/              Injected on youtube.com/watch*. Reads the video, mounts the overlay.
  overlay/              The in-page rhythm game HUD. Canvas 2D over the video.
  game/                 Clock, input, hit detection, scoring, state machine.
  api/                  Backend client.
  types/                Chart JSON TS types matching shared/chart-schema.json.
  utils/                Storage helpers, etc.
tests/
```

## Tests

```powershell
npm test
```
