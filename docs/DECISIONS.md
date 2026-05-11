# Architecture decisions

Append-only log of decisions that shape the project. Each entry records the date, the question, the choice, and why.

## 2026-05-11: Tab capture vs videoId+yt-dlp

**Question:** How does the backend get the audio it needs to analyze?

**Choice:** Support both, prefer tab capture for production.

- **Tab capture (Option A)** is the production path. The user installs the extension; while a YouTube tab is playing audio, the extension captures the MediaStream via `chrome.tabCapture.getMediaStreamId` and streams a short window (or the full song once we know length) to the backend. Pros: no copyright distribution concern, works on any audio source the user is already entitled to hear. Cons: needs the tab to actually play; cold-start UX has a wait while audio is collected.
- **yt-dlp (Option B)** is kept behind a dev flag (`BACKEND_ALLOW_YTDLP=1`) for repeatable local testing and CI. We do not ship yt-dlp to end users. Backend treats it as a developer convenience.

**Why:** Tab capture sidesteps the "are we redistributing copyrighted audio" question that would otherwise hold up Chrome Web Store review. yt-dlp stays useful for testing because the same video must yield a deterministic chart and we cannot trust real users to feed identical audio.

**Revisit when:** Web Store rejects us anyway, or tab capture turns out to be flaky on Manifest V3 service workers.

## 2026-05-11: Beat tracking library

**Question:** librosa or madmom?

**Choice:** librosa for v1.

**Why:**
- madmom is more accurate on tricky tempo curves but its install story on Windows + Python 3.13 is rough (Cython, numpy ABI pinning, archived pip releases). librosa installs clean.
- We can wrap beat tracking behind a `BeatTracker` interface so madmom drops in later without rippling.

**Revisit when:** Stage 2 playtesting shows beat detection failing on songs with tempo shifts.

## 2026-05-11: Source separation

**Question:** Run Demucs as part of every chart generation?

**Choice:** No, not in v1. Use spectral-flux onset detection on the full mix.

**Why:**
- Demucs on CPU is roughly 1x to 4x realtime (4-minute song takes 4 to 16 minutes). That blows our 60s cold-start budget.
- Drum-stem-only onset detection would be ideal, but rule-based lane assignment using frequency bands on the full mix gets us most of the way there for v1.
- Demucs stays in pyproject as an optional dependency. Pipeline calls it only if `USE_DEMUCS=1` is set.

**Revisit when:** Charts feel bad after Stage 3 playtesting and we have a GPU-backed deploy target.

## 2026-05-11: Lane assignment

**Question:** Rule-based vs trained model.

**Choice:** Rule-based for v1, behind a clean interface so a model can replace it later.

**Why:** PLAN.md Stage 2.3 calls for this. Rules: frequency band (low to 0-1, high to 2-3), anti-cluster within hit window, drum-priority if stems are available.

## 2026-05-11: Chart cache backing store

**Question:** SQLite vs Redis.

**Choice:** SQLite for v1.

**Why:** Single backend instance, single writer, no need for multi-process atomicity yet. Migrate to Postgres when we go multi-user (per PLAN.md storage section).

## 2026-05-11: Note rendering

**Question:** Canvas 2D vs WebGL.

**Choice:** Canvas 2D.

**Why:** 4 lanes, sub-100 notes on screen at once. Canvas 2D is well under the perf budget and avoids shader complexity. Revisit if we ever need particle effects beyond what's cheap on canvas.

## 2026-05-11: Game loop timing source

**Question:** Drive the game from `requestAnimationFrame` or from the audio clock?

**Choice:** Audio clock is ground truth, rAF is only for "when to draw."

**Why:** Per CLAUDE.md timing model. Confusing these is the #1 cause of rhythm-game feel bugs. Each frame we recompute `gameTime = audioClock + userLatencyOffset` and position notes from `gameTime`. The render frame timestamp never enters note-position math.
