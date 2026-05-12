# PROGRESS

A running log of milestones, blockers, and open tasks. Newest entries on top.

## Legend

- [x] done
- [~] partial
- [ ] not started
- [!] blocked

## Open blockers

None. The extension is functional end-to-end on real YouTube audio. The HUD
is windowed (360px wide draggable panel) with a translucent gradient. Scoring
is osu!-style. Timing is OD-tunable.

The most common operational issue is a stale extension load: Chrome doesn't
hot-reload content scripts. After every backend or extension code change,
reload at `chrome://extensions` AND reload the YouTube tab.

## Open TODOs (rolling)

- [ ] Chord notes: lane assigner splits high-strength onsets into 2 notes.
      Data model + game loop already support it (see DECISIONS.md entry).
- [ ] Tab capture path (Option A): currently the backend uses yt-dlp behind a
      flag. For production we want extension-side tab capture so we never
      have to redownload audio.
- [ ] Manual playtest the calibration flow on bluetooth vs wired headphones.
- [ ] Settings panel for keybindings + note speed; right now only OD is
      surfaced in the popup.
- [ ] Telemetry shape for Stage 6 (Plausible or Umami; anonymous only).

## Milestones

### 2026-05-11 (session 1)

- [x] Read CLAUDE.md and PLAN.md, captured working agreements.
- [x] Scaffolded repo: backend/, extension/, shared/, docs/.
- [x] Stage 1.1: FastAPI skeleton (health, /charts/generate, request-id middleware).
- [x] Stage 1.2: Extension MV3 skeleton (popup, content script, overlay iframe, service worker).
- [x] Stage 1.3: Chart JSON contract (cross-validation test against shared/chart-schema.json).
- [x] Stage 2.1: Audio ingest (content-hash cache, yt-dlp behind a flag).
- [x] Stage 2.2: Analysis pipeline (librosa beat + onset + lane + difficulty).
- [x] Stage 2.3: Pipeline tests on synthetic click track.
- [x] Stage 3.1-3.3: Input capture, hit detection, scoring (initial 4-tier system).
- [x] Stage 3.4-3.5: Canvas HUD, state machine.
- [x] Stage 4: Calibration page.

### 2026-05-11 (session 2)

Subsequent iteration after first playtest. User reported HUD was fullscreen,
scoring needed osu! rules, timing was too lenient, and asked for proof that
notes are actually synced to audio.

- [x] **Real chart generation working**: yt-dlp + imageio-ffmpeg installed,
      backend run with `BACKEND_ALLOW_YTDLP=1`. Verified on Rick Astley:
      547 (initial) -> 689 (after pipeline fixes) real notes detected at
      112.3 BPM. Pipeline cold-start ~3-30s, warm ~700ms, cached ~5ms.
- [x] **Lane balance fix**: median-centroid split replaced fixed 1500Hz
      cutoff. Before: lanes [0, 2, 3] only. After: 176/183/156/174 (balanced
      across all four lanes).
- [x] **Difficulty thinning fix**: stride-keep replaced with gap-respecting
      decimation + lane-variety tiebreaker. The old stride collapsed
      alternating lanes into single-lane chains.
- [x] **HUD windowed**: iframe is now a 360px-wide panel positioned on the
      right of the YouTube viewport with translucent gradient and a draggable
      header. NOT full-screen.
- [x] **osu!-style scoring**: each non-miss hit awards `base * combo` points.
      Combo increments per non-miss; miss resets to 0.
- [x] **Six-tier judgment system**: max/great/good/ok/meh/miss with OD-based
      hit windows. Default OD=8. User-tunable in the popup with live window
      preview.
- [x] **Music sync verification**: `python -m app.tools.verify_sync` produces
      `mix.wav` (original at 30% + clicks at every note time), `clicks_only.wav`,
      `beats_only.wav`, and a JSON report. Playing mix.wav and hearing the
      clicks land on musical events is the proof.
- [x] **Algorithm doc**: docs/PIPELINE.md walks through every step from
      audio bytes to Chart JSON, listing the exact librosa functions used.
- [x] Backend test isolation fix: conftest now reloads cache + routes after
      monkeypatching CACHE_DIR.

### Not started

- [ ] Stage 5: Polish (settings panel, score-history view, replay improvements).
- [ ] Stage 6: Shipping (deploy backend, Chrome Web Store submission).

## Test counts

- Backend: 24 pytest passing.
- Extension: 34 vitest passing.

## Stage status

| Stage | Status | Exit criterion | Notes |
|-------|--------|----------------|-------|
| 0 | [x] | Architecture decisions logged | docs/DECISIONS.md, docs/PIPELINE.md |
| 1 | [x] | Notes fall on screen over a YouTube video | Works |
| 2 | [x] | Backend returns chart in <60s that follows the music | Verified on real YouTube audio with mix.wav |
| 3 | [x] | Full song playable end-to-end | osu! scoring + 6-tier judgments + draggable HUD |
| 4 | [~] | Friend on their own machine reports it feels tight | Calibration page implemented; in-the-wild test pending |
| 5 | [ ] | Hand it to a stranger, they play through without help | Settings panel partial (only OD surfaced) |
| 6 | [ ] | Backend deployed, extension submitted | Not started |

## How to verify chart sync (for anyone skeptical)

```powershell
cd backend
.venv\Scripts\Activate.ps1
$env:BACKEND_ALLOW_YTDLP="1"
python -m app.tools.verify_sync --video-id <id> --out .\verify_out
```

Play `verify_out/mix.wav`. Each click should land on a perceived musical
event. If they do, the chart is synced to the audio.
