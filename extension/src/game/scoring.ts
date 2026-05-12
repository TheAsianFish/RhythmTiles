// Scoring + combo + accuracy.
//
// Score model: base points (300/300/200/100/50) multiplied by the combo at
// the time of the hit (osu!-style). Combo increments on every non-miss.
// A miss resets combo to 0 which resets the effective multiplier.
//
// Accuracy weights are proportional to base / 300 (max/great = 1.0, good =
// 0.667, ok = 0.333, meh = 0.167, miss = 0).

import type { HitResult, Judgment, ScoreState } from "./types";

export function emptyScoreState(): ScoreState {
  return {
    score: 0,
    combo: 0,
    maxCombo: 0,
    multiplier: 1.0,
    hitCounts: { max: 0, great: 0, good: 0, ok: 0, meh: 0, miss: 0 },
    notesProcessed: 0,
  };
}

const ACC_WEIGHT: Record<Judgment, number> = {
  max: 1.0,
  great: 1.0,
  good: 200 / 300,
  ok: 100 / 300,
  meh: 50 / 300,
  miss: 0.0,
};

// Combo IS the multiplier. Floor of 1 so combo=0 still pays base.
export function multiplierFor(combo: number): number {
  return Math.max(1, combo);
}

export function applyHit(state: ScoreState, hit: HitResult): ScoreState {
  const combo = hit.combo;
  const multiplier = multiplierFor(combo);
  const score = state.score + Math.round(hit.scoreAwarded * multiplier);
  return {
    score,
    combo,
    maxCombo: Math.max(state.maxCombo, combo),
    multiplier,
    hitCounts: { ...state.hitCounts, [hit.judgment]: state.hitCounts[hit.judgment] + 1 },
    notesProcessed: state.notesProcessed + 1,
  };
}

export function applyMiss(state: ScoreState): ScoreState {
  return {
    score: state.score,
    combo: 0,
    maxCombo: state.maxCombo,
    multiplier: 1.0,
    hitCounts: { ...state.hitCounts, miss: state.hitCounts.miss + 1 },
    notesProcessed: state.notesProcessed + 1,
  };
}

export function accuracyPercent(state: ScoreState): number {
  const total =
    state.hitCounts.max +
    state.hitCounts.great +
    state.hitCounts.good +
    state.hitCounts.ok +
    state.hitCounts.meh +
    state.hitCounts.miss;
  if (total === 0) return 100;
  const weighted =
    state.hitCounts.max * ACC_WEIGHT.max +
    state.hitCounts.great * ACC_WEIGHT.great +
    state.hitCounts.good * ACC_WEIGHT.good +
    state.hitCounts.ok * ACC_WEIGHT.ok +
    state.hitCounts.meh * ACC_WEIGHT.meh;
  return (weighted / total) * 100;
}
