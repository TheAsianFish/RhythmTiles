// Hit detection: take a press event in game-time and find the nearest unhit
// note in that lane. Bucket the delta into a Judgment.

import {
  DEFAULT_HIT_WINDOWS,
  type HitResult,
  type HitWindowsMs,
  type Judgment,
  type NoteRuntime,
} from "./types";

export function judge(deltaMs: number, windows: HitWindowsMs = DEFAULT_HIT_WINDOWS): Judgment {
  const a = Math.abs(deltaMs);
  if (a <= windows.perfect) return "perfect";
  if (a <= windows.good) return "good";
  if (a <= windows.ok) return "ok";
  return "miss";
}

export function scoreForJudgment(j: Judgment): number {
  switch (j) {
    case "perfect":
      return 300;
    case "good":
      return 150;
    case "ok":
      return 50;
    case "miss":
      return 0;
  }
}

// Find the nearest unhit note in `lane` whose start time is within `searchWindowMs`
// of `pressGameMs`. Linear scan from the provided `cursor` is fine for the note
// counts we deal with (under ~5 per second per lane).
export function findCandidateNote(
  notes: NoteRuntime[],
  lane: number,
  pressGameMs: number,
  cursor: number,
  searchWindowMs: number = DEFAULT_HIT_WINDOWS.ok,
): { idx: number; deltaMs: number } | null {
  let best: { idx: number; deltaMs: number } | null = null;
  for (let i = cursor; i < notes.length; i++) {
    const n = notes[i]!;
    const delta = pressGameMs - n.startMs;
    if (delta > searchWindowMs && n.startMs < pressGameMs - searchWindowMs) {
      // Note is too far in the past from this press; skip but keep walking
      // in case the cursor lagged.
      continue;
    }
    if (n.startMs - pressGameMs > searchWindowMs) {
      // We've walked past the search window; nothing further can match.
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

// Update the cursor so future searches don't walk past notes already resolved
// or far in the past. Returns the new cursor.
export function advanceCursor(
  notes: NoteRuntime[],
  cursor: number,
  gameMs: number,
  missWindowMs: number = DEFAULT_HIT_WINDOWS.ok,
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
    windows.ok,
  );
  if (!cand) return null;
  const j: Judgment = judge(cand.deltaMs, windows);
  if (j === "miss") {
    return null; // outside ok window; ignore as a misfire press
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
