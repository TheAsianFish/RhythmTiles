// Canvas 2D renderer for the 4-lane HUD.
//
// Positions are computed from gameTimeMs (audio-derived), never from rAF time.
// The renderer is dumb: it draws what it's told. The game loop owns timing.
//
// Layout: lanes fill the canvas width with a small side margin. The canvas
// itself is transparent so the panel's CSS gradient shows through. Notes get
// a subtle border + glow to stay legible against arbitrary YouTube content.

import type { NoteRuntime } from "@/game/types";

export interface RenderConfig {
  sideMarginPx: number;        // horizontal padding inside the canvas
  laneGapPx: number;
  hitLineFromBottomPx: number;
  noteHeightPx: number;
  pixelsPerMs: number;         // note travel speed
  opacity: number;
  hudReservedTopPx: number;    // vertical space reserved for HUD text at the top
}

export const DEFAULT_RENDER_CONFIG: RenderConfig = {
  sideMarginPx: 16,
  laneGapPx: 4,
  hitLineFromBottomPx: 100,
  noteHeightPx: 22,
  pixelsPerMs: 0.55,
  opacity: 1.0,
  hudReservedTopPx: 96,
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
    const usableWidth = Math.max(0, W - c.sideMarginPx * 2);
    const laneWidth = Math.max(20, (usableWidth - c.laneGapPx * 3) / 4);
    const laneOriginX = c.sideMarginPx;
    const totalLanesWidth = laneWidth * 4 + c.laneGapPx * 3;
    const hitLineY = H - c.hitLineFromBottomPx;
    const lanesTop = c.hudReservedTopPx;

    // Soft lane backgrounds (very subtle - panel gradient shows through).
    for (let i = 0; i < 4; i++) {
      const x = laneOriginX + i * (laneWidth + c.laneGapPx);
      const pressed = frame.pressedLanes.has(i);
      ctx.globalAlpha = pressed ? 0.22 : 0.08;
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(x, lanesTop, laneWidth, H - lanesTop);
      // Lane separator on the right edge.
      if (i < 3) {
        ctx.globalAlpha = 0.15;
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(x + laneWidth, lanesTop, c.laneGapPx, H - lanesTop);
      }
    }

    // Hit line.
    ctx.globalAlpha = 0.85;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(laneOriginX, hitLineY);
    ctx.lineTo(laneOriginX + totalLanesWidth, hitLineY);
    ctx.stroke();

    // Notes with subtle outline + glow so they read on any background.
    ctx.globalAlpha = c.opacity;
    for (const n of frame.notes) {
      if (n.hit || n.missed) continue;
      const dt = n.startMs - frame.gameMs;
      const y = hitLineY - dt * c.pixelsPerMs;
      if (y < lanesTop - c.noteHeightPx || y > H + c.noteHeightPx) continue;

      const lane = n.note.lane;
      const x = laneOriginX + lane * (laneWidth + c.laneGapPx);
      const fill = LANE_COLORS[lane]!;

      if (n.note.type === "hold") {
        const tailDt = n.endMs - frame.gameMs;
        const tailY = hitLineY - tailDt * c.pixelsPerMs;
        const top = Math.min(y, tailY);
        const height = Math.abs(y - tailY) + c.noteHeightPx;
        drawNoteRect(ctx, x + 3, top, laneWidth - 6, height, fill);
      } else {
        drawNoteRect(ctx, x + 3, y - c.noteHeightPx / 2, laneWidth - 6, c.noteHeightPx, fill);
      }
    }

    // Lane key labels below the hit line.
    ctx.globalAlpha = 0.85;
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 14px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i < 4; i++) {
      const x = laneOriginX + i * (laneWidth + c.laneGapPx) + laneWidth / 2;
      ctx.fillText(LANE_KEYS[i]!, x, hitLineY + 22);
    }

    // HUD text at top of the panel.
    ctx.globalAlpha = 1.0;
    ctx.textAlign = "left";
    ctx.fillStyle = "#ffffff";
    ctx.font = "600 13px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText("SCORE", c.sideMarginPx, 18);
    ctx.font = "bold 28px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(formatScore(frame.score), c.sideMarginPx, 46);

    ctx.font = "12px ui-sans-serif, system-ui, sans-serif";
    ctx.fillStyle = "#b8c3d3";
    ctx.fillText(`Combo x${frame.combo}`, c.sideMarginPx, 66);
    ctx.fillText(`Acc ${frame.accuracyPercent.toFixed(1)}%`, c.sideMarginPx, 84);

    // Judgment text near hit line, centered across the lanes.
    if (frame.lastJudgment && frame.gameMs - frame.lastJudgment.atMs < 350) {
      const age = frame.gameMs - frame.lastJudgment.atMs;
      const fade = Math.max(0, 1 - age / 350);
      ctx.globalAlpha = fade;
      ctx.textAlign = "center";
      ctx.font = "bold 26px ui-sans-serif, system-ui, sans-serif";
      ctx.fillStyle = judgmentColor(frame.lastJudgment.judgment);
      ctx.fillText(
        frame.lastJudgment.judgment.toUpperCase(),
        laneOriginX + totalLanesWidth / 2,
        hitLineY - 28,
      );
      ctx.globalAlpha = 1.0;
    }
  }
}

function drawNoteRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  fill: string,
): void {
  // Soft glow underneath.
  ctx.save();
  ctx.shadowColor = fill;
  ctx.shadowBlur = 10;
  ctx.fillStyle = fill;
  ctx.fillRect(x, y, w, h);
  ctx.restore();
  // Crisp inner border for legibility.
  ctx.strokeStyle = "rgba(255,255,255,0.7)";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(x + 0.75, y + 0.75, w - 1.5, h - 1.5);
}

function formatScore(score: number): string {
  // Group thousands with comma; keeps the HUD readable when combo scoring
  // sends the number into the millions.
  return score.toLocaleString("en-US");
}

function judgmentColor(j: string): string {
  switch (j) {
    case "max":
      return "#9ef0ff";   // rainbow / max: pale cyan
    case "great":
      return "#ffd700";   // gold
    case "good":
      return "#7cffb0";   // green
    case "ok":
      return "#7cb0ff";   // blue
    case "meh":
      return "#ffae5e";   // orange
    case "miss":
    default:
      return "#ff7c7c";   // red
  }
}
