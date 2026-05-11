import { describe, it, expect } from "vitest";
import { FixedClock, gameTimeMs } from "@/game/clock";

describe("gameTimeMs", () => {
  it("converts audio seconds + offset to ms", () => {
    const c = new FixedClock(1.234);
    expect(gameTimeMs(c, 0)).toBeCloseTo(1234);
    expect(gameTimeMs(c, 25)).toBeCloseTo(1259);
    expect(gameTimeMs(c, -10)).toBeCloseTo(1224);
  });

  it("tracks updates to currentTime", () => {
    const c = new FixedClock(0);
    c.set(0.5);
    expect(gameTimeMs(c, 0)).toBe(500);
    c.set(0.7);
    expect(gameTimeMs(c, 0)).toBeCloseTo(700);
  });
});
