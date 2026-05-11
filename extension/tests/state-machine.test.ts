import { describe, expect, it } from "vitest";
import { initialContext, reduce } from "@/game/state-machine";

describe("state machine", () => {
  it("happy path without calibration", () => {
    let ctx = initialContext(false);
    ctx = reduce(ctx, { type: "start" });
    expect(ctx.state).toBe("loading");
    ctx = reduce(ctx, { type: "chart_ready" });
    expect(ctx.state).toBe("ready");
    ctx = reduce(ctx, { type: "start" });
    expect(ctx.state).toBe("playing");
    ctx = reduce(ctx, { type: "finish" });
    expect(ctx.state).toBe("results");
  });

  it("inserts calibration on first run", () => {
    let ctx = initialContext(true);
    ctx = reduce(ctx, { type: "start" });
    ctx = reduce(ctx, { type: "chart_ready" });
    expect(ctx.state).toBe("calibrating");
    ctx = reduce(ctx, { type: "calibration_done" });
    expect(ctx.state).toBe("ready");
    expect(ctx.needsCalibration).toBe(false);
  });

  it("supports pause/resume", () => {
    let ctx = initialContext(false);
    ctx = reduce(ctx, { type: "start" });
    ctx = reduce(ctx, { type: "chart_ready" });
    ctx = reduce(ctx, { type: "start" });
    ctx = reduce(ctx, { type: "pause" });
    expect(ctx.state).toBe("paused");
    ctx = reduce(ctx, { type: "resume" });
    expect(ctx.state).toBe("playing");
  });

  it("error in loading transitions to error and reset returns to idle", () => {
    let ctx = initialContext(false);
    ctx = reduce(ctx, { type: "start" });
    ctx = reduce(ctx, { type: "error", message: "backend down" });
    expect(ctx.state).toBe("error");
    expect(ctx.errorMessage).toBe("backend down");
    ctx = reduce(ctx, { type: "reset" });
    expect(ctx.state).toBe("idle");
  });
});
