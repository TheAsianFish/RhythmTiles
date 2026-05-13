# Ship checklist

A pre-flight list to walk before each release. Skim this when prepping a tagged build.

## Backend

- [ ] `pytest` clean on the current commit (all modes).
- [ ] `ruff check backend` returns zero issues.
- [ ] `.env.example` covers every env var the code reads.
- [ ] `/healthz` returns 200 + the `ml` block from the deploy target.
- [ ] CORS origins set to the extension's chrome-extension:// origin (not `*`).
- [ ] Cache directory writable on the host; `<CACHE_DIR>/beats/` and
      `<CACHE_DIR>/onsets/` exist and are pruned alongside the chart cache.
- [ ] Pipeline cold start under 60s baseline (90s with ML) on the deploy
      target for a 4-minute song. `python -m app.tools.bench_ml_phase
      --baseline-only` is the canonical check.
- [ ] Logs include request IDs and pipeline timing.
- [ ] If shipping ML-full: verify Demucs weights download succeeds on
      a clean host (~80MB) and that subsequent generations use the
      per-stem onset cache (no Demucs re-run for repeat difficulties).
- [ ] Production hardening: `MAX_CONCURRENT_CHARTS` + `CHART_QUEUE_TIMEOUT_S`
      env vars set appropriately for the deploy target (CPU = 1 slot
      fail-fast, GPU = 1-2 slots short queue). Upload audio > 8 min
      rejects with 413. Audio cache prunes on startup.
- [ ] Mode-aware cache verified: generate a song at baseline, switch to
      ML-light by env flag, generate the same song + difficulty -
      should produce a DIFFERENT chart (not cache-hit). The `_mode_key`
      suffix on the cache key prevents cross-mode pollution.

## Extension

- [ ] `npm test` clean.
- [ ] `npm run build` clean with no TS errors.
- [ ] Load `dist/` unpacked in a fresh Chrome profile and confirm the popup opens.
- [ ] Open a real YouTube video, click Start, see notes fall.
- [ ] Calibrate timing on the same machine and confirm offset persists across reloads.
- [ ] Play through a full short song and reach the results screen.
- [ ] Test on bluetooth headphones (high audio latency) and wired (low).
      Calibration values should differ.
- [ ] Verify pause/resume on the underlying video pauses the game
      cleanly; resume runs the 3-2-1 countdown.
- [ ] Press `P` from inside the overlay (focus the iframe first):
      video toggles, countdown plays on resume.
- [ ] Scrub the YouTube timeline mid-song: countdown re-runs at the new
      position. Scrub while paused: position updates without countdown,
      countdown plays on next unpause.
- [ ] Click Replay on the results card: song restarts at 0, score
      resets, results card dismisses, countdown plays.
- [ ] Open the in-overlay menu (`☰` button): difficulty switch + Start
      regenerates the chart in place. Mode chip shows the right symbol
      (`≋` baseline / `♫` ML-light / `♫◓` ML-full / `♫◓✦` ML-max).
- [ ] Slider-only menu changes (sfxVolume, noteSpeed, opacity) close
      the menu and resume WITHOUT a chart refetch (audit Round 4 fix).
      Only difficulty changes trigger the multi-minute regenerate.
- [ ] Try each of the four skins (Midnight / Aurora / Arcade / Ember /
      Pro). Pro should show a flat off-white popup + flat white menu
      card (no gradient), dark text, gray slate accent.
- [ ] Let YouTube auto-advance to the next song: overlay stays open,
      menu auto-opens with the previous difficulty pre-selected.
- [ ] Toggle backend URL via `VITE_BACKEND_URL` and rebuild to confirm prod target.

## Cross-stack

- [ ] Bumped versions in `backend/pyproject.toml`, `extension/package.json`, and `extension/manifest.json` to the new version.
- [ ] Tag matches all three.
- [ ] Updated `PROGRESS.md` stage table.
- [ ] Updated `docs/DECISIONS.md` with anything new since the last release.

## Chrome Web Store submission (release-only)

- [ ] Privacy policy URL accessible.
- [ ] Demo video (under 30s) showing the popup, the game playing, and the results screen.
- [ ] At least three screenshots: popup, game, results.
- [ ] Permissions in the listing match `manifest.json` exactly. Justify each.
- [ ] Notes on data handling: tab capture only, no audio uploaded to third parties beyond the configured backend, no PII.
