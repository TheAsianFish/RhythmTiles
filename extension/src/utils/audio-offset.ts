// Latency calibration / manual offset (ms) applied to playback time for judgment.

export const AUDIO_OFFSET_MS_MIN = -500;
export const AUDIO_OFFSET_MS_MAX = 500;

export function clampAudioOffsetMs(n: number): number {
  const r = Math.round(Number.isFinite(n) ? n : 0);
  if (r < AUDIO_OFFSET_MS_MIN) return AUDIO_OFFSET_MS_MIN;
  if (r > AUDIO_OFFSET_MS_MAX) return AUDIO_OFFSET_MS_MAX;
  return r;
}
