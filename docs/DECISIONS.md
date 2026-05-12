# Architecture decisions

Append-only log of decisions that shape the project. Each entry records the date, the question, the choice, and why.

## 2026-05-12: In-overlay menu (no extension reload to change difficulty)

**Question:** Changing difficulty mid-session required reloading the
extension. SPA navigation to the next YouTube video force-closed the
overlay panel and re-mounted it ~10s later. Both flows interrupted the
player and made it hard to iterate on difficulty between songs.

**Choice:** Add a `☰` button to the overlay header that opens an inline
menu inside the iframe with difficulty, OD, note speed, opacity, hit
sound, and hit volume. Start posts BB_REQUEST_NEW_CHART to the content
script; the existing chart-fetch path returns the new chart via
BB_LOAD_CHART without rebuilding the iframe. SPA navigation now keeps
the overlay alive: pauses the new video, re-binds pause/play/seeked
listeners to the new <video>, and posts BB_NEW_VIDEO to the overlay so
it auto-opens the menu with the previous difficulty pre-selected.

**Why:** Players were rage-quitting Hard within 10 seconds of starting
because the only way to retry on Normal was to reload the extension and
reposition the panel. The menu eliminates that friction. Keeping the
overlay anchored across songs preserves panel position + scroll state +
binding overrides for the whole session.

**Revisit when:** Players want to change key bindings mid-session, or
want to recalibrate without leaving the page. Both would extend the
existing MenuPanel.

## 2026-05-12: Mode chip in the menu (backend-pipeline awareness)

**Question:** Without restarting the backend it's hard to know which
pipeline is producing the chart on screen. A player switching from
Baseline to ML-light has no way to verify the flag took effect.

**Choice:** /healthz now returns an `ml` block (`beatThisFlag`,
`beatThisActive`, `demucsFlag`). The overlay menu re-pings on every
open and renders a compact chip: `≋ Baseline`, `♫ ML-light`, `♫◓ ML-full`,
plus `⚠ ML flag, inactive` (flag set but package not importable),
`⌛ Restart needed` (server up but no ml field, i.e. older build), `✕
Down` (ping failed), `… Checking` (ping in flight). Color-coded:
blue/amber/red/grey.

**Why:** Earlier in development a 2-hour debugging session was wasted
because Beat This! was off but the player thought it was on. A chip
turns "is ML active?" from a CLI question into a glance. The chip also
catches partial-failure states (e.g. flag on, package install broken)
that would otherwise silently fall back to librosa.

**Revisit when:** We add Phase 3 (MERT section detection). The chip
will need a fourth symbol or a separate row.

## 2026-05-12: Pause / seek / replay all run the 3-2-1 countdown

**Question:** Players reported that mid-song actions (P pause, scrubbing
the YouTube timeline, clicking Replay on the results card) either
silently dropped them back in or left UI in a broken state. Specifically:
P didn't work when the overlay had focus, scrubbing skipped the
countdown, Replay restarted the song but left the results card on top.

**Choice:**
- Install a mirror `P` keydown listener inside the overlay iframe that
  posts BB_REQUEST_VIDEO_TOGGLE to the parent. The content script
  toggles the video; resume round-trips back as BB_VIDEO_PLAYING which
  runs the existing 3-2-1.
- BB_VIDEO_SEEKED now carries `wasPlaying`. Scrubbing while playing
  triggers `scheduleCountdown()`; scrubbing while paused defers to the
  next play-resume which already runs the countdown.
- Replay sets `pendingReplayRef`, force-resets the BridgedClock to 0
  paused, then re-mounts the GameLoop with `start({ startPaused: true })`.
  The countdown's `resume()` at the end of 3-2-1 actually starts the
  rAF for the first time, so the new loop's first tick never reads
  the stale end-of-song clock.

**Why:** Three different bug reports converged on "the loop fires
onFinish reading a stale clock before the video seek completes." The
startPaused option in GameLoop is the single root-cause fix; the rest
is wiring + a defensive `seekToVideoTime(0)` so cursor/notes/score are
clean before any ricochets land.

**Revisit when:** We add a checkpoint / "skip to chorus" feature that
also fires a seek. The same startPaused path should cover it.

## 2026-05-12: Demucs activated + per-stem onset cache

**Question:** Demucs has been scaffolded for a while behind `USE_DEMUCS`.
Once we flip it on for real, two things have to be solved that the
scaffold left open: (a) what happens when the same song is regenerated
at multiple difficulties (Demucs cost is dominant — minutes on CPU),
and (b) what to install / configure to actually use it.

**Choice:**
- Install path: `pip install -e ".[demucs]"` brings in demucs 4.0.1 +
  htdemucs weights (~80MB, auto-downloaded on first inference). Now
  documented as one of three supported modes in DEVELOPMENT.md
  (Baseline / ML-light / ML-full).
- **Per-stem onset cache** at `app/ml/onset_cache.py`: caches the
  per-stem Onset list (the output of Demucs + onset detection) by
  audio content hash. Difficulty selectivity runs downstream of onset
  detection so the cache hit is difficulty-independent — easy /
  normal / hard / expert on the same song all pay Demucs once.
- Cache layout mirrors `beat_cache.py`: small JSON file per audio
  under `<CACHE_DIR>/onsets/`, versioned, silent on read/write
  errors so a corrupt cache never breaks chart generation.

**Why:** without the cache, a player who replays a song at a higher
difficulty waits 2-4 minutes for Demucs to run again on the exact
same audio, producing identical stems and identical onsets. That
break in the chart-cache-hits-are-instant guarantee would be a
worse UX than just shipping the heuristic pipeline.

**Revisit when:** GPU deploy lands (Modal / Replicate) — Demucs is
~3x realtime on GPU vs 0.25x on CPU, which might let us drop the
per-stem cache in favour of always-recompute. Probably not worth it
even then; the cache is tiny.

## 2026-05-12: Beat This! downbeat sanity filter

**Question:** Beat This! 1.1.0 sometimes returns a downbeat for
nearly every beat on out-of-distribution audio (regular click tracks,
drones, very repetitive synth music). On a 21-beat synth click track
during smoke testing, Beat This! reported 18 downbeats. Our
downbeat-accent rule fires a chord on every onset near a downbeat,
so the resulting chart had a 100% chord rate.

**Choice:** Added `_sanity_filter_downbeats` to `beat_track.py`. When
the downbeats-to-beats ratio exceeds `_MAX_PLAUSIBLE_DOWNBEAT_RATIO`
(0.55), the downbeat list is discarded and the lane assigner falls
back to the strength+centroid accent gate.

**Why:** 0.55 is above every legitimate time signature (4/4 = 0.25,
3/4 = 0.33, 2/4 = 0.50, 6/8 = 0.17). A ratio above means the model
is fooling itself, and trusting it would produce charts that are
gameplay-broken. The fallback is the gate that shipped before
downbeats existed; it produces playable charts with a ~7% chord
rate, which is the right behaviour.

**Revisit when:** Beat This! ships a more robust downbeat head, or
when we add a different out-of-distribution detector (e.g. low
inference confidence).

## 2026-05-12: ML Phase 1 (Beat This!) and Phase 2 (per-stem onsets) implemented

**Question:** Which ML modules from docs/ML_PLAN.md land first, and how
do they integrate with the heuristic pipeline that already ships?

**Choice:**

- **Beat This!** detector wired into `app/pipeline/beat_track.py` behind
  a `USE_BEAT_THIS=1` env flag. Returns beats AND downbeats AND a derived
  piecewise bpm curve. Falls back to librosa on any failure (package
  missing, inference error, no beats found). Beat-tracker output is
  cached on disk per audio content hash so re-runs at different
  difficulties pay zero ML cost.
- **Downbeat-aware accent chords.** When the Beat This! path produces
  downbeats, the lane assigner emits chord stacks on real bar starts
  rather than guessing accents from strength quantile + centroid. The
  strength+centroid gate stays in for the librosa fallback path and for
  accents that happen between bars.
- **Per-stem onset detection.** `detect_onsets_per_stem` runs energy-
  envelope onset detection on the drum stem and vocal stem separately
  and tags each Onset with its source. The lane assigner routes drums
  to the LOW band (lanes 0/1) and vocals to the HIGH band (lanes 2/3),
  overriding the centroid split when a stem label is present. Activated
  by `USE_DEMUCS=1`; falls back to full-mix detection on the same audio
  when Demucs isn't installed.
- **bpmCurve in the Chart JSON.** Always populated when there are
  enough beats; consumed by no game logic today but exposed in the
  wire contract so the HUD can read tempo shifts later without a
  schema change.

**Why:**
- ML_PLAN's "honest reframe" research called Beat This! the single
  biggest "hand-mapped feel" jump available because every downstream
  decision (snap-to-beat, subdivisions, density bucketing, chord
  emission) compounds on beat-grid quality.
- The plan calls for each phase to be flag-gated with a heuristic
  rollback. Both new paths follow the existing Demucs scaffold pattern:
  try ML, log on failure, return the heuristic result.
- Per-stem onset detection is the unlock that makes "drums route to
  lanes 0/1, vocals route to lanes 2/3" deterministic instead of
  relying on the centroid being a clean proxy for instrument. On songs
  where the vocal sits below 2 kHz this is the difference between a
  chart that follows the melody and one that doesn't.

**Test coverage:** `tests/test_ml_phases.py` asserts the fallback paths
work without the ML packages installed, the cache round-trips, the
downbeat chord rule fires regardless of centroid, and stem labels route
to the expected band. Existing 53 tests still pass.

**Revisit when:** Phase 3 (MERT section detection) is implemented, or
when playtesting shows the strength/centroid accent gate is still firing
unwanted chords on librosa-fallback songs (would suggest tightening
CHORD_STRENGTH_QUANTILE per-difficulty further).


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

## 2026-05-11: Beat-aware hold duration with grid snap

**Question:** What's a "reasonable" hold length?

**Choice:** Bound by song tempo, not a fixed seconds value. Default
MAX_HOLD_BEATS=2.0 (so at 172 BPM, max hold = ~0.7s; at 60 BPM, max =
2.0s clamped by the absolute MAX_HOLD_S). Floor at MIN_HOLD_BEATS=0.5
(or MIN_HOLD_S=0.20s, whichever wins). Hold ends snap to the nearest
half-beat in the chart's beat grid so tails land on musical positions.

**Why:** an early playtest had holds running 6+ beats long because we
used a fixed 2.0s cap that didn't scale with tempo. A hold should feel
like a held note (max half a bar in most pop styles); longer is just
visual clutter. Snap-to-beat makes the release feel deliberate.

**Revisit when:** charts include hands or genres where 4-beat held
notes are normal (slow ballad sustains, ambient sections). Could
expose a per-song "MAX_HOLD_BEATS" override in the chart metadata.

## 2026-05-11: Stray-press penalty (combo break on key spam)

**Question:** Should pressing keys away from any note do anything?

**Choice:** Yes. When `registerPress` returns null AND no note exists
within 250ms in the same lane, count it as a miss (combo break + miss
tally). A press near a note but mistimed (within 250ms but outside hit
windows) is not punished; the note will time out normally if not hit.

**Why:** the user reported being able to spam keys without consequence.
osu!mania ignores stray presses, but for a casual rhythm-game extension
the lack of punishment lets players "smash to win" without engaging.
The 250ms threshold separates clear spam (no note in sight) from a
late hit attempt that just missed the meh window (which is 127ms at OD 8).

**Revisit when:** a player complains about combo breaks from finger
tremor on dense streams. Could reduce 250ms or expose as a setting.

## 2026-05-11: Hold rate cap + RMS sustain detection

**Question:** Which onsets become holds?

**Choice:** Walk RMS forward from each tap; measure how long energy
stays above 75% of the onset's peak. Sort all candidates by duration
and promote only the top 5% (`MAX_HOLD_RATIO=0.05`). No baseline
comparison.

**Why:** an earlier baseline-vs-peak filter ("only promote when the
onset clearly sticks out from surrounding RMS") produced 0 holds on
continuous pop music because the entire song's RMS tracks the onset's
peak. The rate cap alone gives the desired 5% density without that
pathology, and on a 780-note song produces clean holds averaging
1.5-2 beats long.

**Revisit when:** stem separation lands. With clean drum / vocal
stems, a sustain check on the vocal stem would actually mean
something (vocals visibly hold; drums don't).

## 2026-05-11: Per-section density curve (chorus vs verse)

**Question:** Should chart density change across a song?

**Choice:** Yes. chart_builder samples a 4-second RMS curve, classifies
each note into low (bottom 33%) / mid / high (top 33%) energy
buckets, and passes the bucket array to shape_difficulty. The thinner
scales its local min_gap by the bucket multiplier
(`_BUCKET_DENSITY_MULT = (0.75, 1.0, 1.30)`).

**Why:** uniform density meant verses felt cluttered and choruses
felt sparse relative to the music. The bucket approach is rule-based
(no ML, no audio features beyond RMS) so it's deterministic and
debuggable, but still tracks the song's actual dynamics.

**Revisit when:** stem separation makes a per-stem energy curve
available. Drum energy is a better "chorus" proxy than full-mix RMS,
since vocal-driven choruses without big drums get missed today.

## 2026-05-11: Mirror-pair lane palette (osu!mania 4K convention)

**Question:** Should lanes have 4 unique colors or be visually paired?

**Choice:** Mirror pair. Outer lanes (D, K) share cool cyan; inner
lanes (F, J) share warm gold.

**Why:** during fast streams, four-unique-color palettes force the
player to read each lane's color independently, which is cognitive
load that doesn't help play. The mirror palette groups outer vs
inner so the visual chunk matches finger placement (outer thumbs
vs inner fingers, in a manner of speaking). osu!mania 4K default
skin uses this convention; Beatstar (3 lanes) and Fortnite Festival
(5 lanes) use related inner/outer grouping.

## 2026-05-11: Demucs stem separation (scaffold, off by default)

**Question:** Do we run source separation as part of every chart?

**Choice:** No by default. New `app/pipeline/stems.py` exposes
`separate_stems(y, sr, use_demucs)` which returns drums/vocals/bass/
other when `USE_DEMUCS=1` is set AND the `demucs` package is
importable, otherwise returns a pass-through Stems where each "stem"
is the original mix. When real, `chart_builder` uses the drum stem
for onset detection.

**Why:** the user explicitly asked about the system's understanding
of vocal vs bass vs drum. Without stems the answer is "none, it's
all spectral centroid on the mix." Demucs gives real separation
(deep-learning model) but costs 1-4x realtime on CPU and downloads
~300MB on first use, so it can't be default-on. The scaffold lets
power users flip a flag without us redesigning the pipeline.

**Revisit when:** a GPU deploy target lands (Modal, Replicate) and
the latency budget allows running Demucs on every cold-start.

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
