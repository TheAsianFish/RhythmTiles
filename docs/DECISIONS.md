# Architecture decisions

Append-only log of decisions that shape the project. Each entry records the date, the question, the choice, and why.

## 2026-05-11: Six-tier judgment system with OD-based windows

**Question:** What hit-window scheme do we use?

**Choice:** osu!mania v1 formulas. Six tiers: max, great, good, ok, meh, miss.

| Tier | Base score | Window |
|---|---|---|
| MAX | 300 | ±16.5 ms (fixed) |
| GREAT | 300 | ±(64 - 3·OD) ms |
| GOOD | 200 | ±(97 - 3·OD) ms |
| OK | 100 | ±(127 - 3·OD) ms |
| MEH | 50 | ±(151 - 3·OD) ms |
| MISS | 0 | anything outside ±(188 - 3·OD) |

**Why:** the user explicitly asked for stricter timing; the four-tier system
was too forgiving. OD-based formulas give us one knob (5..10) that controls
the whole tier spread. Default OD=8 is roughly "challenging" (great window
~40ms). User-tunable from the popup.

**Revisit when:** playtesting on a real device suggests a different curve, or
if we add a "no-fail" mode where windows widen mid-song after misses.

## 2026-05-11: Combo-multiplied scoring (osu!-style)

**Question:** How does the score accumulate?

**Choice:** `score += baseValue × combo`. Each non-miss increments combo by 1.
A miss resets combo to 0.

**Why:** the user requested osu! scoring. With this formula, a clean run
grows the score quadratically with note count, which makes long chains
disproportionately valuable. This rewards consistency more than raw note
count, which is the whole point of a rhythm game.

**Revisit when:** if it makes scoring hard to read at extreme combos, we can
display in K/M units, but the math stays the same.

## 2026-05-11: Chord notes (planned, not yet built)

**Question:** Should two-note chords (simultaneous keys) be supported?

**Choice:** Plan it now, build it later. The data model already supports it
(multiple `Note` entries can share the same `t` value with different lanes).
The game loop, input capture, and hit detection all scope by lane, so two
same-`t` notes "just work" as a chord without changes.

What's missing:
1. The lane assigner picks one lane per onset today; for chord support it
   would split "strong" onsets (top-quantile onset_strength) into two notes
   across the band split (e.g. one low-band lane + one high-band lane).
2. The canvas renderer doesn't visually link chord notes; could add a faint
   horizontal connector so the player sees the chord at a glance.

Tracked in `extension/src/game/types.ts` NoteRuntime docstring and
`docs/PIPELINE.md` "Future: chord notes".

## 2026-05-11: yt-dlp + bundled ffmpeg for real chart generation

**Question:** Do we depend on a system ffmpeg install?

**Choice:** No. We use `imageio-ffmpeg`, which is pip-installable and ships a
platform-native ffmpeg binary. yt-dlp gets the path via `--ffmpeg-location`.

**Why:** the user shouldn't have to install ffmpeg system-wide. One
`pip install '.[ytdlp]'` covers both yt-dlp and ffmpeg.

## 2026-05-11: Lane balance via median centroid

**Question:** How do we route onsets to lanes 0-1 vs 2-3?

**Choice:** Use the MEDIAN spectral centroid across the song as the split.

**Why:** a fixed 1500 Hz cutoff put 99% of pop-song onsets above the cutoff
(verified on Rick Astley: 1091/1093 onsets had centroid > 1500 Hz). Lanes
0-1 were nearly empty. Splitting at the median guarantees ~50/50 across the
band, so all four lanes get used regardless of genre.

**Revisit when:** we add stems (Demucs); drum stems would route directly to
lane mapping based on instrument rather than band, which is more musical.

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
