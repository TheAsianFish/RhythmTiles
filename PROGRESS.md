# PROGRESS

A running log of milestones, blockers, and open tasks. Newest entries on top.

## Legend

- [x] done
- [~] partial
- [ ] not started
- [!] blocked

## Open blockers

None right now.

## Open TODOs (rolling)

- [ ] Decide between tab capture (Option A) and yt-dlp (Option B) by end of Stage 1. Current lean: tab capture for production, yt-dlp behind a dev flag for repeatable testing.
- [ ] Benchmark librosa beat tracking on three reference songs once Stage 2 lands.
- [ ] Confirm Demucs cost/benefit. Default: skip in v1, use spectral-flux on the full mix.
- [ ] Calibration UX flow on first run.
- [ ] Telemetry shape for Stage 6.

## Milestones

### 2026-05-11 (session 1)

- [x] Read CLAUDE.md and PLAN.md, captured working agreements.
- [x] Scaffolded repo: backend/, extension/, shared/, docs/.
- [x] Wrote .gitignore covering Python venv, node_modules, audio cache, secrets.
- [x] Logged initial architecture decisions in docs/DECISIONS.md.
- [ ] Stage 1: end-to-end skeleton (in progress).
- [ ] Stage 2: real chart generation pipeline (pending).
- [ ] Stage 3: playable game loop (pending).
- [ ] Stage 4: calibration + sync hardening (pending).
- [ ] Stage 5: polish (pending).
- [ ] Stage 6: shipping (pending).

## Stage status

| Stage | Status | Exit criterion | Notes |
|-------|--------|----------------|-------|
| 0 | [x] | Architecture decisions logged | Skipped formal spike scripts; decisions in docs/DECISIONS.md |
| 1 | [ ] | Notes fall on screen over a YouTube video | In progress |
| 2 | [ ] | Backend returns chart in <60s that follows the music | Not started |
| 3 | [ ] | Full song playable end-to-end with score and accuracy | Not started |
| 4 | [ ] | Friend on their own machine reports it feels tight | Not started |
| 5 | [ ] | Hand it to a stranger, they play through without help | Not started |
| 6 | [ ] | Backend deployed, extension submitted | Not started |
