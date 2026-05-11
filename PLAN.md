# PLAN.md

Staged build plan for BeatBridge. Each stage has a clear exit criterion. Do not advance to the next stage until the current one's exit criterion is met and verified by running the thing, not by reading the code.

Total realistic timeline solo: 10-14 weeks if working part-time around classes. Faster if a stage gets compressed because something turns out easier than expected. Slower is fine; do not skip stages.

---

## Stage 0: Spike & feasibility (1 week)

The goal here is not to write production code. It's to answer the questions that, if answered wrong, kill the project. Throwaway scripts are fine. Notebook output and screenshots are the deliverables.

### Tasks

1. **Audio capture spike.** Build a minimal MV3 extension that captures audio from a YouTube tab via `chrome.tabCapture` and dumps the first 10 seconds to a WAV file (download). Confirm: does this actually work? Is the audio clean? What's the permission UX like?
2. **yt-dlp spike.** Write a 30-line Python script that takes a YouTube URL, extracts audio, returns a WAV. Time it. Note any failures.
3. **Beat tracking spike.** Run `librosa.beat.beat_track` on three songs of different genres (a JPOP track with clear beat, a ballad with ambiguous tempo, an instrumental with tempo shifts). Compare detected beats to manually-tapped beats. How close is it?
4. **Onset detection spike.** Same three songs. Run `librosa.onset.onset_detect`. Eyeball whether the onsets land on perceived note starts.
5. **Source separation spike.** Run Demucs on one of the songs. How long does it take? Is the drum stem usable for rhythm extraction?
6. **Sync spike.** In the extension, play a YouTube video and overlay a div that flashes on every detected beat (using a precomputed beat list from Stage 0.3). Watch it. Does it feel synced? Off by how much?

### Exit criterion

You can describe, in two paragraphs, what the full pipeline will look like, including which capture method you're using and which ML libraries are confirmed. You have rough timing numbers for each step.

### Decisions to log in CLAUDE.md after this stage

- Tab capture vs videoId+yt-dlp
- Will the backend run Demucs, or skip separation?
- Beat tracker choice (librosa vs madmom)

---

## Stage 1: Skeleton end-to-end (2 weeks)

Get the dumbest possible version of the full loop working. No game yet. No real chart generation. Just: extension activates, sends audio somewhere, gets back a fake chart, displays notes falling on screen.

### 1.1 Backend skeleton

- FastAPI app with one endpoint: `POST /charts/generate` accepting either audio bytes or a videoId, returning a hardcoded Chart JSON with 20 notes evenly spaced over 30 seconds.
- Health check, basic logging, request ID middleware.
- Dockerfile so it can move to a real host later without surgery.

### 1.2 Extension skeleton

- MV3 manifest, content script injected on `youtube.com/watch*`.
- Popup with one button: "Start Game."
- Click triggers: get current video ID (or capture tab audio), send to backend, receive chart, render an HUD overlay with notes falling from top to bottom in 4 lanes.
- Notes do not need to be hittable yet. They just fall.

### 1.3 The contract

- Chart JSON schema written as TypeScript types in the extension and Pydantic models in the backend. Generate from a shared JSON Schema if you want to be tidy; otherwise duplicate and add a test that ensures they match.

### Exit criterion

You click a button on a YouTube page, wait a few seconds, and see notes falling on the screen overlaying the video. They are not playable, not synced to music, not generated from real audio. They just exist.

---

## Stage 2: Real chart generation (2-3 weeks)

This is the meat of the project. Replace the hardcoded chart with a real pipeline.

### 2.1 Audio ingest

- Implement the capture method chosen in Stage 0.
- The backend receives audio (WAV or MP3) and writes it to a temp location keyed by a content hash.
- Cache layer: if a chart for this content hash exists, return it immediately.

### 2.2 Analysis pipeline

Build this as a sequence of pure functions, each taking audio + metadata in and returning enriched metadata out. Makes testing and swapping easy.

```
audio -> beat_track() -> {beats, downbeats, bpm, bpmCurve}
audio -> onset_detect() -> {onsets: [(time, strength, freq_band)]}
audio -> source_separate() -> {drums, bass, vocals, other}  [optional]
{onsets, beats, stems} -> lane_assign() -> [{t, lane, type}]
notes -> shape_difficulty(target='normal') -> notes  [thinned and patterned]
notes -> hold_detection() -> notes  [convert sustained onsets to holds]
notes + metadata -> chart.json
```

### 2.3 Lane assignment (the creative part)

Start with a rule-based approach. Three signals:
- **Frequency band:** Low onsets -> lanes 0-1, high onsets -> lanes 2-3.
- **Stem source:** Drum hits get prioritized lane placement; vocal melodic onsets fill remaining lanes.
- **Anti-clustering:** Never place two notes in the same lane within the hit window.

Document the heuristic clearly in code. Future-you or future-Claude will swap it for an ML model and needs to know what was replaced.

### 2.4 Difficulty shaping

Three presets:
- Easy: ~1 note per beat, no holds, lanes spread out.
- Normal: ~1.5 notes per beat, holds on sustained vocals, occasional doubles.
- Hard: ~2-3 notes per beat, holds, doubles, complex patterns.

Implement as a post-processing pass that thins or thickens the raw onset list. Do not regenerate from scratch per difficulty; use the same onsets and filter.

### Exit criterion

Backend takes any YouTube videoId and returns a Chart JSON in under 60 seconds. The chart, played back in the Stage 1 overlay, looks like it follows the music when you watch it. Notes land on beats. It's not necessarily fun yet; just not random.

---

## Stage 3: The game loop (2-3 weeks)

Make it playable. This is the stage where it stops being a tech demo and starts being a game.

### 3.1 Input handling

- Keyboard listeners on d/f/j/k with millisecond timestamps from `performance.now()`.
- Translate to game time using the timing model in CLAUDE.md.
- Edge cases: held keys, key released early, key pressed twice rapidly.

### 3.2 Hit detection

- For each input event, find the nearest unhit note in that lane.
- Compute timing delta. Bucket into Perfect / Good / OK / Miss.
- For holds: track press-start and press-release; both must land in windows.

### 3.3 Scoring

- Base score per note: Perfect 300, Good 150, OK 50, Miss 0.
- Combo: increments on any non-Miss. Resets on Miss.
- Multiplier: 1x base, then +0.1x per 25 combo, capped at 4x.
- Accuracy: weighted average of hit ratings as a percentage.
- Health bar (optional but improves feel): drains on Miss, recovers on Perfect/Good.

### 3.4 HUD rendering

- Canvas 2D, full-tab overlay with opacity so the video remains visible.
- 4 lanes, notes fall from top, hit line near bottom (or vice versa per preference).
- Visible elements: lanes, hit line, falling notes, combo counter, score, accuracy, multiplier, health (if added).
- Hit effects: flash on key press, judgment text (PERFECT, GOOD, OK, MISS) for 200ms.
- Use `requestAnimationFrame` for draw, but compute positions from game time, not frame count.

### 3.5 Game state machine

```
IDLE -> LOADING (generating chart) -> CALIBRATING (if first run) -> READY -> PLAYING -> RESULTS
                                                                       |
                                                                       v
                                                                     PAUSED -> PLAYING
```

### Exit criterion

You can play a real song end-to-end, see your score and accuracy at the end, and the experience feels like a rhythm game. Not necessarily a *great* one yet, but recognizable.

---

## Stage 4: Latency calibration & sync hardening (1 week)

Drop this stage and the product feels broken on half the devices it runs on. Do not skip.

### 4.1 Calibration flow

- On first run, show a screen with a metronome at 120 BPM.
- Ask user to tap any key on each click for 16 beats.
- Drop the first 4 (warm-up). Average the offset of the remaining 12.
- Store `audioLatencyOffset` in chrome.storage.local.
- Provide a "recalibrate" button in settings.

### 4.2 Sync hardening

- Test on Bluetooth headphones, wired headphones, laptop speakers, external speakers. Calibration values should differ; the game should feel right with each.
- Add a debug overlay (toggleable) showing audio clock, game clock, frame time, and the last 10 hit deltas. Use this to verify timing under different conditions.
- Test what happens if the user scrubs the YouTube video while playing. Pause the game. Resume requires a brief re-sync.

### Exit criterion

A friend who has never seen the project can play a song on their own machine and describe the experience as "tight" or "responsive." Their hit timings cluster around 0ms after calibration.

---

## Stage 5: Polish & quality of life (1-2 weeks)

The stuff that makes it feel finished.

- **Visual polish:** Note skins, lane highlights on press, combo milestone effects (every 50, 100, 250), screen shake on miss streak (subtle).
- **Audio feedback:** Optional click/snap sound on each note hit. Some players love it, some hate it; make it toggleable.
- **Settings panel:** Lane width, note speed, opacity, audio latency offset, key bindings (let people remap from d/f/j/k).
- **Score history:** Save best score per song in chrome.storage.local. Show on results screen.
- **Pause/resume:** Spacebar pauses. Resume countdown 3-2-1 before audio resumes.
- **Error states:** Backend down, video unavailable, audio capture denied, chart generation timeout. Each gets a real error message, not a stack trace.
- **Loading state:** A real "generating your chart" screen with progress indicators if you can expose them from the backend (separation done, beats detected, lanes assigned, etc).

### Exit criterion

You can hand this to someone with no instructions beyond "install this and play," and they get through a full song without confusion.

---

## Stage 6: Shipping (1 week)

- **Backend deploy:** Modal, Replicate, or a Hetzner GPU box. Pick based on cost vs latency tolerance. Modal is easiest for on-demand GPU.
- **Extension submission:** Chrome Web Store. Privacy policy, screenshots, demo video. Submission review can take days; build in slack.
- **Telemetry (privacy-respecting):** Anonymous metrics on generation time, error rates, average accuracy. No personal data. Use Plausible or self-hosted Umami.
- **README and landing page:** Even a simple one-pager on GitHub Pages. Demo GIF is non-negotiable.

---

## Future stages (not on the critical path)

- ML-based lane assignment trained on osu!mania chart data.
- Spotify and SoundCloud as additional sources.
- Custom chart editor for users to refine auto-generated charts.
- Leaderboards (requires accounts).
- Mobile companion app or web version.
- Real-time chart streaming for live audio (radio, livestreams) — this is genuinely hard and probably its own project.

---

## Risk register

Things that might go wrong and what to do about them.

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Chart generation too slow on cold start | High | High | Cache aggressively; warm backend; consider precomputing popular videos |
| Auto-generated charts feel bad | High | High | Stage 2 deliverable is "follows the music"; fun is iterative. Get feedback in Stage 3 |
| Audio sync drift on long songs | Medium | High | Resync game clock to audio clock every frame, not every N seconds |
| Chrome Web Store rejection (YouTube ToS concerns) | Medium | Medium | Use tab capture, not video downloading, in production. Frame as accessibility/educational tool |
| Demucs latency kills UX | Medium | Medium | Benchmark in Stage 0; fall back to no-separation if needed |
| Backend GPU costs | Low | Medium | Cache hard, consider CPU-only inference for lighter models |

---

## How to work with Claude Code on this

Claude Code's strengths and weaknesses are predictable. Use them.

**Use Claude Code for:**
- Scaffolding new stages (file structure, types, stubs)
- Writing tests once you've described the behavior
- Refactoring across files (renaming, restructuring)
- Boilerplate (FastAPI routes, React components, manifest config)
- Explaining unfamiliar libraries (Demucs, madmom internals)
- Debugging when you can describe what's wrong precisely

**Drive manually for:**
- The first cut of audio analysis logic, where intuition about what "feels right" matters
- Game feel tuning (hit windows, animation timing, sound design)
- Architectural decisions where you need to weigh tradeoffs
- Anything where you would otherwise just rubber-stamp Claude's output

**Per-stage prompt pattern:**
1. Drop CLAUDE.md and PLAN.md into context.
2. Tell Claude what stage you're on and what specific task within it.
3. Ask for the smallest unit of work that makes progress. Avoid "build me stage 3" prompts. Prefer "build the input handler module described in section 3.1, with tests."
4. Run the code. Verify it works. Commit before moving on.
5. When the stage exit criterion is met, update CLAUDE.md with any decisions that were made and start the next stage in a fresh session.

Keep sessions focused. Long sessions accumulate cruft and Claude starts to lose track of what's been decided. Fresh session per stage transition is a good default.