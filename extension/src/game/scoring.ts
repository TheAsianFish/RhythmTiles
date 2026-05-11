// Scoring + combo + accuracy.
//
// Multiplier: starts at 1x, +0.1x per 25 combo, capped at 4x.
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

export function multiplierFor(combo: number): number {
  const tier = Math.floor(combo / 25);
  return Math.min(1 + tier * 0.1, 4.0);
}

export function applyHit(state: ScoreState, hit: HitResult): ScoreState {
  const combo = hit.combo;
  const multiplier = multiplierFor(combo);
  const score = state.score + Math.round(hit.scoreAwarded * state.multiplier);
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
