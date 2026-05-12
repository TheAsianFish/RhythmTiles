import { describe, expect, it } from "vitest";
import {
  accuracyPercent,
  applyHit,
  applyMiss,
  emptyScoreState,
  multiplierFor,
} from "@/game/scoring";

describe("multiplierFor", () => {
  it("returns combo directly (osu-style scoring)", () => {
    expect(multiplierFor(0)).toBe(1);
    expect(multiplierFor(1)).toBe(1);
    expect(multiplierFor(25)).toBe(25);
    expect(multiplierFor(100)).toBe(100);
    expect(multiplierFor(1000)).toBe(1000);
  });
});

describe("applyHit", () => {
  it("first hit pays base score (combo=1 multiplier)", () => {
    let s = emptyScoreState();
    s = applyHit(s, {
      judgment: "perfect",
      deltaMs: 0,
      noteIndex: 0,
      combo: 1,
      scoreAwarded: 300,
    });
    expect(s.score).toBe(300);
    expect(s.combo).toBe(1);
    expect(s.multiplier).toBe(1);
  });

  it("score grows linearly with combo", () => {
    let s = emptyScoreState();
    // simulate 5 hits with increasing combo
    let expected = 0;
    for (let combo = 1; combo <= 5; combo++) {
      s = applyHit(s, {
        judgment: "perfect",
        deltaMs: 0,
        noteIndex: combo - 1,
        combo,
        scoreAwarded: 300,
      });
      expected += 300 * combo;
    }
    // 300*1 + 300*2 + 300*3 + 300*4 + 300*5 = 300*(1+2+3+4+5) = 4500
    expect(s.score).toBe(expected);
    expect(s.score).toBe(4500);
    expect(s.maxCombo).toBe(5);
  });

  it("good notes pay 100 base", () => {
    let s = emptyScoreState();
    s = applyHit(s, {
      judgment: "good",
      deltaMs: 30,
      noteIndex: 0,
      combo: 1,
      scoreAwarded: 100,
    });
    expect(s.score).toBe(100);
  });

  it("tracks max combo across miss drops", () => {
    let s = emptyScoreState();
    for (let i = 1; i <= 5; i++) {
      s = applyHit(s, {
        judgment: "good",
        deltaMs: 30,
        noteIndex: i,
        combo: i,
        scoreAwarded: 100,
      });
    }
    expect(s.maxCombo).toBe(5);
    s = applyMiss(s);
    expect(s.combo).toBe(0);
    expect(s.maxCombo).toBe(5);
    expect(s.multiplier).toBe(1);
  });
});

describe("accuracyPercent", () => {
  it("returns 100 when no notes processed", () => {
    expect(accuracyPercent(emptyScoreState())).toBe(100);
  });

  it("weights judgments correctly", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "perfect", deltaMs: 0, noteIndex: 0, combo: 1, scoreAwarded: 300 });
    s = applyHit(s, { judgment: "good", deltaMs: 30, noteIndex: 1, combo: 2, scoreAwarded: 100 });
    s = applyMiss(s);
    // weights: 1.0 + 0.66 + 0 = 1.66 over 3 = 55.33%
    expect(accuracyPercent(s)).toBeCloseTo((1 + 0.66) / 3 * 100, 1);
  });
});
