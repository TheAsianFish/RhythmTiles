# Architecture decisions

Append-only log of decisions that shape the project. Each entry records the date, the question, the choice, and why.

## 2026-05-11: YouTube seek and pause behavior in the overlay loop

**Question:** What happens when the user pauses the video or scrubs the timeline?

**Choice:**
- **Pause / play:** `BB_VIDEO_PAUSED` and `BB_VIDEO_PLAYING` from the content
  script update the bridged clock and call `GameLoop.pause()` / `resume()`.
  Pause cancels the rAF tick; play restarts it. No countdown yet (that is a
  separate Stage 5 polish item).
- **Seek:** `BB_VIDEO_SEEKED` updates clock time and calls
  `GameLoop.seekToVideoTime(t)`, which converts to game ms with the stored
  calibration offset. All `NoteRuntime` hit/miss flags clear; score state
  resets to empty. Notes with start time before the new position (minus the
  meh window) are marked `missed` only so the renderer hides them; they do
  not add miss penalties. The chart JSON is never refetched.

**Why:** The previous behavior advanced the miss cursor on large time jumps,
which spammed misses and broke rewind. Resetting local state keeps one chart
load authoritative and matches user expectations for "scrub = start this
section fresh."

**Revisit when:** we want "resume score from before seek" or tallies per
segment, which would need a different scoring model.

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

## 2026-05-11: Chord notes (implemented)

**Question:** Should two-note chords (simultaneous keys) be supported?

**Choice:** Yes. Lane assigner now emits chord pairs when an onset is in the
top quantile of normalized strength AND both bands have lane capacity at
that moment.

**Mechanics:**
- `CHORD_STRENGTH_QUANTILE = 0.88` (top 12% of onsets eligible).
- `MIN_ONSETS_FOR_CHORDS = 24`; below that the threshold becomes +inf so
  no chords are emitted (strength distribution too noisy on tiny clips).
- A chord is one low-band lane (0 or 1) plus one high-band lane (2 or 3),
  picked by the existing per-band toggle. If either band has no capacity
  inside `HIT_WINDOW_S`, the onset falls back to a single note.
- The difficulty shaper has a chord-aware branch: same-`t` notes with
  distinct lanes are always kept regardless of `min_gap`, so the thinner
  never tears a chord apart.

**Why:** the data model + game loop already supported it (multiple notes
can share `t` with distinct lanes; hit detection scopes by lane). The
missing piece was the assigner. Top-quantile strength is a reasonable
proxy for "the player would intuit a chord here" without a stem-based
classifier.

**Revisit when:** chord rate post-thinning ends up too low (<5%) or too
high (>20%) on real songs. Tune CHORD_STRENGTH_QUANTILE.

## 2026-05-11: Keyboard interception lives in the content script

**Question:** How do we stop YouTube from acting on d/f/j/k key presses
when the game is running?

**Choice:** Content script installs a document-level keydown/keyup at
capture phase. For lane-bound keys it calls
`stopImmediatePropagation()` + `preventDefault()` and forwards the event
to the overlay iframe via `postMessage`. The overlay's `InputCapture`
exposes an `injectKey` method that funnels external events through the
same pipeline as DOM events.

**Why:**
- YouTube installs its own keydown listeners. `k` toggles pause, `j` and
  `l` seek by 10s, `f` toggles fullscreen, etc. Without interception,
  every game key would also trigger YouTube actions.
- `stopImmediatePropagation` at capture phase fires before bubble-phase
  listeners (which is where most JS handlers live) and also blocks other
  capture-phase listeners on the same target. `preventDefault` covers
  browser-default behaviors.
- We skip interception when `event.target` is an editable element
  (input/textarea/contenteditable) so users can type into YouTube's
  search bar normally while the game is running.

**Revisit when:** YouTube adds a listener attached at `document` capture
phase that fires before us, or if MV3 service workers gain a way to
intercept page-level keys directly (unlikely).

## 2026-05-11: HUD sized as a floating widget

**Question:** How tall and wide should the overlay panel be?

**Choice:** 320px wide, 620px max height (was 360px wide and full viewport
height minus margins).

**Why:**
- A rhythm game needs vertical room for note travel, but full-screen
  feels like a takeover. Other Chrome extensions (PiP, SponsorBlock,
  Plasmo widgets) sit as compact floating panels and don't dominate.
- 620px at the default `pixelsPerMs=0.55` still gives ~800ms of note
  lead time, which is plenty for any music. Users who want more can
  drag-resize later (out of scope for v1).

## 2026-05-11: Hit-sound is synthesized, not bundled

**Question:** Where does the click sound come from?

**Choice:** Synthesize a short noise burst on demand via the Web Audio
API (`overlay/sfx.ts`).

**Why:**
- Bundling an asset means a fetch (or base64 inflation in the JS bundle)
  and another permission to declare. Synthesis is ~30 lines of code,
  zero bytes of asset, and identical playback latency.
- Triggered only on non-miss `registerPress` results so key spam without
  a corresponding note stays silent (user requirement).
- 10% gain, 35ms duration, 8ms rate cap to prevent clipping when chord
  double-hits or buffered presses fire in quick succession.

## 2026-05-11: Settings live in the popup, not a separate page

**Question:** Where do users configure keybindings, note speed, opacity?

**Choice:** Inline in the popup, hidden behind an "Advanced settings"
disclosure. Calibration stays a separate full-tab page because it needs
keyboard focus and audio playback.

**Why:** popup is the one place users go before every play session. Adding
a second settings page splits attention and adds another click. The
disclosure keeps the default popup view clean for casual users while
power users can rebind keys and dial speed without navigating away.

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
