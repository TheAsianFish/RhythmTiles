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

- [ ] Tab capture path (Option A): currently the backend uses yt-dlp behind a
      flag. For production we want extension-side tab capture so we never
      have to redownload audio.
- [ ] Manual playtest the calibration flow on bluetooth vs wired headphones.
- [ ] Telemetry shape for Stage 6 (Plausible or Umami; anonymous only).
- [ ] Mirror-pair lane colors (osu!mania 4K convention): outer lanes one
      color, inner lanes another, so D/K match and F/J match. Helps hand
      parsing during streams. (Research note from session 5.)
- [ ] Per-section density curve (chorus denser than verse): detect with
      spectral-flux envelope before lane assignment.
- [ ] Demucs source separation behind USE_DEMUCS=1 flag for stem-based
      lane assignment. Biggest expected jump in chart quality.

## Milestones

### 2026-05-11 (session 5)

Polish round informed by rhythm-game UX research (osu!mania, GH/RB, Beatstar,
Piano Tiles, Fortnite Festival).

- [x] **Hold detection**: new `app/pipeline/hold_detect.py` uses RMS energy
      sustain (SUSTAIN_THRESHOLD=0.55 of onset peak, MIN_HOLD_S=0.15,
      MAX_HOLD_S=2.0). Respects same-lane next note with safety margin.
      Three new tests for silent audio, sustained sine, and same-lane horizon.
- [x] **Hand-balance stream rule**: lane_assign now tracks consecutive
      same-hand notes (lanes 0-1=left, 2-3=right). After 2 same-hand notes
      in a stream (gap < beat_period/2), the next note prefers the opposite
      band. Charts no longer feel like one hand is overworked. Beat period
      threaded through from chart_builder via beat_info.bpm.
- [x] **Renderer polish (lane press flash, miss vignette, combo milestone)**:
      lane-color gradient flash on press (180ms decay), hit-line pulse on
      successful hits, red radial vignette on miss streaks of 3+, gold
      "X COMBO" splash at every 50-combo milestone. Rounded note rects with
      vertical gradient; hold notes get bright head/tail caps.
- [x] **Hit-delta meter**: horizontal calibration bar above the hit line
      showing the last 12 hit timings as colored ticks (color matches
      judgment tier, opacity fades with age). Lets the player see if they're
      drifting early or late mid-song.
- [x] **3-2-1 countdown on resume**: when the user pauses then unpauses a
      YouTube video, overlay pauses the video again, runs a 3-2-1-GO
      countdown over the receptor area (~58% down, GH/RB convention), then
      plays. Loop ignores keypresses during pause/countdown (still tracks
      pressedLanes for visual continuity). First play of a chart skips the
      countdown.
- [x] **Input gated by pause state**: handleInput no-ops scoring when
      `isPaused` is true so countdown keypresses don't accidentally land on
      notes near the pause point.

### 2026-05-11 (session 4)

Extension robustness around slow chart delivery and YouTube transport controls.

- [x] **Chart-received timeout vs late chart**: overlay starts a 15s error
      timer; if generation exceeds 15s the banner could appear even after
      `BB_LOAD_CHART` succeeds. Fix: cancel timeout and clear `errorMsg` on
      chart arrival (`chartTimeoutRef` in `overlay-main.tsx`).
- [x] **Video pause/play maps to game loop**: `BB_VIDEO_PAUSED` /
      `BB_VIDEO_PLAYING` call `GameLoop.pause()` / `resume()`, which stop
      and restart `requestAnimationFrame` instead of spinning while time is
      frozen.
- [x] **Video seek resets playable state**: `BB_VIDEO_SEEKED` calls
      `seekToVideoTime(videoTime)`. Chart notes are not recomputed. All
      note hit/miss flags reset; score and combo reset to zero; onsets before
      the seek point are marked missed only for rendering (no scoring penalty).
      Lets rewind/replay a section without corrupted cursor state.

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

### 2026-05-11 (session 3)

Stage 5 polish + chord notes. Tightening up before Stage 6 ship work.

- [x] **Settings panel in popup**: keybindings (per-lane KeyCapture button,
      duplicate-detection warning), note speed slider (0.5x-2.0x), panel
      opacity slider (40%-100%), hit-sound toggle, reset-to-defaults. All
      persist to chrome.storage.local immediately.
- [x] **Panel opacity wired**: overlay.css uses a `--panel-alpha` CSS variable
      multiplied against each gradient stop; overlay-main sets it from
      UserSettings.opacity on game start.
- [x] **Note speed wired**: overlay-main multiplies DEFAULT_RENDER_CONFIG
      .pixelsPerMs by settings.noteSpeed via renderer.setConfig.
- [x] **Results card shows previous best**: loadBestScore runs alongside
      chart load; if the just-played score beats it, the card shows a gold
      "New best!" line.
- [x] **Chord notes** (backend): lane_assign emits two simultaneous notes
      (low-band + high-band) when an onset is in the top 12% strength
      quantile AND both bands have lane capacity. CHORD_STRENGTH_QUANTILE=0.88,
      MIN_ONSETS_FOR_CHORDS=24 (below that, threshold is +inf so no chords).
- [x] **Difficulty shaper preserves chord pairs**: same-`t` notes with
      distinct lanes are always kept regardless of min_gap.
- [x] **Video listener leak fix**: content-script now tracks the pause/play
      /seeked listeners it attaches to `<video>` and detaches them on
      overlay close or on the next Start Game.
- [x] **Keyboard isolation from YouTube**: content-script installs a
      document-level keydown/keyup at capture phase. For lane-bound codes
      it calls stopImmediatePropagation + preventDefault and forwards the
      event to the overlay iframe via postMessage. The iframe's InputCapture
      gained an `injectKey` path so forwarded events route through the same
      hit-detection pipeline as direct-focus events. Editable element guard
      (input/textarea/contenteditable) so typing in YouTube search still
      works.
- [x] **HUD overlay sized like a widget**: panel is now 320px wide and
      capped at 620px tall (was full viewport height). Lead time at default
      noteSpeed is still ~800ms, plenty for any song.
- [x] **Hit-sound on note hits**: synthesized noise click via Web Audio API
      (`extension/src/overlay/sfx.ts`). Fires only on non-miss registerPress
      results (key spam stays silent). Gated by `sfxEnabled`, now defaulted
      to true. Volume 10%, 35ms duration, 8ms rate cap to avoid clipping on
      chord double-hits.

### Not started

- [ ] Stage 6: Shipping (deploy backend, Chrome Web Store submission).

## Test counts

- Backend: 32 pytest passing (added 3 hold-detect + 2 hand-balance tests).
- Extension: 50 vitest passing.

## Stage status

| Stage | Status | Exit criterion | Notes |
|-------|--------|----------------|-------|
| 0 | [x] | Architecture decisions logged | docs/DECISIONS.md, docs/PIPELINE.md |
| 1 | [x] | Notes fall on screen over a YouTube video | Works |
| 2 | [x] | Backend returns chart in <60s that follows the music | Verified on real YouTube audio with mix.wav |
| 3 | [x] | Full song playable end-to-end | osu! scoring + 6-tier judgments + draggable HUD |
| 4 | [~] | Friend on their own machine reports it feels tight | Calibration page implemented; in-the-wild test pending |
| 5 | [~] | Hand it to a stranger, they play through without help | Settings panel done; best-score on results; chord notes. Pause/resume countdown + lane FX pending |
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
