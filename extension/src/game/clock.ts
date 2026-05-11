// The game clock. Audio time is ground truth; rAF only decides when to draw.

export interface ClockSource {
  // Returns the current audio time in seconds.
  currentTime: number;
  // True when the underlying media is paused (so the game can pause too).
  paused: boolean;
}

// A wrapper around an HTMLMediaElement used by both YouTube tab capture
// (video element on page) and any internal AudioContext playback.
export class MediaClock implements ClockSource {
  constructor(private readonly el: HTMLMediaElement) {}
  get currentTime(): number {
    return this.el.currentTime;
  }
  get paused(): boolean {
    return this.el.paused || this.el.ended || this.el.readyState < 2;
  }
}

// For tests and headless use.
export class FixedClock implements ClockSource {
  constructor(public currentTime = 0, public paused = false) {}
  set(t: number) {
    this.currentTime = t;
  }
}

// Translate the audio clock into the game clock.
// gameTimeMs = audioTime * 1000 + calibrationOffsetMs
export function gameTimeMs(clock: ClockSource, calibrationOffsetMs: number): number {
  return clock.currentTime * 1000 + calibrationOffsetMs;
}
