// Canvas 2D renderer for the 4-lane HUD.
//
// Positions are computed from gameTimeMs (audio-derived), never from rAF time.
// The renderer is dumb: it draws what it's told. The game loop owns timing.

import type { NoteRuntime } from "@/game/types";

export interface RenderConfig {
  laneWidthPx: number;
  laneGapPx: number;
  hitLineFromBottomPx: number;
  noteHeightPx: number;
  // How many pixels per millisecond a note travels. Tunable for game feel.
  pixelsPerMs: number;
  opacity: number;
}

export const DEFAULT_RENDER_CONFIG: RenderConfig = {
  laneWidthPx: 96,
  laneGapPx: 4,
  hitLineFromBottomPx: 120,
  noteHeightPx: 28,
  pixelsPerMs: 0.6,
  opacity: 0.92,
};

const LANE_COLORS = ["#5fb7ff", "#a0e0ff", "#ffd28a", "#ff8aa0"];
const LANE_KEYS = ["D", "F", "J", "K"];

export interface HudFrame {
  gameMs: number;
  notes: NoteRuntime[];
  score: number;
  combo: number;
  multiplier: number;
  accuracyPercent: number;
  lastJudgment?: { judgment: string; deltaMs: number; atMs: number };
  pressedLanes: ReadonlySet<number>;
}

export class CanvasRenderer {
  private ctx: CanvasRenderingContext2D;
  private config: RenderConfig;

  constructor(canvas: HTMLCanvasElement, config: Partial<RenderConfig> = {}) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas 2d context unavailable");
    this.ctx = ctx;
    this.config = { ...DEFAULT_RENDER_CONFIG, ...config };
  }

  setConfig(c: Partial<RenderConfig>) {
    this.config = { ...this.config, ...c };
  }

  resize(width: number, height: number) {
    const dpr = (globalThis.devicePixelRatio || 1);
    const canvas = this.ctx.canvas;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  draw(frame: HudFrame) {
    const { ctx } = this;
    const canvas = ctx.canvas;
    const W = canvas.clientWidth || canvas.width;
    const H = canvas.clientHeight || canvas.height;

    ctx.clearRect(0, 0, W, H);

    const c = this.config;
    const totalLanesWidth = c.laneWidthPx * 4 + c.laneGapPx * 3;
    const laneOriginX = Math.floor((W - totalLanesWidth) / 2);
    const hitLineY = H - c.hitLineFromBottomPx;

    // Dim backdrop column so the lanes read against the video.
    ctx.globalAlpha = 0.35 * c.opacity;
    ctx.fillStyle = "#000";
    ctx.fillRect(laneOriginX - 8, 0, totalLanesWidth + 16, H);

    // Lane backgrounds.
    ctx.globalAlpha = 0.55 * c.opacity;
    for (let i = 0; i < 4; i++) {
      const x = laneOriginX + i * (c.laneWidthPx + c.laneGapPx);
      const pressed = frame.pressedLanes.has(i);
      ctx.fillStyle = pressed ? "rgba(255,255,255,0.18)" : "rgba(255,255,255,0.06)";
      ctx.fillRect(x, 0, c.laneWidthPx, H);
    }

    // Hit line.
    ctx.globalAlpha = 0.9 * c.opacity;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(laneOriginX, hitLineY);
    ctx.lineTo(laneOriginX + totalLanesWidth, hitLineY);
    ctx.stroke();

    // Notes.
    ctx.globalAlpha = c.opacity;
    for (const n of frame.notes) {
      if (n.hit || n.missed) continue;
      const dt = n.startMs - frame.gameMs;
      const y = hitLineY - dt * c.pixelsPerMs;
      // Skip notes off-screen.
      if (y < -c.noteHeightPx || y > H + c.noteHeightPx) continue;

      const lane = n.note.lane;
      const x = laneOriginX + lane * (c.laneWidthPx + c.laneGapPx);

      ctx.fillStyle = LANE_COLORS[lane]!;
      if (n.note.type === "hold") {
        const tailDt = n.endMs - frame.gameMs;
        const tailY = hitLineY - tailDt * c.pixelsPerMs;
        const top = Math.min(y, tailY);
        const height = Math.abs(y - tailY) + c.noteHeightPx;
        ctx.fillRect(x + 4, top, c.laneWidthPx - 8, height);
      } else {
        ctx.fillRect(x + 4, y - c.noteHeightPx / 2, c.laneWidthPx - 8, c.noteHeightPx);
      }
    }

    // Lane key labels under the hit line.
    ctx.globalAlpha = 0.9 * c.opacity;
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 18px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i < 4; i++) {
      const x = laneOriginX + i * (c.laneWidthPx + c.laneGapPx) + c.laneWidthPx / 2;
      ctx.fillText(LANE_KEYS[i]!, x, hitLineY + 32);
    }

    // HUD text.
    ctx.globalAlpha = 1.0;
    ctx.textAlign = "left";
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 24px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(`Score ${frame.score}`, 24, 36);
    ctx.font = "16px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(`Combo ${frame.combo}x${frame.multiplier.toFixed(1)}`, 24, 60);
    ctx.fillText(`Acc ${frame.accuracyPercent.toFixed(1)}%`, 24, 82);

    // Judgment text near hit line.
    if (frame.lastJudgment && frame.gameMs - frame.lastJudgment.atMs < 250) {
      ctx.textAlign = "center";
      ctx.font = "bold 28px ui-sans-serif, system-ui, sans-serif";
      ctx.fillStyle = judgmentColor(frame.lastJudgment.judgment);
      ctx.fillText(frame.lastJudgment.judgment.toUpperCase(), W / 2, hitLineY - 40);
    }
  }
}

function judgmentColor(j: string): string {
  switch (j) {
    case "perfect":
      return "#ffd700";
    case "good":
      return "#7cffb0";
    case "ok":
      return "#7cb0ff";
    default:
      return "#ff7c7c";
  }
}
