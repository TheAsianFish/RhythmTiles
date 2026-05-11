// The game loop. Wires clock + input + hit detection + scoring + rendering.
// Lives in /game so it stays UI-independent (the renderer is injected).

import type { Chart } from "@/types/chart";
import { advanceCursor, registerPress, scoreForJudgment } from "./hit-detection";
import { InputCapture, type KeyBindings } from "./input";
import { applyHit, applyMiss, accuracyPercent, emptyScoreState, multiplierFor } from "./scoring";
import {
  DEFAULT_HIT_WINDOWS,
  type ActiveHold,
  type HitResult,
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
}

export interface GameLoopOptions {
  chart: Chart;
  clock: ClockSource;
  bindings: KeyBindings;
  audioLatencyOffsetMs: number;
  callbacks: LoopCallbacks;
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

  constructor(opts: GameLoopOptions) {
    this.chart = opts.chart;
    this.notes = noteRuntimes(opts.chart.notes);
    this.clock = opts.clock;
    this.offsetMs = opts.audioLatencyOffsetMs;
    this.capture = new InputCapture(opts.bindings);
    this.callbacks = opts.callbacks;
  }

  setBindings(b: KeyBindings) {
    this.capture.setBindings(b);
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
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    if (this.unsubscribeInput) {
      this.unsubscribeInput();
      this.unsubscribeInput = null;
    }
    this.capture.detach(target);
  }

  private handleInput(ev: { lane: number; kind: "press" | "release"; perfMs: number }) {
    const nowGameMs = gameTimeMs(this.clock, this.offsetMs);
    if (ev.kind === "press") {
      this.pressedLanes.add(ev.lane);
      const result = registerPress({
        pressGameMs: nowGameMs,
        lane: ev.lane,
        notes: this.notes,
        cursor: this.cursor,
        combo: this.score.combo,
      });
      if (result) {
        this.applyResult(result, nowGameMs);
        const note = this.notes[result.noteIndex]!;
        if (note.note.type === "hold") {
          this.activeHolds.set(ev.lane, {
            noteIndex: result.noteIndex,
            lane: ev.lane,
            pressStartMs: nowGameMs,
            expectedReleaseMs: note.endMs,
          });
        }
      }
    } else {
      this.pressedLanes.delete(ev.lane);
      const hold = this.activeHolds.get(ev.lane);
      if (hold) {
        this.activeHolds.delete(ev.lane);
        const releaseDelta = nowGameMs - hold.expectedReleaseMs;
        // Re-use the same hit windows for hold-release accuracy.
        const judgment = judgeAbs(Math.abs(releaseDelta));
        if (judgment !== "miss") {
          // Treat as bonus award scaled like a tap.
          this.score = applyHit(this.score, {
            judgment,
            deltaMs: releaseDelta,
            noteIndex: hold.noteIndex,
            combo: this.score.combo + 1,
            scoreAwarded: Math.floor(scoreForJudgment(judgment) / 2),
          });
          this.lastJudgment = { judgment, deltaMs: releaseDelta, atMs: nowGameMs };
        }
      }
    }
  }

  private applyResult(result: HitResult, gameMs: number) {
    this.score = applyHit(this.score, result);
    this.lastJudgment = { judgment: result.judgment, deltaMs: result.deltaMs, atMs: gameMs };
  }

  private tick = () => {
    if (!this.running) return;
    const gameMs = gameTimeMs(this.clock, this.offsetMs);

    // Advance cursor; mark past-window notes as missed.
    this.cursor = advanceCursor(this.notes, this.cursor, gameMs);
    // Fold any newly-missed notes into the score state. Bounded by the cursor;
    // notes ahead of the cursor cannot yet be missed.
    for (let i = this.missAccountedUpTo; i < this.cursor; i++) {
      const n = this.notes[i]!;
      if (n.missed) {
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

function judgeAbs(absMs: number): Judgment {
  if (absMs <= DEFAULT_HIT_WINDOWS.perfect) return "perfect";
  if (absMs <= DEFAULT_HIT_WINDOWS.good) return "good";
  if (absMs <= DEFAULT_HIT_WINDOWS.ok) return "ok";
  return "miss";
}
