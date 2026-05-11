import type { Note } from "@/types/chart";

export type Judgment = "perfect" | "good" | "ok" | "miss";

export interface HitWindowsMs {
  perfect: number;
  good: number;
  ok: number;
}

export const DEFAULT_HIT_WINDOWS: HitWindowsMs = {
  perfect: 25,
  good: 50,
  ok: 100,
};

export interface HitResult {
  judgment: Judgment;
  deltaMs: number;            // signed: positive = late, negative = early
  noteIndex: number;          // index into the chart.notes array
  combo: number;              // combo after this hit
  scoreAwarded: number;       // points before multiplier
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
export interface NoteRuntime {
  index: number;
  note: Note;
  startMs: number;     // note.t * 1000
  endMs: number;       // start + duration for holds, else start
  hit: boolean;
  missed: boolean;
  judgment?: Judgment;
}

export function noteRuntimes(notes: Note[]): NoteRuntime[] {
  return notes.map((n, i) => {
    const startMs = n.t * 1000;
    const endMs = n.type === "hold" ? startMs + (n.duration ?? 0) * 1000 : startMs;
    return { index: i, note: n, startMs, endMs, hit: false, missed: false };
  });
}
