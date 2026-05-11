import { describe, expect, it } from "vitest";
import {
  advanceCursor,
  findCandidateNote,
  judge,
  registerPress,
  scoreForJudgment,
} from "@/game/hit-detection";
import { DEFAULT_HIT_WINDOWS, noteRuntimes } from "@/game/types";

const chart = noteRuntimes([
  { t: 1.0, lane: 0, type: "tap" },
  { t: 1.2, lane: 1, type: "tap" },
  { t: 1.5, lane: 0, type: "tap" },
  { t: 2.0, lane: 3, type: "hold", duration: 0.4 },
]);

describe("judge", () => {
  it("buckets by absolute delta", () => {
    expect(judge(0)).toBe("perfect");
    expect(judge(24.9)).toBe("perfect");
    expect(judge(-25)).toBe("perfect");
    expect(judge(40)).toBe("good");
    expect(judge(-49)).toBe("good");
    expect(judge(80)).toBe("ok");
    expect(judge(101)).toBe("miss");
  });
});

describe("scoreForJudgment", () => {
  it("matches design table", () => {
    expect(scoreForJudgment("perfect")).toBe(300);
    expect(scoreForJudgment("good")).toBe(150);
    expect(scoreForJudgment("ok")).toBe(50);
    expect(scoreForJudgment("miss")).toBe(0);
  });
});

describe("findCandidateNote", () => {
  it("returns nearest unhit note in lane", () => {
    // press at 1000ms in lane 0; nearest note is at 1000ms.
    const found = findCandidateNote(chart, 0, 1000, 0);
    expect(found?.idx).toBe(0);
    expect(found?.deltaMs).toBe(0);
  });

  it("skips notes in other lanes", () => {
    // press at 1200ms in lane 0. Note 1 (lane 1) at 1200ms is exactly aligned
    // but wrong lane, so we should skip it. In lane 0, note 0 is at 1000ms
    // (delta 200ms = miss window) and note 2 is at 1500ms (delta 300ms,
    // outside ok window). With searchWindowMs = ok (100ms) both are outside;
    // expand search to a wider window so we can verify the lane filter alone.
    const found = findCandidateNote(chart, 0, 1200, 0, 400);
    // Closer-in-lane wins; note 0 is at delta 200, note 2 at delta 300.
    expect(found?.idx).toBe(0);
  });

  it("returns null when outside search window", () => {
    const found = findCandidateNote(chart, 0, 5000, 0, DEFAULT_HIT_WINDOWS.ok);
    expect(found).toBeNull();
  });
});

describe("advanceCursor", () => {
  it("marks past-window notes as missed", () => {
    const runs = noteRuntimes([
      { t: 1.0, lane: 0, type: "tap" },
      { t: 1.2, lane: 1, type: "tap" },
    ]);
    const c = advanceCursor(runs, 0, 1500); // 1500 - 100 = 1400 > 1200 -> both missed
    expect(c).toBe(2);
    expect(runs[0].missed).toBe(true);
    expect(runs[1].missed).toBe(true);
  });

  it("leaves notes still in-window untouched", () => {
    const runs = noteRuntimes([{ t: 1.0, lane: 0, type: "tap" }]);
    const c = advanceCursor(runs, 0, 1050);
    expect(c).toBe(0);
    expect(runs[0].missed).toBe(false);
  });
});

describe("registerPress", () => {
  it("returns a hit on the closest note in window", () => {
    const runs = noteRuntimes([{ t: 1.0, lane: 0, type: "tap" }]);
    const result = registerPress({
      pressGameMs: 1010,
      lane: 0,
      notes: runs,
      cursor: 0,
      combo: 5,
    });
    expect(result?.judgment).toBe("perfect");
    expect(result?.combo).toBe(6);
    expect(runs[0].hit).toBe(true);
  });

  it("ignores presses outside ok window", () => {
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
