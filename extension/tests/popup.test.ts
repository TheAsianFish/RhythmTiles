import { describe, expect, it } from "vitest";
import { codeToLabel, findConflicts } from "@/popup/popup";

describe("codeToLabel", () => {
  it("strips Key/Digit/Arrow prefixes", () => {
    expect(codeToLabel("KeyD")).toBe("D");
    expect(codeToLabel("KeyJ")).toBe("J");
    expect(codeToLabel("Digit7")).toBe("7");
    expect(codeToLabel("ArrowLeft")).toBe("Left");
  });

  it("uses friendly labels for punctuation and modifiers", () => {
    expect(codeToLabel("Semicolon")).toBe(";");
    expect(codeToLabel("ShiftLeft")).toBe("LShift");
    expect(codeToLabel("Space")).toBe("Space");
  });

  it("falls back to the raw code when unrecognized", () => {
    expect(codeToLabel("Fn")).toBe("Fn");
  });
});

describe("findConflicts", () => {
  it("returns empty set when all bindings are unique", () => {
    const c = findConflicts(["KeyD", "KeyF", "KeyJ", "KeyK"]);
    expect(c.size).toBe(0);
  });

  it("flags both lane indices that share a binding", () => {
    const c = findConflicts(["KeyD", "KeyF", "KeyD", "KeyK"]);
    expect(c.has(0)).toBe(true);
    expect(c.has(2)).toBe(true);
    expect(c.has(1)).toBe(false);
    expect(c.has(3)).toBe(false);
  });

  it("flags a 3-way conflict", () => {
    const c = findConflicts(["KeyD", "KeyD", "KeyD", "KeyK"]);
    expect(c.has(0) && c.has(1) && c.has(2)).toBe(true);
    expect(c.has(3)).toBe(false);
  });
});
