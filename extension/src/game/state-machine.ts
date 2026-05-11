// Game state machine.
//
// IDLE -> LOADING -> CALIBRATING (first run) -> READY -> PLAYING -> RESULTS
//                                                            ^
//                                                            |
//                                                          PAUSED

export type GameState =
  | "idle"
  | "loading"
  | "calibrating"
  | "ready"
  | "playing"
  | "paused"
  | "results"
  | "error";

export type GameEvent =
  | { type: "start" }
  | { type: "chart_ready" }
  | { type: "calibration_done" }
  | { type: "pause" }
  | { type: "resume" }
  | { type: "finish" }
  | { type: "reset" }
  | { type: "error"; message: string };

export interface GameContext {
  state: GameState;
  needsCalibration: boolean;
  errorMessage?: string;
}

export function initialContext(needsCalibration: boolean): GameContext {
  return { state: "idle", needsCalibration };
}

export function reduce(ctx: GameContext, ev: GameEvent): GameContext {
  switch (ctx.state) {
    case "idle":
      if (ev.type === "start") return { ...ctx, state: "loading" };
      break;
    case "loading":
      if (ev.type === "chart_ready") {
        return { ...ctx, state: ctx.needsCalibration ? "calibrating" : "ready" };
      }
      if (ev.type === "error") return { ...ctx, state: "error", errorMessage: ev.message };
      break;
    case "calibrating":
      if (ev.type === "calibration_done") return { ...ctx, state: "ready", needsCalibration: false };
      break;
    case "ready":
      if (ev.type === "start") return { ...ctx, state: "playing" };
      break;
    case "playing":
      if (ev.type === "pause") return { ...ctx, state: "paused" };
      if (ev.type === "finish") return { ...ctx, state: "results" };
      if (ev.type === "error") return { ...ctx, state: "error", errorMessage: ev.message };
      break;
    case "paused":
      if (ev.type === "resume") return { ...ctx, state: "playing" };
      if (ev.type === "finish") return { ...ctx, state: "results" };
      break;
    case "results":
    case "error":
      if (ev.type === "reset") return { state: "idle", needsCalibration: ctx.needsCalibration };
      break;
  }
  return ctx;
}
