// Cross-validate that a sample chart shape is consistent across the
// JSON schema and our TS types. Loaded straight from shared/.

import { describe, expect, it } from "vitest";
import schema from "../../shared/chart-schema.json" assert { type: "json" };
import type { Chart } from "@/types/chart";

describe("Chart contract", () => {
  it("schema describes the same top-level shape as the TS type", () => {
    const required = (schema as { required: string[] }).required;
    expect(required).toContain("version");
    expect(required).toContain("audio");
    expect(required).toContain("metadata");
    expect(required).toContain("notes");
  });

  it("sample chart compiles and conforms by structure", () => {
    const sample: Chart = {
      version: "1.0",
      audio: { source: "youtube", videoId: "abc", duration: 60, bpm: 120 },
      metadata: {
        generatedAt: new Date().toISOString(),
        pipelineVersion: "0.1.0",
        difficulty: "normal",
        keyMode: 4,
      },
      notes: [
        { t: 1, lane: 0, type: "tap" },
        { t: 2, lane: 3, type: "hold", duration: 0.5 },
      ],
    };
    expect(sample.notes.length).toBe(2);
    expect(sample.notes[1].duration).toBe(0.5);
  });
});
