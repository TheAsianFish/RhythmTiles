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
