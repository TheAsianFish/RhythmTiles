import { describe, expect, it } from "vitest";
import { computeCalibrationOffset, metronomeBeats } from "@/game/calibration";

describe("metronomeBeats", () => {
  it("produces evenly spaced beats", () => {
    const beats = metronomeBeats(120, 4);
    expect(beats).toEqual([0, 500, 1000, 1500]);
  });
});

describe("computeCalibrationOffset", () => {
  const beats = metronomeBeats(120, 16);
  const audioStart = 1000; // performance.now() when audio time = 0

  it("returns ~0 when taps land exactly on the beat", () => {
    const taps = beats.map((b) => audioStart + b);
    const result = computeCalibrationOffset({
      expectedAudioMs: beats,
      actualPerfMs: taps,
      audioStartPerfMs: audioStart,
    });
    expect(Math.abs(result.offsetMs)).toBeLessThan(0.5);
    expect(result.usableTaps).toBe(beats.length - 4);
  });

  it("returns -30ms when user is consistently 30ms late", () => {
    const lateBy = 30;
    const taps = beats.map((b) => audioStart + b + lateBy);
    const result = computeCalibrationOffset({
      expectedAudioMs: beats,
      actualPerfMs: taps,
      audioStartPerfMs: audioStart,
    });
    // user late by +30, so the computed offset is -mean(delta) = -30.
    expect(result.offsetMs).toBeCloseTo(-30, 0);
  });

  it("rejects far-out taps as noise", () => {
    const taps = beats.map((b, i) => audioStart + b + (i === 8 ? 1000 : 0));
    const result = computeCalibrationOffset({
      expectedAudioMs: beats,
      actualPerfMs: taps,
      audioStartPerfMs: audioStart,
    });
    expect(result.rejectedTaps).toBeGreaterThanOrEqual(1);
    expect(Math.abs(result.offsetMs)).toBeLessThan(0.5);
  });

  it("drops the warmup count from the front", () => {
    const taps = beats.map((b, i) => audioStart + b + (i < 4 ? 200 : 0));
    const result = computeCalibrationOffset({
      expectedAudioMs: beats,
      actualPerfMs: taps,
      audioStartPerfMs: audioStart,
    });
    // Warmup taps (which are off by 200ms) should be dropped; remaining taps are exact.
    expect(result.offsetMs).toBeCloseTo(0, 0);
  });
});
