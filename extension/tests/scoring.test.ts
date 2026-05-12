import { describe, expect, it } from "vitest";
import {
  accuracyPercent,
  applyHit,
  applyMiss,
  emptyScoreState,
  multiplierFor,
} from "@/game/scoring";

describe("multiplierFor", () => {
  it("returns combo directly with a floor of 1", () => {
    expect(multiplierFor(0)).toBe(1);
    expect(multiplierFor(1)).toBe(1);
    expect(multiplierFor(50)).toBe(50);
    expect(multiplierFor(1000)).toBe(1000);
  });
});

describe("applyHit", () => {
  it("first hit (combo=1) pays base", () => {
    let s = emptyScoreState();
    s = applyHit(s, {
      judgment: "max",
      deltaMs: 0,
      noteIndex: 0,
      combo: 1,
      scoreAwarded: 300,
    });
    expect(s.score).toBe(300);
    expect(s.combo).toBe(1);
  });

  it("score grows linearly with combo for max hits", () => {
    let s = emptyScoreState();
    for (let c = 1; c <= 5; c++) {
      s = applyHit(s, {
        judgment: "max",
        deltaMs: 0,
        noteIndex: c - 1,
        combo: c,
        scoreAwarded: 300,
      });
    }
    // 300 * (1+2+3+4+5) = 4500
    expect(s.score).toBe(4500);
    expect(s.maxCombo).toBe(5);
    expect(s.hitCounts.max).toBe(5);
  });

  it("good notes pay 200 base", () => {
    let s = emptyScoreState();
    s = applyHit(s, {
      judgment: "good",
      deltaMs: 60,
      noteIndex: 0,
      combo: 1,
      scoreAwarded: 200,
    });
    expect(s.score).toBe(200);
    expect(s.hitCounts.good).toBe(1);
  });

  it("ok and meh follow the same combo math", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "ok", deltaMs: 95, noteIndex: 0, combo: 10, scoreAwarded: 100 });
    expect(s.score).toBe(1000); // 100 * 10
    s = applyHit(s, { judgment: "meh", deltaMs: 120, noteIndex: 1, combo: 11, scoreAwarded: 50 });
    expect(s.score).toBe(1550); // +50*11
  });

  it("miss resets combo and multiplier", () => {
    let s = emptyScoreState();
    for (let c = 1; c <= 5; c++) {
      s = applyHit(s, { judgment: "great", deltaMs: 30, noteIndex: c, combo: c, scoreAwarded: 300 });
    }
    s = applyMiss(s);
    expect(s.combo).toBe(0);
    expect(s.maxCombo).toBe(5);
    expect(s.multiplier).toBe(1);
    expect(s.hitCounts.miss).toBe(1);
  });
});

describe("accuracyPercent", () => {
  it("returns 100 when no notes processed", () => {
    expect(accuracyPercent(emptyScoreState())).toBe(100);
  });

  it("max and great weight at 1.0", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "max", deltaMs: 0, noteIndex: 0, combo: 1, scoreAwarded: 300 });
    s = applyHit(s, { judgment: "great", deltaMs: 30, noteIndex: 1, combo: 2, scoreAwarded: 300 });
    expect(accuracyPercent(s)).toBeCloseTo(100);
  });

  it("good/ok/meh weight proportional to base/300", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "good", deltaMs: 60, noteIndex: 0, combo: 1, scoreAwarded: 200 });
    s = applyHit(s, { judgment: "ok", deltaMs: 95, noteIndex: 1, combo: 2, scoreAwarded: 100 });
    s = applyHit(s, { judgment: "meh", deltaMs: 120, noteIndex: 2, combo: 3, scoreAwarded: 50 });
    s = applyMiss(s);
    // weights: (200+100+50)/300 = 1.167 over 4 notes => 29.17%
    const expected = ((200 / 300) + (100 / 300) + (50 / 300)) / 4 * 100;
    expect(accuracyPercent(s)).toBeCloseTo(expected, 1);
  });

  // osu! accuracy formula: total score value / max possible score value.
  // A single hit of value V over a one-note song yields V/300 * 100% acc.
  it("single 300 (max or great) is exactly 100% accuracy", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "max", deltaMs: 0, noteIndex: 0, combo: 1, scoreAwarded: 300 });
    expect(accuracyPercent(s)).toBeCloseTo(100, 4);
  });

  it("single 100 (ok) is 33.33% accuracy", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "ok", deltaMs: 95, noteIndex: 0, combo: 1, scoreAwarded: 100 });
    expect(accuracyPercent(s)).toBeCloseTo(100 / 3, 4);
  });

  it("single 50 (meh) is 16.67% accuracy", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "meh", deltaMs: 130, noteIndex: 0, combo: 1, scoreAwarded: 50 });
    expect(accuracyPercent(s)).toBeCloseTo(50 / 3, 4);
  });

  it("single miss is 0% accuracy", () => {
    let s = applyMiss(emptyScoreState());
    expect(accuracyPercent(s)).toBeCloseTo(0, 4);
  });

  it("accuracy is independent of combo", () => {
    // Same judgment counts -> same accuracy regardless of whether they were
    // all chained or broken up by misses. Score differs (combo math) but
    // acc must not.
    let chained = emptyScoreState();
    for (let c = 1; c <= 4; c++) {
      chained = applyHit(chained, {
        judgment: "ok",
        deltaMs: 90,
        noteIndex: c - 1,
        combo: c,
        scoreAwarded: 100,
      });
    }
    let broken = emptyScoreState();
    for (let c = 1; c <= 4; c++) {
      broken = applyHit(broken, {
        judgment: "ok",
        deltaMs: 90,
        noteIndex: c - 1,
        combo: 1,
        scoreAwarded: 100,
      });
    }
    expect(accuracyPercent(chained)).toBeCloseTo(accuracyPercent(broken), 6);
    expect(accuracyPercent(chained)).toBeCloseTo(100 / 3, 4);
    expect(chained.score).not.toBe(broken.score); // score IS combo-dependent
  });

  it("matches total-value / max-possible-value identity", () => {
    let s = emptyScoreState();
    s = applyHit(s, { judgment: "max", deltaMs: 0, noteIndex: 0, combo: 1, scoreAwarded: 300 });
    s = applyHit(s, { judgment: "ok", deltaMs: 90, noteIndex: 1, combo: 2, scoreAwarded: 100 });
    s = applyHit(s, { judgment: "meh", deltaMs: 130, noteIndex: 2, combo: 3, scoreAwarded: 50 });
    s = applyMiss(s);
    const totalValue = 300 + 100 + 50 + 0;
    const maxPossible = 4 * 300;
    expect(accuracyPercent(s)).toBeCloseTo((totalValue / maxPossible) * 100, 4);
  });
});
