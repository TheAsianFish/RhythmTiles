# Ship checklist

A pre-flight list to walk before each release. Skim this when prepping a tagged build.

## Backend

- [ ] `pytest` clean on the current commit.
- [ ] `ruff check backend` returns zero issues.
- [ ] `.env.example` covers every env var the code reads.
- [ ] Health endpoint returns 200 from the deploy target.
- [ ] CORS origins set to the extension's chrome-extension:// origin (not `*`).
- [ ] Cache directory writable on the host; old entries pruned.
- [ ] Pipeline cold start is under 60s on the deploy target for a 4-minute song.
- [ ] Logs include request IDs and pipeline timing.

## Extension

- [ ] `npm test` clean.
- [ ] `npm run build` clean with no TS errors.
- [ ] Load `dist/` unpacked in a fresh Chrome profile and confirm the popup opens.
- [ ] Open a real YouTube video, click Start, see notes fall.
- [ ] Calibrate timing on the same machine and confirm offset persists across reloads.
- [ ] Play through a full short song and reach the results screen.
- [ ] Test on bluetooth headphones (high audio latency) and wired (low). Calibration values should differ.
- [ ] Verify pause/resume on the underlying video pauses the game cleanly.
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
