# PROGRESS

A running log of milestones, blockers, and open tasks. Newest entries on top.

## Legend

- [x] done
- [~] partial
- [ ] not started
- [!] blocked

## Open blockers

None right now. The skeleton + game loop + calibration are working end-to-end against a placeholder chart. Real audio analysis runs and is tested against synthetic input. The remaining gap to a "Stage 2 done" rating is exercising the real pipeline against actual YouTube audio in the browser flow.

## Open TODOs (rolling)

- [ ] Manual playtest the extension on a live YouTube video: load `extension/dist` unpacked, open a video, hit Start.
- [ ] Decide between tab capture (Option A) and yt-dlp (Option B) for production. Current code supports both: tab capture is the production path, yt-dlp behind `BACKEND_ALLOW_YTDLP` is the dev-only path. Final call moves to Stage 2 of real audio.
- [ ] Bench librosa beat tracking on three reference songs of differing genre.
- [ ] If chart feel is rough on real audio, evaluate Demucs (gate behind `USE_DEMUCS=1` and a GPU runtime).
- [ ] Sync hardening: test on bluetooth headphones, wired headphones, laptop speakers.
- [ ] Hook results screen up to scores history view in the popup.
- [ ] Telemetry shape for Stage 6 (Plausible or Umami; anonymous only).

## Milestones

### 2026-05-11 (session 1)

Built the project skeleton and pushed deeper than just Stage 1 because the game logic modules are pure and easier to verify together.

- [x] Read CLAUDE.md and PLAN.md, captured working agreements.
- [x] Scaffolded repo: backend/, extension/, shared/, docs/.
- [x] Logged initial architecture decisions in docs/DECISIONS.md.
- [x] Stage 1.1: FastAPI skeleton (health, /charts/generate with placeholder chart, request-id middleware, CORS).
- [x] Stage 1.2: Extension MV3 skeleton (popup, content script, overlay iframe, service worker).
- [x] Stage 1.3: Chart JSON contract (shared/chart-schema.json, Pydantic models, TS types). Cross-validation test guards drift.
- [x] Stage 2.1: Audio ingest (content-hash cache, optional yt-dlp behind a flag).
- [x] Stage 2.2: Analysis pipeline (librosa beat tracking, spectral-flux onsets, rule-based lane assignment, difficulty shaping).
- [x] Stage 2.3: Pipeline tests on synthetic click track (bpm within +/- 8 of ground truth).
- [x] Stage 3.1-3.3: Input capture, hit detection, scoring.
- [x] Stage 3.4-3.5: Canvas HUD, state machine.
- [x] Stage 4: Calibration page (metronome scheduled via WebAudio for clock accuracy, offset persisted to chrome.storage.local).
- [~] Stage 5: Polish (pause/resume bones present via state machine but no UI; score history is saved but no UI panel; key remapping requires manual storage edit for now).
- [ ] Stage 6: Shipping (no deploy target yet, no Chrome Web Store submission).

## Test counts

- Backend: 24 passing (health, models, routes, pipeline units, synthetic E2E, ingest).
- Extension: 27 passing (clock, hit-detection, scoring, state machine, contract, calibration).

## Bugs found and fixed this session

- librosa `beat_track` returns a 1-element numpy array for `tempo` (not a scalar). Caught by `test_pipeline_synth`. Fixed in `app/pipeline/beat_track.py`.
- Pydantic chart serialization wrote `null` for optional fields, which the JSON schema rejected. Switched routes to `response_model_exclude_none=True` and updated `fake_chart` to match.
- TS error: `findCandidateNote` test had two contradicting `expect` calls. Rewrote the test to widen the search window so the lane-filter behavior is what's asserted.
- TS error: post-build copy plugin used CommonJS `require` inside an ESM Vite config. Switched to ESM imports.

## Stage status

| Stage | Status | Exit criterion | Notes |
|-------|--------|----------------|-------|
| 0 | [x] | Architecture decisions logged | docs/DECISIONS.md |
| 1 | [x] | Notes fall on screen over a YouTube video | Achieved end-to-end via placeholder chart. Manual visual verification still needed. |
| 2 | [~] | Backend returns chart in <60s that follows the music | Pipeline modules implemented and verified on synthetic audio. Real YouTube audio path lives behind BACKEND_ALLOW_YTDLP. |
| 3 | [x] | Full song playable end-to-end with score and accuracy | Input + hit + scoring + state machine implemented; loop ties them together. Manual playtest pending. |
| 4 | [~] | Friend on their own machine reports it feels tight | Calibration math + page implemented; sync hardening across audio devices not yet exercised. |
| 5 | [ ] | Hand it to a stranger, they play through without help | Settings UI, score-history panel, remapping, error-state polish all open. |
| 6 | [ ] | Backend deployed, extension submitted | Not started. |
