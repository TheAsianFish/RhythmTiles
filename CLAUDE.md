# CLAUDE.md

This file is the persistent context for Claude Code working on this project. Read it fully before any task. Update it when architecture decisions change.

## Project: BeatBridge (working name)

A Chrome extension that lets a user, while watching any YouTube video (or any tab with audio), activate an overlay rhythm game generated from the playing audio in near-real-time. Think osu!mania or Piano Tiles, 4 lanes (d/f/j/k), single notes and held sliders, with combo, score multiplier, and accuracy tracking.

## Owner context

Solo developer (Patrick). CS student at UCSD, comfortable with full-stack web, Python ML, and shipping under deadlines. Primary IDEs: Cursor Pro and Claude Code (Max). Writing/comment style: direct, human, no em dashes, no AI cadence, no corporate filler. Match that voice in code comments, commit messages, and any user-facing copy.

## Working agreements with Claude Code

1. **Stay at the layer asked.** If the task is "design the chart generation pipeline," produce architecture and interfaces, not implementation. If the task is "implement the note scheduler," write the actual code. Do not leak layers.
2. **Use Sonnet for implementation, Opus for architecture/refactors/debugging hard issues.** Default to Sonnet for file edits, test writing, and well-scoped feature work. Escalate to Opus when the task involves cross-cutting decisions, performance hunting, or anything where being wrong costs hours.
3. **No speculative abstractions.** Build for the current stage, not the imagined stage 4. If something needs to scale later, leave a clear TODO with the trigger condition ("when chart generation exceeds 30s p50, move to backend worker").
4. **Tests before features once core is stable.** Stage 0/1 can move fast and dirty. From Stage 2 onward, every new module gets at least a smoke test. Audio timing logic gets exhaustive tests because regressions there are invisible until someone plays the game and it feels off.
5. **Always run, never assume.** Audio/timing/ML code lies in ways that look right on paper. Run a quick script, log actual values, verify before claiming done.
6. **Commit at logical boundaries.** Conventional commits (`feat:`, `fix:`, `chore:`, `refactor:`). One concern per commit. If a commit message needs "and," split it.
7. **No em dashes in any output.** Use periods, commas, or parentheses. This applies to code comments, docs, and UI copy.

## Architecture overview

The system has four major layers. Keep them strictly separated so each can be swapped without rewriting the others.

```
[ Chrome Extension (TS/React) ]
   |  - HUD overlay, input capture, game loop, rendering
   |  - Talks to: audio capture API, backend, local storage
   |
[ Audio Capture & Sync Layer ]
   |  - Option A: chrome.tabCapture -> MediaStream -> backend
   |  - Option B: extract videoId, backend pulls audio via yt-dlp
   |  - Maintains AudioContext clock locked to video.currentTime
   |
[ Backend Service (Python/FastAPI) ]
   |  - Receives audio, runs analysis pipeline
   |  - Returns Chart JSON (notes, timing, metadata)
   |  - Caches by (videoId, difficulty) to avoid recomputation
   |
[ Chart Generation Pipeline (Python) ]
      - Source separation (Demucs) -> stems
      - Beat/tempo tracking (madmom or librosa)
      - Onset detection per stem
      - Lane assignment heuristic / ML model
      - Difficulty shaping (note density, slider ratio)
      - Output: Chart JSON
```

The Chart JSON is the contract between the backend and the extension. Define it once, version it, and never break it without bumping the version.

## Tech stack (locked in for v1)

**Extension**
- Manifest V3
- TypeScript, React for HUD (small surface, but worth it for state management)
- Vite for bundling
- Web Audio API for timing
- Canvas 2D for note rendering (WebGL is overkill for 4 lanes; revisit if perf demands)

**Backend**
- Python 3.11+
- FastAPI + Uvicorn
- Demucs (htdemucs) for source separation
- madmom for beat tracking (fallback: librosa)
- librosa for onset detection and feature extraction
- yt-dlp for audio fetch (if going Option B)
- Redis for chart cache (optional v1; SQLite is fine to start)
- Hosted on a single GPU box or Modal/Replicate for the heavy ML calls

**Storage**
- Chart cache: SQLite -> Postgres when multi-user
- User scores/settings: chrome.storage.local for v1, backend later

**Dev tooling**
- pnpm for the extension
- uv or poetry for Python (prefer uv, it's faster)
- ruff + mypy on Python
- eslint + prettier on TS
- Playwright for extension E2E tests
- pytest for backend

## The Chart JSON contract

```json
{
  "version": "1.0",
  "audio": {
    "source": "youtube",
    "videoId": "dQw4w9WgXcQ",
    "duration": 213.5,
    "bpm": 113.0,
    "bpmCurve": [[0.0, 113.0], [120.5, 114.2]]
  },
  "metadata": {
    "generatedAt": "2026-05-11T12:00:00Z",
    "pipelineVersion": "0.3.1",
    "difficulty": "normal",
    "keyMode": 4
  },
  "notes": [
    { "t": 1.234, "lane": 0, "type": "tap" },
    { "t": 1.567, "lane": 2, "type": "hold", "duration": 0.5 }
  ],
  "sections": [
    { "start": 0.0, "end": 30.0, "intensity": 0.3, "label": "intro" }
  ]
}
```

- `t` is seconds from the audio's t=0, not from when the game started.
- `lane` is 0-3 mapping to d/f/j/k.
- `type` is `tap` or `hold`. Holds have `duration` in seconds.
- `bpmCurve` is optional but recommended; without it, the game can still snap visuals to a constant BPM.
- `sections` are optional; used for difficulty pacing and visual cues.

## Timing model (this is the part that has to be right)

There are three clocks. Confusing them is the #1 source of "the game feels off" bugs.

1. **Audio clock:** `AudioContext.currentTime` for any audio we play ourselves, or `video.currentTime` for the YouTube player. This is ground truth.
2. **Game clock:** Derived from the audio clock. Every frame, recompute `gameTime = audioClock + userLatencyOffset`. Never drift from this.
3. **Render clock:** `requestAnimationFrame` timestamp. Used only to decide when to draw, never to decide when a note should be hit.

User latency offset comes from a calibration screen the user runs once. They tap d/f/j/k along to a metronome; we measure the average offset between expected and actual taps. Persist it per user.

The note hit window:
- Perfect: ±25ms
- Good: ±50ms
- OK: ±100ms
- Miss: >100ms or no hit by the time the note crosses the line

These are starting values. Tune after playtesting.

## What is intentionally out of scope for v1

- Multiplayer
- Custom map editor
- Difficulty selection beyond Easy/Normal/Hard presets
- Non-YouTube sources (Spotify, SoundCloud) — possible later but each is its own capture story
- Mobile
- Account system (use chrome.storage.local; sync if/when we add accounts)

## Non-negotiables

- **Audio sync precision under 20ms.** If we ship and it feels late or early, the product fails. Test this early and test it often.
- **First-time generation under 60s for a 4-minute song.** Anything longer and users bounce.
- **Cached generation under 2s.** Once a song has been mapped, replays must be instant.
- **No copyright bombs.** We do not ship copies of audio. The extension reads what's already playing in the user's tab. Backend audio fetch (if used) operates per-user, on-demand, not as a redistribution service.

## Open questions to revisit at each stage gate

1. Are we going tab capture (Option A) or videoId+yt-dlp (Option B)? Decide by end of Stage 1.
2. Demucs is slow. Do we need full separation, or can we get acceptable charts from spectral flux on the full mix? Benchmark in Stage 2.
3. Lane assignment: rule-based (pitch buckets, drum hits) vs trained model. Start rule-based; revisit only if charts feel bad after Stage 3 playtesting.
4. Where does the backend run? Local dev box is fine for Stage 1-3. For sharing/portfolio, deploy to Modal (cheap GPU on-demand) or a small Hetzner GPU box.

## Glossary

- **Chart / Beatmap:** The generated note pattern for a song.
- **Stem:** A separated audio track (vocals, drums, bass, other) from source separation.
- **Onset:** A detected note-start event in audio.
- **Lane assignment:** The decision of which of the 4 columns a given onset belongs to.
- **Calibration offset:** Per-user audio latency, measured once, applied to all timing.