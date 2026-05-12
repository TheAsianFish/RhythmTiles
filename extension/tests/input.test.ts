import { describe, expect, it } from "vitest";
import { InputCapture, type InputEvent } from "@/game/input";

function makeCapture() {
  const events: InputEvent[] = [];
  const cap = new InputCapture({ codes: ["KeyD", "KeyF", "KeyJ", "KeyK"] });
  cap.subscribe((e) => events.push(e));
  return { cap, events };
}

describe("InputCapture.injectKey", () => {
  it("emits press then release for a known code", () => {
    const { cap, events } = makeCapture();
    cap.injectKey("KeyD", "press", 100);
    cap.injectKey("KeyD", "release", 180);
    expect(events).toEqual([
      { lane: 0, kind: "press", perfMs: 100 },
      { lane: 0, kind: "release", perfMs: 180 },
    ]);
  });

  it("dedups a held press (no second press fires until release)", () => {
    const { cap, events } = makeCapture();
    cap.injectKey("KeyJ", "press", 100);
    cap.injectKey("KeyJ", "press", 120); // ignored: still held
    cap.injectKey("KeyJ", "release", 180);
    cap.injectKey("KeyJ", "press", 200);
    expect(events.map((e) => `${e.lane}/${e.kind}`)).toEqual([
      "2/press",
      "2/release",
      "2/press",
    ]);
  });

  it("ignores unbound codes", () => {
    const { cap, events } = makeCapture();
    cap.injectKey("KeyZ", "press", 100);
    cap.injectKey("Space", "release", 100);
    expect(events).toEqual([]);
  });

  it("release with no matching press is a no-op", () => {
    const { cap, events } = makeCapture();
    cap.injectKey("KeyF", "release", 100);
    expect(events).toEqual([]);
  });
});
