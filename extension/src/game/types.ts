import type { Note } from "@/types/chart";

// Six-tier judgment system, osu!mania-style.
// Each tier has a base score (see hit-detection.scoreForJudgment) and a hit
// window derived from the OD (Overall Difficulty) value (see hitWindowsForOD).
export type Judgment = "max" | "great" | "good" | "ok" | "meh" | "miss";

export const ALL_JUDGMENTS: readonly Judgment[] = [
  "max",
  "great",
  "good",
  "ok",
  "meh",
  "miss",
] as const;

// Hit window in absolute milliseconds (|deltaMs| <= window).
// `miss` is the OUTER boundary: presses outside this are not even registered
// as a miss-on-press; the note will eventually time out and be marked missed
// when it leaves the active window.
export interface HitWindowsMs {
  max: number;
  great: number;
  good: number;
  ok: number;
  meh: number;
  miss: number;
}

// Compute hit windows for a given Overall Difficulty (OD).
//
// osu!mania v1 formula (ms):
//   max:   16.5 (fixed; the "rainbow 300" / MAX tier)
//   great: 64 - 3 * OD
//   good:  97 - 3 * OD
//   ok:   127 - 3 * OD
//   meh:  151 - 3 * OD
//   miss boundary: 188 - 3 * OD
//
// Default OD = 8 is roughly "challenging" (great window ~40ms).
export const DEFAULT_OD = 8;
export const MAX_WINDOW_MS = 16.5;

export function hitWindowsForOD(od: number): HitWindowsMs {
  const k = Math.max(0, od);
  return {
    max: MAX_WINDOW_MS,
    great: 64 - 3 * k,
    good: 97 - 3 * k,
    ok: 127 - 3 * k,
    meh: 151 - 3 * k,
    miss: 188 - 3 * k,
  };
}

export const DEFAULT_HIT_WINDOWS: HitWindowsMs = hitWindowsForOD(DEFAULT_OD);

export interface HitResult {
  judgment: Judgment;
  deltaMs: number;            // signed: positive = late, negative = early
  noteIndex: number;          // index into the chart.notes array
  combo: number;              // combo after this hit
  scoreAwarded: number;       // base points (pre-combo multiplication)
}

export interface ScoreState {
  score: number;
  combo: number;
  maxCombo: number;
  multiplier: number;
  hitCounts: Record<Judgment, number>;
  notesProcessed: number;
}

export interface ActiveHold {
  noteIndex: number;
  lane: number;
  pressStartMs: number;       // when the player pressed down, in game-time ms
  expectedReleaseMs: number;  // chart's release time, in game-time ms
}

// A snapshot of a single note as the game tracks it during play.
//
// Future: chord support. The chart contract already allows multiple notes at
// the same `t` value (since notes is a flat array indexed by appearance).
// When chord notes are added, the lane assigner will emit N notes with the
// same `t` and different `lane` values; the input + hit detection already
// scope by lane so the existing logic handles multi-press naturally. The
// only gap is visual highlighting that a column-spanning chord is incoming;
// see canvas-renderer for where to add a chord-link decoration.
export interface NoteRuntime {
  index: number;
  note: Note;
  startMs: number;     // note.t * 1000
  endMs: number;       // start + duration for holds, else start
  // Final states: hit (success) and missed (failure). Mutually exclusive
  // and set once. Notes in either state are skipped by the cursor + renderer.
  hit: boolean;
  missed: boolean;
  // Transient state for holds only: true after the head was pressed and
  // before the tail is released. The renderer keeps drawing the body while
  // this is true; the loop resolves it to hit or missed on release / timeout.
  holding: boolean;
  // gameMs at which the player landed a successful hit on this note. Used
  // by the renderer to fade out a ghost at the hit line instead of letting
  // the note pop out of existence. Undefined until hit (success), and stays
  // undefined for misses since there's nothing to fade.
  hitAtMs?: number;
  judgment?: Judgment;
}

export function noteRuntimes(notes: Note[]): NoteRuntime[] {
  return notes.map((n, i) => {
    const startMs = n.t * 1000;
    const endMs = n.type === "hold" ? startMs + (n.duration ?? 0) * 1000 : startMs;
    return { index: i, note: n, startMs, endMs, hit: false, missed: false, holding: false };
  });
}
