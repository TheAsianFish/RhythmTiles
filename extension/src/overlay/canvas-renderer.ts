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

// Animation timing constants. Tuned so the eye registers each flash but they
// never linger long enough to confuse the player about timing.
const LANE_FLASH_DECAY_MS = 180;
const HIT_LINE_PULSE_MS = 220;
const MISS_VIGNETTE_DECAY_MS = 450;
const MILESTONE_FLASH_MS = 600;
const MISS_STREAK_THRESHOLD = 3;
const MILESTONE_EVERY = 50;

export interface HudFrame {
  gameMs: number;
  notes: NoteRuntime[];
  score: number;
  combo: number;
  multiplier: number;
  accuracyPercent: number;
  lastJudgment?: { judgment: string; deltaMs: number; atMs: number };
  pressedLanes: ReadonlySet<number>;
  // Recent hit deltas in ms. Most recent at the END of the array. Renderer
  // shows the last ~12 as ticks on a calibration bar above the hit line.
  recentDeltas?: ReadonlyArray<{ deltaMs: number; tier: string }>;
}

interface AnimState {
  // performance.now() at last press, per lane. Used for the lane flash decay.
  lanePressAt: [number, number, number, number];
  // Last seen value of pressedLanes so we only re-arm the press flash on a
  // fresh press (not while held).
  prevPressed: Set<number>;
  // Last gameMs we saw a non-miss judgment land at, used for the hit-line
  // pulse animation.
  lastHitAt: number;
  // Consecutive miss counter. Resets on any non-miss judgment.
  missStreak: number;
  // When (gameMs) the most recent miss landed, for the red vignette fade.
  lastMissAt: number;
  // Highest combo we've seen so far. When the current combo crosses a
  // MILESTONE_EVERY multiple, we set milestoneFlashAt to start the flash.
  prevCombo: number;
  milestoneFlashAt: number;
  milestoneValue: number;
}

function freshAnimState(): AnimState {
  return {
    lanePressAt: [-1e9, -1e9, -1e9, -1e9],
    prevPressed: new Set(),
    lastHitAt: -1e9,
    missStreak: 0,
    lastMissAt: -1e9,
    prevCombo: 0,
    milestoneFlashAt: -1e9,
    milestoneValue: 0,
  };
}

export class CanvasRenderer {
  private ctx: CanvasRenderingContext2D;
  private config: RenderConfig;
  private anim: AnimState = freshAnimState();

  constructor(canvas: HTMLCanvasElement, config: Partial<RenderConfig> = {}) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas 2d context unavailable");
    this.ctx = ctx;
    this.config = { ...DEFAULT_RENDER_CONFIG, ...config };
  }

  setConfig(c: Partial<RenderConfig>) {
    this.config = { ...this.config, ...c };
  }

  // Drop all transient animation state. Called when the loop seeks or restarts.
  resetAnim() {
    this.anim = freshAnimState();
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
    this.updateAnimState(frame);

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
    const now = performance.now();

    // Lane backgrounds. Pressed lanes get a brighter base, and we layer a
    // decaying flash on top that uses the lane color.
    for (let i = 0; i < 4; i++) {
      const x = laneOriginX + i * (laneWidth + c.laneGapPx);
      const pressed = frame.pressedLanes.has(i);

      // Static lane fill.
      ctx.globalAlpha = pressed ? 0.22 : 0.08;
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(x, lanesTop, laneWidth, H - lanesTop);

      // Press-flash overlay using the lane color, fading vertically toward
      // the hit line so the eye reads it as "an event happened here".
      const sincePress = now - this.anim.lanePressAt[i]!;
      if (sincePress < LANE_FLASH_DECAY_MS) {
        const t = 1 - sincePress / LANE_FLASH_DECAY_MS;
        const grad = ctx.createLinearGradient(0, lanesTop, 0, H);
        grad.addColorStop(0, hexWithAlpha(LANE_COLORS[i]!, 0));
        grad.addColorStop(0.85, hexWithAlpha(LANE_COLORS[i]!, 0.35 * t));
        grad.addColorStop(1, hexWithAlpha(LANE_COLORS[i]!, 0.55 * t));
        ctx.globalAlpha = 1.0;
        ctx.fillStyle = grad;
        ctx.fillRect(x, lanesTop, laneWidth, H - lanesTop);
      }

      // Lane separator.
      if (i < 3) {
        ctx.globalAlpha = 0.15;
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(x + laneWidth, lanesTop, c.laneGapPx, H - lanesTop);
      }
    }

    // Hit line. Pulse the thickness + glow on each successful hit.
    const sinceHit = frame.gameMs - this.anim.lastHitAt;
    const pulse = sinceHit < HIT_LINE_PULSE_MS ? 1 - sinceHit / HIT_LINE_PULSE_MS : 0;
    ctx.save();
    ctx.globalAlpha = 0.85 + 0.15 * pulse;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2 + 2 * pulse;
    if (pulse > 0) {
      ctx.shadowColor = "#ffffff";
      ctx.shadowBlur = 14 * pulse;
    }
    ctx.beginPath();
    ctx.moveTo(laneOriginX, hitLineY);
    ctx.lineTo(laneOriginX + totalLanesWidth, hitLineY);
    ctx.stroke();
    ctx.restore();

    // Notes.
    ctx.globalAlpha = c.opacity;
    for (const n of frame.notes) {
      if (n.hit || n.missed) continue;
      const dt = n.startMs - frame.gameMs;
      const y = hitLineY - dt * c.pixelsPerMs;

      const lane = n.note.lane;
      const x = laneOriginX + lane * (laneWidth + c.laneGapPx);
      const fill = LANE_COLORS[lane]!;

      if (n.note.type === "hold") {
        // For an active hold, clamp the head to the hit line. Player is
        // holding the key; the head is "locked in" at the receptor and the
        // tail descends toward it. Released = the renderer stops drawing
        // this note because the loop set hit or missed.
        const tailDt = n.endMs - frame.gameMs;
        const tailY = hitLineY - tailDt * c.pixelsPerMs;
        const headY = n.holding ? hitLineY : y;
        const top = Math.min(headY, tailY);
        const height = Math.abs(headY - tailY) + c.noteHeightPx;
        if (top > H + c.noteHeightPx || top + height < lanesTop - c.noteHeightPx) continue;
        drawHoldNote(ctx, x + 3, top, laneWidth - 6, height, fill, n.holding);
      } else {
        if (y < lanesTop - c.noteHeightPx || y > H + c.noteHeightPx) continue;
        drawTapNote(ctx, x + 3, y - c.noteHeightPx / 2, laneWidth - 6, c.noteHeightPx, fill);
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
    ctx.fillText(`Acc ${frame.accuracyPercent.toFixed(1)}%`, c.sideMarginPx, 84);

    // Combo display: grows in size briefly on milestone (every 50). Sits on
    // the right side of the HUD bar so it doesn't fight the score number.
    drawCombo(ctx, W - c.sideMarginPx, 18, frame.combo, this.anim, now);

    // Hit-delta meter near the hit line. Shows last few hit timings as ticks.
    if (frame.recentDeltas && frame.recentDeltas.length > 0) {
      drawHitDeltaMeter(
        ctx,
        laneOriginX,
        hitLineY - 50,
        totalLanesWidth,
        frame.recentDeltas,
      );
    }

    // Judgment text.
    if (frame.lastJudgment && frame.gameMs - frame.lastJudgment.atMs < 350) {
      const age = frame.gameMs - frame.lastJudgment.atMs;
      const fade = Math.max(0, 1 - age / 350);
      const rise = (1 - fade) * 8;
      ctx.globalAlpha = fade;
      ctx.textAlign = "center";
      ctx.font = "bold 26px ui-sans-serif, system-ui, sans-serif";
      ctx.fillStyle = judgmentColor(frame.lastJudgment.judgment);
      ctx.shadowColor = "rgba(0,0,0,0.55)";
      ctx.shadowBlur = 4;
      ctx.fillText(
        frame.lastJudgment.judgment.toUpperCase(),
        laneOriginX + totalLanesWidth / 2,
        hitLineY - 32 - rise,
      );
      ctx.shadowBlur = 0;
      ctx.globalAlpha = 1.0;
    }

    // Miss-streak vignette. Red pulse on the edges of the canvas, kicks in
    // only when the player has chained MISS_STREAK_THRESHOLD or more misses.
    if (this.anim.missStreak >= MISS_STREAK_THRESHOLD) {
      const sinceMiss = frame.gameMs - this.anim.lastMissAt;
      if (sinceMiss < MISS_VIGNETTE_DECAY_MS) {
        const t = 1 - sinceMiss / MISS_VIGNETTE_DECAY_MS;
        const intensity = Math.min(0.55, 0.20 + this.anim.missStreak * 0.05) * t;
        const vg = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.35, W / 2, H / 2, Math.max(W, H) * 0.75);
        vg.addColorStop(0, "rgba(255,80,80,0)");
        vg.addColorStop(1, `rgba(255,80,80,${intensity.toFixed(3)})`);
        ctx.globalAlpha = 1.0;
        ctx.fillStyle = vg;
        ctx.fillRect(0, 0, W, H);
      }
    }

    // Combo milestone splash. Big number in the lane area, fades fast.
    const sinceMilestone = now - this.anim.milestoneFlashAt;
    if (sinceMilestone < MILESTONE_FLASH_MS) {
      const t = 1 - sinceMilestone / MILESTONE_FLASH_MS;
      ctx.save();
      ctx.globalAlpha = t * 0.85;
      ctx.textAlign = "center";
      ctx.font = `bold ${Math.round(44 + 14 * t)}px ui-sans-serif, system-ui, sans-serif`;
      ctx.fillStyle = "#ffd700";
      ctx.shadowColor = "rgba(255, 215, 0, 0.8)";
      ctx.shadowBlur = 18;
      ctx.fillText(`${this.anim.milestoneValue} COMBO`, W / 2, (lanesTop + hitLineY) / 2);
      ctx.restore();
    }
  }

  // Update per-frame derived animation state. Kept inside the renderer so the
  // game loop never has to know about animation timing.
  private updateAnimState(frame: HudFrame): void {
    const now = performance.now();

    // Lane press flash: fire on the transition unpressed -> pressed.
    for (let i = 0; i < 4; i++) {
      const isPressed = frame.pressedLanes.has(i);
      const wasPressed = this.anim.prevPressed.has(i);
      if (isPressed && !wasPressed) this.anim.lanePressAt[i] = now;
    }
    this.anim.prevPressed = new Set(frame.pressedLanes);

    // Hit line pulse + miss streak tracking. We only react to a fresh
    // judgment (different atMs than last time we saw one).
    const j = frame.lastJudgment;
    if (j && j.atMs > this.anim.lastHitAt && j.atMs > this.anim.lastMissAt) {
      if (j.judgment === "miss") {
        this.anim.missStreak += 1;
        this.anim.lastMissAt = j.atMs;
      } else {
        this.anim.lastHitAt = j.atMs;
        this.anim.missStreak = 0;
      }
    }

    // Combo milestone. Fire only when combo crosses an unseen multiple of
    // MILESTONE_EVERY upward (so a reset to 0 + climb back through 50 fires
    // a fresh splash, but a stationary 50 does not).
    if (frame.combo > this.anim.prevCombo) {
      const prevMilestone = Math.floor(this.anim.prevCombo / MILESTONE_EVERY);
      const curMilestone = Math.floor(frame.combo / MILESTONE_EVERY);
      if (curMilestone > prevMilestone && frame.combo % MILESTONE_EVERY === 0) {
        this.anim.milestoneFlashAt = now;
        this.anim.milestoneValue = frame.combo;
      }
    }
    this.anim.prevCombo = frame.combo;
  }
}

function drawTapNote(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  fill: string,
): void {
  // Vertical gradient so notes read as glossy capsules rather than flat
  // rectangles. Lighter at top, lane color at bottom, with a soft glow.
  const grad = ctx.createLinearGradient(x, y, x, y + h);
  grad.addColorStop(0, hexWithAlpha(fill, 1));
  grad.addColorStop(0.5, hexWithAlpha(fill, 0.95));
  grad.addColorStop(1, hexWithAlpha(fill, 0.7));
  ctx.save();
  ctx.shadowColor = fill;
  ctx.shadowBlur = 12;
  ctx.fillStyle = grad;
  roundRect(ctx, x, y, w, h, 4);
  ctx.fill();
  ctx.restore();
  ctx.strokeStyle = "rgba(255,255,255,0.75)";
  ctx.lineWidth = 1.5;
  roundRect(ctx, x + 0.75, y + 0.75, w - 1.5, h - 1.5, 3.5);
  ctx.stroke();
}

function drawHoldNote(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  fill: string,
  isHolding: boolean = false,
): void {
  // Body: translucent gradient pillar in the lane color. When the player is
  // actively holding the key, the body brightens and gains a pulse-style
  // outer glow so the locked-in state is visually unambiguous.
  ctx.save();
  ctx.shadowColor = fill;
  ctx.shadowBlur = isHolding ? 16 : 8;
  const grad = ctx.createLinearGradient(x, y, x, y + h);
  grad.addColorStop(0, hexWithAlpha(fill, isHolding ? 0.95 : 0.75));
  grad.addColorStop(1, hexWithAlpha(fill, isHolding ? 0.7 : 0.45));
  ctx.fillStyle = grad;
  roundRect(ctx, x, y, w, h, 4);
  ctx.fill();
  ctx.restore();
  // Bright caps at the head and tail so the player sees clear start/end.
  const capH = Math.min(8, h / 3);
  ctx.fillStyle = fill;
  ctx.fillRect(x, y + h - capH, w, capH);
  ctx.fillRect(x, y, w, capH);
  ctx.strokeStyle = isHolding ? "rgba(255,255,255,0.95)" : "rgba(255,255,255,0.6)";
  ctx.lineWidth = isHolding ? 1.8 : 1.2;
  roundRect(ctx, x + 0.5, y + 0.5, w - 1, h - 1, 3.5);
  ctx.stroke();
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function drawCombo(
  ctx: CanvasRenderingContext2D,
  rightX: number,
  topY: number,
  combo: number,
  anim: AnimState,
  now: number,
): void {
  if (combo <= 0) return;
  const sinceMilestone = now - anim.milestoneFlashAt;
  const pump = sinceMilestone < 250 ? 1 - sinceMilestone / 250 : 0;
  ctx.textAlign = "right";
  ctx.fillStyle = "#ffffff";
  ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
  ctx.fillText("COMBO", rightX, topY);
  ctx.font = `bold ${Math.round(28 + 8 * pump)}px ui-sans-serif, system-ui, sans-serif`;
  if (pump > 0) {
    ctx.shadowColor = "#ffd700";
    ctx.shadowBlur = 14 * pump;
  }
  ctx.fillText(`x${combo}`, rightX, topY + 28);
  ctx.shadowBlur = 0;
}

function drawHitDeltaMeter(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  recent: ReadonlyArray<{ deltaMs: number; tier: string }>,
): void {
  // Horizontal bar that maps deltaMs in [-160, +160] to the bar's width.
  // Center line marks 0ms (perfect). Each tick is colored by the tier the
  // hit was bucketed into; transparency fades older entries.
  const halfRange = 160;
  const barH = 4;
  const tickH = 12;
  ctx.save();
  ctx.globalAlpha = 0.45;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(x, y, width, barH);
  ctx.globalAlpha = 0.9;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(x + width / 2 - 0.5, y - 3, 1.5, barH + 6);
  const n = recent.length;
  for (let i = 0; i < n; i++) {
    const entry = recent[i]!;
    const age = (n - 1 - i) / Math.max(1, n - 1);
    const fade = 1 - age * 0.7;
    const clamped = Math.max(-halfRange, Math.min(halfRange, entry.deltaMs));
    const px = x + width / 2 + (clamped / halfRange) * (width / 2);
    ctx.globalAlpha = fade;
    ctx.fillStyle = judgmentColor(entry.tier);
    ctx.fillRect(px - 1, y - tickH / 2 + barH / 2, 2, tickH);
  }
  ctx.restore();
}

function formatScore(score: number): string {
  return score.toLocaleString("en-US");
}

function judgmentColor(j: string): string {
  switch (j) {
    case "max":
      return "#9ef0ff";
    case "great":
      return "#ffd700";
    case "good":
      return "#7cffb0";
    case "ok":
      return "#7cb0ff";
    case "meh":
      return "#ffae5e";
    case "miss":
    default:
      return "#ff7c7c";
  }
}

// Convert a "#rrggbb" hex string into a CSS rgba() with the given alpha.
// Falls back to the original string if the hex isn't recognized.
function hexWithAlpha(hex: string, alpha: number): string {
  if (!hex.startsWith("#") || hex.length !== 7) return hex;
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  if (Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b)) return hex;
  return `rgba(${r},${g},${b},${alpha})`;
}
