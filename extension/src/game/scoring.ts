// Scoring + combo + accuracy.
//
// Score model: each non-miss hit awards base points (300/100/50) multiplied by
// the player's combo at the time of the hit. Combo increments on every
// non-miss hit. A miss resets the combo to 0, which "resets" the multiplier.
// Accuracy weights: Perfect=1.0, Good=0.66, OK=0.33, Miss=0.0.

import type { HitResult, Judgment, ScoreState } from "./types";

export function emptyScoreState(): ScoreState {
  return {
    score: 0,
    combo: 0,
    maxCombo: 0,
    multiplier: 1.0,
    hitCounts: { perfect: 0, good: 0, ok: 0, miss: 0 },
    notesProcessed: 0,
  };
}

const ACC_WEIGHT: Record<Judgment, number> = {
  perfect: 1.0,
  good: 0.66,
  ok: 0.33,
  miss: 0.0,
};

// Combo IS the multiplier in the new model. Exposed for tests and HUD code.
export function multiplierFor(combo: number): number {
  return Math.max(1, combo);
}

export function applyHit(state: ScoreState, hit: HitResult): ScoreState {
  const combo = hit.combo;
  const multiplier = multiplierFor(combo);
  // Award base points scaled by the combo at this hit. First hit (combo=1)
  // pays base; combo=50 pays 50x base; combo=1000 pays 1000x base.
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
    state.hitCounts.perfect +
    state.hitCounts.good +
    state.hitCounts.ok +
    state.hitCounts.miss;
  if (total === 0) return 100;
  const weighted =
    state.hitCounts.perfect * ACC_WEIGHT.perfect +
    state.hitCounts.good * ACC_WEIGHT.good +
    state.hitCounts.ok * ACC_WEIGHT.ok;
  return (weighted / total) * 100;
}
