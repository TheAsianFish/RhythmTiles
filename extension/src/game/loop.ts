// The game loop. Wires clock + input + hit detection + scoring + rendering.
// Lives in /game so it stays UI-independent (the renderer is injected).

import type { Chart } from "@/types/chart";
import { advanceCursor, findCandidateNote, registerPress, scoreForJudgment } from "./hit-detection";
import { InputCapture, type KeyBindings } from "./input";
import { applyHit, applyMiss, accuracyPercent, emptyScoreState, multiplierFor } from "./scoring";
import {
  DEFAULT_HIT_WINDOWS,
  DEFAULT_OD,
  hitWindowsForOD,
  type ActiveHold,
  type HitResult,
  type HitWindowsMs,
  type Judgment,
  noteRuntimes,
  type NoteRuntime,
  type ScoreState,
} from "./types";
import { type ClockSource, gameTimeMs } from "./clock";

export interface LoopCallbacks {
  onFrame: (frame: {
    gameMs: number;
    notes: NoteRuntime[];
    score: ScoreState;
    lastJudgment?: { judgment: Judgment; deltaMs: number; atMs: number };
    pressedLanes: ReadonlySet<number>;
  }) => void;
  onFinish: (final: { score: ScoreState; accuracyPercent: number }) => void;
  // Fires once per non-miss note press. Does NOT fire for misses or for
  // presses that don't land on a note (registerPress returns null in both
  // cases). Use this for SFX so key spam stays silent.
  onHit?: (info: { judgment: Judgment; deltaMs: number; lane: number }) => void;
}

export interface GameLoopOptions {
  chart: Chart;
  clock: ClockSource;
  bindings: KeyBindings;
  audioLatencyOffsetMs: number;
  callbacks: LoopCallbacks;
  // Overall Difficulty (osu!-style). Higher = tighter windows. Default 8.
  overallDifficulty?: number;
}

export class GameLoop {
  private chart: Chart;
  private notes: NoteRuntime[];
  private cursor = 0;
  private clock: ClockSource;
  private offsetMs: number;
  private capture: InputCapture;
  private score: ScoreState = emptyScoreState();
  private lastJudgment?: { judgment: Judgment; deltaMs: number; atMs: number };
  private rafId: number | null = null;
  private running = false;
  private pressedLanes = new Set<number>();
  private activeHolds = new Map<number, ActiveHold>();
  private callbacks: LoopCallbacks;
  private unsubscribeInput: (() => void) | null = null;
  // Index up to which we have already folded misses into the score state.
  // Each frame we only need to inspect notes between this value and the cursor.
  private missAccountedUpTo = 0;
  private windows: HitWindowsMs;
  private isPaused = false;

  constructor(opts: GameLoopOptions) {
    this.chart = opts.chart;
    this.notes = noteRuntimes(opts.chart.notes);
    this.clock = opts.clock;
    this.offsetMs = opts.audioLatencyOffsetMs;
    this.capture = new InputCapture(opts.bindings);
    this.callbacks = opts.callbacks;
    this.windows = hitWindowsForOD(opts.overallDifficulty ?? DEFAULT_OD);
  }

  setBindings(b: KeyBindings) {
    this.capture.setBindings(b);
  }

  // Forwarded keys from the content script (used when the parent YouTube page
  // has focus and the iframe wouldn't otherwise receive the event).
  injectKey(code: string, kind: "press" | "release", perfMs: number) {
    this.capture.injectKey(code, kind, perfMs);
  }

  setLatencyOffsetMs(ms: number) {
    this.offsetMs = ms;
  }

  start(target: Document | Window = window) {
    if (this.running) return;
    this.running = true;
    this.capture.attach(target);
    this.unsubscribeInput = this.capture.subscribe((ev) => this.handleInput(ev));
    this.tick();
  }

  stop(target: Document | Window = window) {
    this.running = false;
    this.isPaused = false;
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    if (this.unsubscribeInput) {
      this.unsubscribeInput();
      this.unsubscribeInput = null;
    }
    this.capture.detach(target);
  }

  pause() {
    if (!this.running || this.isPaused) return;
    this.isPaused = true;
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
  }

  resume() {
    if (!this.running || !this.isPaused) return;
    this.isPaused = false;
    this.tick();
  }

  // Convenience wrapper: takes the raw video currentTime in seconds and
  // applies the stored latency offset before calling seekTo.
  seekToVideoTime(videoTimeS: number) {
    this.seekTo(videoTimeS * 1000 + this.offsetMs);
  }

  // Seek the game to a new audio position without recomputing the chart.
  // Notes before newGameMs are silently skipped (no score penalty). Notes at or
  // after newGameMs are reset to unhit so they can be played normally.
  // Score resets to zero since the context from before the seek is gone.
  seekTo(newGameMs: number) {
    // Reset all note runtime state.
    for (const n of this.notes) {
      n.hit = false;
      n.missed = false;
      n.holding = false;
      n.judgment = undefined;
    }

    // Reset all transient game state.
    this.score = emptyScoreState();
    this.lastJudgment = undefined;
    this.activeHolds.clear();
    this.pressedLanes.clear();

    // Silently advance past notes that are now behind the seek point.
    // Mark them missed so the renderer skips them, but don't apply score.
    let c = 0;
    while (c < this.notes.length) {
      const n = this.notes[c]!;
      if (n.startMs < newGameMs - this.windows.meh) {
        n.missed = true;
        c++;
      } else {
        break;
      }
    }
    this.cursor = c;
    this.missAccountedUpTo = c;
  }

  private handleInput(ev: { lane: number; kind: "press" | "release"; perfMs: number }) {
    // Track press visuals even while paused so the renderer's last-drawn
    // frame reflects what the player physically did, but skip all scoring
    // and hold-tracking logic until the game resumes.
    if (this.isPaused) {
      if (ev.kind === "press") this.pressedLanes.add(ev.lane);
      else this.pressedLanes.delete(ev.lane);
      return;
    }
    const nowGameMs = gameTimeMs(this.clock, this.offsetMs);
    if (ev.kind === "press") {
      this.pressedLanes.add(ev.lane);
      const result = registerPress({
        pressGameMs: nowGameMs,
        lane: ev.lane,
        notes: this.notes,
        cursor: this.cursor,
        combo: this.score.combo,
        windows: this.windows,
      });
      if (result) {
        this.applyResult(result, nowGameMs);
        this.callbacks.onHit?.({
          judgment: result.judgment,
          deltaMs: result.deltaMs,
          lane: ev.lane,
        });
        const note = this.notes[result.noteIndex]!;
        if (note.note.type === "hold") {
          this.activeHolds.set(ev.lane, {
            noteIndex: result.noteIndex,
            lane: ev.lane,
            pressStartMs: nowGameMs,
            expectedReleaseMs: note.endMs,
          });
        }
      } else {
        this.handleStrayPress(ev.lane, nowGameMs);
      }
    } else {
      this.pressedLanes.delete(ev.lane);
      const hold = this.activeHolds.get(ev.lane);
      if (hold) {
        this.resolveHoldOnRelease(hold, nowGameMs);
      }
    }
  }

  // A press that didn't land on a note. If there's also no note within a
  // generous lookahead window in the same lane, this was just spam and the
  // combo breaks. If a note IS coming up (just outside the hit window) we
  // assume the player was trying and leave combo alone, since the note will
  // still time out as a regular miss later.
  private handleStrayPress(lane: number, pressGameMs: number) {
    const STRAY_LOOKAHEAD_MS = 250;
    const candidate = findCandidateNote(
      this.notes,
      lane,
      pressGameMs,
      this.cursor,
      STRAY_LOOKAHEAD_MS,
    );
    if (candidate) return; // a note exists nearby, don't punish a near-miss
    this.score = applyMiss(this.score);
    this.score.multiplier = multiplierFor(this.score.combo);
    this.lastJudgment = { judgment: "miss", deltaMs: 0, atMs: pressGameMs };
  }

  // The player released the key while still in an active hold. Two outcomes:
  //   - release happened AT or AFTER the tail (with a meh-window grace
  //     before the tail to be forgiving about a hair-early release) -> the
  //     hold completes successfully. The tick auto-completes any hold whose
  //     tail has passed, so this branch only fires when the player releases
  //     at exactly the tail time or in the tiny grace before it.
  //   - release happened BEFORE the tail by more than meh -> hold broken.
  //     The note becomes a miss; combo resets.
  // Release time NEVER deducts score or combo on its own past the duration;
  // this matches osu!mania LN release semantics for casual play.
  private resolveHoldOnRelease(hold: ActiveHold, nowGameMs: number) {
    this.activeHolds.delete(hold.lane);
    const note = this.notes[hold.noteIndex]!;
    if (note.hit || note.missed) return;
    const releaseDelta = nowGameMs - hold.expectedReleaseMs;
    if (releaseDelta >= -this.windows.meh) {
      // Held long enough. Complete the hold and pay the same half-base bonus
      // the old code used to. We always pay the bonus as a "great" because
      // release timing no longer determines tier.
      this.completeHold(hold, nowGameMs);
    } else {
      // Released too early. Hold broken.
      note.missed = true;
      note.holding = false;
      note.judgment = "miss";
      this.score = applyMiss(this.score);
      this.score.multiplier = multiplierFor(this.score.combo);
      this.lastJudgment = { judgment: "miss", deltaMs: releaseDelta, atMs: nowGameMs };
    }
  }

  // Mark an active hold as successfully completed: tail bonus, combo +1,
  // hit ghost, optional SFX. Used by both the auto-complete in tick() and
  // the on-time-release branch in resolveHoldOnRelease.
  private completeHold(hold: ActiveHold, gameMs: number): void {
    const note = this.notes[hold.noteIndex]!;
    if (note.hit || note.missed) return;
    note.hit = true;
    note.holding = false;
    note.hitAtMs = gameMs;
    // Tail completion always pays a "great" tier bonus. Release timing no
    // longer changes the tier; sustaining for the full duration is the win.
    this.score = applyHit(this.score, {
      judgment: "great",
      deltaMs: 0,
      noteIndex: hold.noteIndex,
      combo: this.score.combo + 1,
      scoreAwarded: Math.floor(scoreForJudgment("great") / 2),
    });
    this.callbacks.onHit?.({
      judgment: "great",
      deltaMs: 0,
      lane: hold.lane,
    });
    this.lastJudgment = { judgment: "great", deltaMs: 0, atMs: gameMs };
  }

  private applyResult(result: HitResult, gameMs: number) {
    this.score = applyHit(this.score, result);
    this.lastJudgment = { judgment: result.judgment, deltaMs: result.deltaMs, atMs: gameMs };
  }

  private tick = () => {
    if (!this.running) return;
    const gameMs = gameTimeMs(this.clock, this.offsetMs);

    // Auto-complete any active hold whose tail time has passed. The player
    // is allowed to keep holding past the duration; release timing past
    // expectedReleaseMs no longer penalizes. This is the casual osu!mania
    // LN convention: sustain for the duration = win.
    for (const [lane, hold] of this.activeHolds) {
      if (gameMs >= hold.expectedReleaseMs) {
        this.activeHolds.delete(lane);
        this.completeHold(hold, gameMs);
      }
    }

    // Advance cursor; mark past-window notes as missed.
    this.cursor = advanceCursor(this.notes, this.cursor, gameMs, this.windows.meh);
    // Fold any newly-missed notes into the score state. Bounded by the cursor;
    // notes ahead of the cursor cannot yet be missed.
    for (let i = this.missAccountedUpTo; i < this.cursor; i++) {
      const n = this.notes[i]!;
      if (n.missed && !n.holding) {
        this.score = applyMiss(this.score);
        this.score.multiplier = multiplierFor(this.score.combo);
        this.lastJudgment = { judgment: "miss", deltaMs: 0, atMs: gameMs };
      }
    }
    this.missAccountedUpTo = this.cursor;

    // Drive the renderer.
    this.callbacks.onFrame({
      gameMs,
      notes: this.notes,
      score: this.score,
      lastJudgment: this.lastJudgment,
      pressedLanes: this.pressedLanes,
    });

    // End-of-song check.
    const durationMs = this.chart.audio.duration * 1000;
    const allResolved = this.cursor >= this.notes.length;
    if (allResolved && gameMs >= (this.notes.at(-1)?.endMs ?? durationMs) + 250) {
      this.running = false;
      this.callbacks.onFinish({
        score: this.score,
        accuracyPercent: accuracyPercent(this.score),
      });
      return;
    }
    if (gameMs >= durationMs + 1000) {
      this.running = false;
      this.callbacks.onFinish({
        score: this.score,
        accuracyPercent: accuracyPercent(this.score),
      });
      return;
    }

    this.rafId = requestAnimationFrame(this.tick);
  };
}

// Suppress unused-import warning until we surface DEFAULT_HIT_WINDOWS in the
// HUD (e.g. a settings panel that displays "current OD => windows").
void DEFAULT_HIT_WINDOWS;
