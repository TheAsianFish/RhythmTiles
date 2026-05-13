// TypeScript types for the Chart JSON contract.
// Source of truth: shared/chart-schema.json (and backend/app/models.py).
// A backend test guards drift between Pydantic and the schema. The extension
// side stays manual; keep this file aligned by eye when the schema changes.

export type Difficulty = "easy" | "normal" | "hard" | "expert";
export type NoteType = "tap" | "hold";
export type AudioSource = "youtube" | "upload" | "synthetic";

export interface AudioMeta {
  source: AudioSource;
  videoId?: string;
  contentHash?: string;
  duration: number;
  bpm: number;
  bpmCurve?: Array<[number, number]>;
}

export interface ChartMeta {
  generatedAt: string;
  pipelineVersion: string;
  difficulty: Difficulty;
  keyMode: 4;
  title?: string;
  artist?: string;
}

export interface Note {
  t: number;        // seconds from audio t=0
  lane: 0 | 1 | 2 | 3;
  type: NoteType;
  duration?: number; // required when type === "hold"
}

export interface Section {
  start: number;
  end: number;
  intensity?: number;
  label?: string;
}

export interface Chart {
  version: "1.0";
  audio: AudioMeta;
  metadata: ChartMeta;
  notes: Note[];
  sections?: Section[];
}

export interface GenerateRequest {
  videoId?: string;
  audioUrl?: string;
  difficulty?: Difficulty;
  duration?: number;
}

/**
 * Runtime shape check for a Chart received over postMessage. The overlay
 * loop crashes ungracefully if any of these fields are missing or the
 * wrong type, and a malformed BB_LOAD_CHART has no other path to
 * surface back to the user. Returns null on the happy path, or a short
 * human-readable reason string when invalid.
 *
 * Intentionally permissive on fields the loop tolerates (e.g. missing
 * bpmCurve, missing sections, contentHash). Only the load-bearing
 * fields - notes array shape, audio.duration, metadata.difficulty -
 * are required.
 */
export function validateChart(value: unknown): string | null {
  if (!value || typeof value !== "object") return "not an object";
  const v = value as Record<string, unknown>;
  const audio = v.audio as Record<string, unknown> | undefined;
  const meta = v.metadata as Record<string, unknown> | undefined;
  if (!audio || typeof audio !== "object") return "missing audio block";
  if (typeof audio.duration !== "number" || !Number.isFinite(audio.duration)) {
    return "audio.duration not a finite number";
  }
  if (typeof audio.bpm !== "number") return "audio.bpm not a number";
  if (!meta || typeof meta !== "object") return "missing metadata block";
  if (typeof meta.difficulty !== "string") return "metadata.difficulty missing";
  if (!Array.isArray(v.notes)) return "notes is not an array";
  // Spot-check the first note's required fields; full validation per-note
  // would be O(n) on every chart load and the backend already validates
  // via Pydantic before sending. The cheap check catches a totally
  // mangled chart payload (e.g. notes that are objects instead of array).
  if (v.notes.length > 0) {
    const n0 = v.notes[0] as Record<string, unknown>;
    if (typeof n0.t !== "number") return "notes[0].t missing";
    if (typeof n0.lane !== "number") return "notes[0].lane missing";
    if (n0.type !== "tap" && n0.type !== "hold") {
      return "notes[0].type not tap/hold";
    }
  }
  return null;
}
