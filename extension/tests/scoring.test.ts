import { describe, expect, it } from "vitest";
import {
  accuracyPercent,
  applyHit,
  applyMiss,
  emptyScoreState,
  multiplierFor,
} from "@/game/scoring";

describe("multiplierFor", () => {
  it("caps at 4x", () => {
    expect(multiplierFor(0)).toBe(1);
    expect(multiplierFor(24)).toBe(1);
    expect(multiplierFor(25)).toBeCloseTo(1.1);
    expect(multiplierFor(100)).toBeCloseTo(1.4);
    expect(multiplierFor(1000)).toBe(4);
  });
});

describe("applyHit", () => {
  it("accumulates score using current multiplier (not the new one)", () => {
    let s = emptyScoreState();
    // First hit: multiplier still 1.0, score gets +300.
    s = applyHit(s, {
      judgment: "perfect",
      deltaMs: 0,
      noteIndex: 0,
      combo: 1,
      scoreAwarded: 300,
    });
    expect(s.score).toBe(300);
    expect(s.combo).toBe(1);
    expect(s.maxCombo).toBe(1);
    expect(s.hitCounts.perfect).toBe(1);
  });

  it("tracks max combo across drops", () => {
    let s = emptyScoreState();
    for (let i = 1; i <= 5; i++) {
      s = applyHit(s, {
        judgment: "good",
        deltaMs: 30,
        noteIndex: i,
        combo: i,
        scoreAwarded: 150,
      });
    }
    expect(s.maxCombo).toBe(5);
    s = applyMiss(s);
    expect(s.combo).toBe(0);
    expect(s.maxCombo).toBe(5);
  });
});

describe("accuracyPercent", () => {
  it("returns 100 when no notes processed", () => {
    expect(accuracyPercent(emptyScoreState())).toBe(100);
  });

  it("weights judgments correctly", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "perfect", deltaMs: 0, noteIndex: 0, combo: 1, scoreAwarded: 300 });
    s = applyHit(s, { judgment: "good", deltaMs: 30, noteIndex: 1, combo: 2, scoreAwarded: 150 });
    s = applyMiss(s);
    // weights: 1.0 + 0.66 + 0 = 1.66 over 3 = 55.33%
    expect(accuracyPercent(s)).toBeCloseTo((1 + 0.66) / 3 * 100, 1);
  });
});
