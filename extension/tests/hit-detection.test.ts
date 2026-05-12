import { describe, expect, it } from "vitest";
import {
  advanceCursor,
  findCandidateNote,
  judge,
  registerPress,
  scoreForJudgment,
} from "@/game/hit-detection";
import { DEFAULT_OD, hitWindowsForOD, noteRuntimes } from "@/game/types";

const chart = noteRuntimes([
  { t: 1.0, lane: 0, type: "tap" },
  { t: 1.2, lane: 1, type: "tap" },
  { t: 1.5, lane: 0, type: "tap" },
  { t: 2.0, lane: 3, type: "hold", duration: 0.4 },
]);

describe("hitWindowsForOD", () => {
  it("matches the osu!mania v1 formula", () => {
    const w = hitWindowsForOD(8);
    expect(w.max).toBeCloseTo(16.5);
    expect(w.great).toBeCloseTo(40);  // 64 - 3*8
    expect(w.good).toBeCloseTo(73);   // 97 - 3*8
    expect(w.ok).toBeCloseTo(103);    // 127 - 3*8
    expect(w.meh).toBeCloseTo(127);   // 151 - 3*8
    expect(w.miss).toBeCloseTo(164);  // 188 - 3*8
  });

  it("tighter windows at higher OD", () => {
    const w8 = hitWindowsForOD(8);
    const w10 = hitWindowsForOD(10);
    expect(w10.great).toBeLessThan(w8.great);
    expect(w10.miss).toBeLessThan(w8.miss);
  });
});

describe("judge", () => {
  const w = hitWindowsForOD(DEFAULT_OD);
  it("buckets by absolute delta into six tiers", () => {
    expect(judge(0, w)).toBe("max");
    expect(judge(16.5, w)).toBe("max");
    expect(judge(16.6, w)).toBe("great");
    expect(judge(40, w)).toBe("great");
    expect(judge(41, w)).toBe("good");
    expect(judge(73, w)).toBe("good");
    expect(judge(74, w)).toBe("ok");
    expect(judge(103, w)).toBe("ok");
    expect(judge(104, w)).toBe("meh");
    expect(judge(127, w)).toBe("meh");
    expect(judge(128, w)).toBe("miss");
    expect(judge(-16, w)).toBe("max");
    expect(judge(-200, w)).toBe("miss");
  });
});

describe("scoreForJudgment", () => {
  it("matches the spec values", () => {
    expect(scoreForJudgment("max")).toBe(300);
    expect(scoreForJudgment("great")).toBe(300);
    expect(scoreForJudgment("good")).toBe(200);
    expect(scoreForJudgment("ok")).toBe(100);
    expect(scoreForJudgment("meh")).toBe(50);
    expect(scoreForJudgment("miss")).toBe(0);
  });
});

describe("findCandidateNote", () => {
  it("returns nearest unhit note in lane", () => {
    const found = findCandidateNote(chart, 0, 1000, 0);
    expect(found?.idx).toBe(0);
    expect(found?.deltaMs).toBe(0);
  });

  it("skips notes in other lanes", () => {
    const found = findCandidateNote(chart, 0, 1200, 0, 400);
    expect(found?.idx).toBe(0);
  });

  it("returns null when outside search window", () => {
    const found = findCandidateNote(chart, 0, 5000, 0, 150);
    expect(found).toBeNull();
  });
});

describe("advanceCursor", () => {
  it("marks past-meh-window notes as missed", () => {
    const runs = noteRuntimes([
      { t: 1.0, lane: 0, type: "tap" },
      { t: 1.2, lane: 1, type: "tap" },
    ]);
    // Default meh = 127ms. 1500 - 127 = 1373 > 1200, so both miss.
    const c = advanceCursor(runs, 0, 1500);
    expect(c).toBe(2);
    expect(runs[0].missed).toBe(true);
    expect(runs[1].missed).toBe(true);
  });
});

describe("registerPress", () => {
  it("returns a max-tier hit on a near-perfect press", () => {
    const runs = noteRuntimes([{ t: 1.0, lane: 0, type: "tap" }]);
    const result = registerPress({
      pressGameMs: 1010,
      lane: 0,
      notes: runs,
      cursor: 0,
      combo: 5,
    });
    expect(result?.judgment).toBe("max");
    expect(result?.combo).toBe(6);
    expect(result?.scoreAwarded).toBe(300);
    expect(runs[0].hit).toBe(true);
  });

  it("buckets a 50ms-late press as great", () => {
    const runs = noteRuntimes([{ t: 1.0, lane: 0, type: "tap" }]);
    const result = registerPress({
      pressGameMs: 1035,
      lane: 0,
      notes: runs,
      cursor: 0,
      combo: 0,
      windows: hitWindowsForOD(8),
    });
    expect(result?.judgment).toBe("great");
    expect(result?.scoreAwarded).toBe(300);
  });

  it("buckets a 60ms-late press as good", () => {
    const runs = noteRuntimes([{ t: 1.0, lane: 0, type: "tap" }]);
    const result = registerPress({
      pressGameMs: 1060,
      lane: 0,
      notes: runs,
      cursor: 0,
      combo: 0,
      windows: hitWindowsForOD(8),
    });
    expect(result?.judgment).toBe("good");
    expect(result?.scoreAwarded).toBe(200);
  });

  it("ignores presses outside meh window", () => {
    const runs = noteRuntimes([{ t: 1.0, lane: 0, type: "tap" }]);
    const result = registerPress({
      pressGameMs: 1500,
      lane: 0,
      notes: runs,
      cursor: 0,
      combo: 0,
    });
    expect(result).toBeNull();
  });
});
