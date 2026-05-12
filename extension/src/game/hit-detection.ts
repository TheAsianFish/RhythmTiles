// Hit detection: take a press event in game-time and find the nearest unhit
// note in that lane. Bucket the delta into a Judgment using osu!mania-style
// hit windows. See types.hitWindowsForOD for the formula.

import {
  DEFAULT_HIT_WINDOWS,
  type HitResult,
  type HitWindowsMs,
  type Judgment,
  type NoteRuntime,
} from "./types";

// Bucket a delta (ms) into one of the six judgment tiers.
export function judge(deltaMs: number, windows: HitWindowsMs = DEFAULT_HIT_WINDOWS): Judgment {
  const a = Math.abs(deltaMs);
  if (a <= windows.max) return "max";
  if (a <= windows.great) return "great";
  if (a <= windows.good) return "good";
  if (a <= windows.ok) return "ok";
  if (a <= windows.meh) return "meh";
  return "miss";
}

// Base point values awarded per judgment, BEFORE combo multiplication.
// applied as: awarded * combo (osu!-style). See scoring.applyHit.
export function scoreForJudgment(j: Judgment): number {
  switch (j) {
    case "max":
      return 300;
    case "great":
      return 300;
    case "good":
      return 200;
    case "ok":
      return 100;
    case "meh":
      return 50;
    case "miss":
      return 0;
  }
}

// Find the nearest unhit note in `lane` whose start time is within
// `searchWindowMs` of `pressGameMs`. Linear scan from the provided `cursor`.
export function findCandidateNote(
  notes: NoteRuntime[],
  lane: number,
  pressGameMs: number,
  cursor: number,
  searchWindowMs: number = DEFAULT_HIT_WINDOWS.meh,
): { idx: number; deltaMs: number } | null {
  let best: { idx: number; deltaMs: number } | null = null;
  for (let i = cursor; i < notes.length; i++) {
    const n = notes[i]!;
    const delta = pressGameMs - n.startMs;
    if (delta > searchWindowMs && n.startMs < pressGameMs - searchWindowMs) {
      continue;
    }
    if (n.startMs - pressGameMs > searchWindowMs) {
      break;
    }
    if (n.hit || n.missed) continue;
    if (n.note.lane !== lane) continue;
    if (!best || Math.abs(delta) < Math.abs(best.deltaMs)) {
      best = { idx: i, deltaMs: delta };
    }
  }
  return best;
}

// Mark notes as missed once their start time is past the OK boundary plus
// some slack. Returns the new cursor position.
export function advanceCursor(
  notes: NoteRuntime[],
  cursor: number,
  gameMs: number,
  missWindowMs: number = DEFAULT_HIT_WINDOWS.meh,
): number {
  let c = cursor;
  while (c < notes.length) {
    const n = notes[c]!;
    if (n.hit || n.missed) {
      c++;
      continue;
    }
    if (n.startMs < gameMs - missWindowMs) {
      n.missed = true;
      n.judgment = "miss";
      c++;
      continue;
    }
    break;
  }
  return c;
}

export interface RegisterPressInput {
  pressGameMs: number;
  lane: number;
  notes: NoteRuntime[];
  cursor: number;
  combo: number;
  windows?: HitWindowsMs;
}

export function registerPress(input: RegisterPressInput): HitResult | null {
  const windows = input.windows ?? DEFAULT_HIT_WINDOWS;
  const cand = findCandidateNote(
    input.notes,
    input.lane,
    input.pressGameMs,
    input.cursor,
    windows.meh,
  );
  if (!cand) return null;
  const j: Judgment = judge(cand.deltaMs, windows);
  if (j === "miss") {
    // Outside the meh window: don't lock to the note. The note remains alive
    // and will be marked missed when it falls past the cursor.
    return null;
  }
  const note = input.notes[cand.idx]!;
  note.hit = true;
  note.judgment = j;
  const combo = input.combo + 1;
  return {
    judgment: j,
    deltaMs: cand.deltaMs,
    noteIndex: cand.idx,
    combo,
    scoreAwarded: scoreForJudgment(j),
  };
}
