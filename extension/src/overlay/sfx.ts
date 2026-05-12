// Tiny synth for game SFX. We avoid shipping an audio asset by generating a
// short osu!mania-style hit tick on the fly. Lazily creates the AudioContext
// on first use so we don't trip Chrome's autoplay policy (each note press IS
// a user gesture, which is enough to satisfy the policy when ctx.resume()
// runs).

const CLICK_DURATION_S = 0.045;
// Runtime-tunable gain. The popup's volume slider calls setHitVolume to
// change this without rebuilding.
const DEFAULT_CLICK_GAIN = 0.40;
// Osu!mania normal-hitnormal: a short tonal tick around 1.8kHz with a fast
// noise transient at the front. Tuned by ear against the stock skin.
const TICK_FREQ_HZ = 1800;
const TICK_DECAY_S = 0.012;
const NOISE_DECAY_S = 0.004;
const NOISE_MIX = 0.35;

let ctx: AudioContext | null = null;
let clickBuffer: AudioBuffer | null = null;
let lastClickAt = 0;
let currentGain = DEFAULT_CLICK_GAIN;

/**
 * Set the hit-click gain (0.0 silent, 1.0 full). Values outside the range
 * are clamped. Persists across calls. Called from overlay-main with the
 * stored UserSettings.sfxVolume at game start.
 */
export function setHitVolume(v: number): void {
  if (Number.isNaN(v)) return;
  currentGain = Math.max(0, Math.min(1, v));
}

// Cap consecutive click rate to avoid clipping when several notes hit at
// once. Two presses within MIN_INTERVAL_MS still play, but a stream of
// presses faster than this throttles down.
const MIN_INTERVAL_MS = 8;

export function playHitClick(): void {
  const now = performance.now();
  if (now - lastClickAt < MIN_INTERVAL_MS) return;
  lastClickAt = now;

  const audioCtx = ensureContext();
  if (!audioCtx) return;
  if (audioCtx.state === "suspended") {
    // resume returns a promise; we don't await because the next click will
    // pick up where this one drops if the context isn't yet running.
    void audioCtx.resume();
  }
  const buffer = ensureClickBuffer(audioCtx);
  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  const gain = audioCtx.createGain();
  gain.gain.value = currentGain;
  src.connect(gain).connect(audioCtx.destination);
  src.start();
}

function ensureContext(): AudioContext | null {
  if (ctx) return ctx;
  const Ctor: typeof AudioContext | undefined =
    (globalThis as { AudioContext?: typeof AudioContext }).AudioContext ??
    (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    ctx = new Ctor();
  } catch {
    return null;
  }
  return ctx;
}

function ensureClickBuffer(audioCtx: AudioContext): AudioBuffer {
  if (clickBuffer) return clickBuffer;
  const sr = audioCtx.sampleRate;
  const length = Math.floor(sr * CLICK_DURATION_S);
  const buf = audioCtx.createBuffer(1, length, sr);
  const data = buf.getChannelData(0);
  // Damped sine tick + very short noise transient at the front. The tonal
  // tick gives the recognizable osu!mania pitch, the noise gives it the
  // initial snap. Both share i=0 as t=0 and decay exponentially.
  const omega = 2 * Math.PI * TICK_FREQ_HZ;
  const tickDecaySamples = Math.max(1, sr * TICK_DECAY_S);
  const noiseDecaySamples = Math.max(1, sr * NOISE_DECAY_S);
  let peak = 0;
  for (let i = 0; i < length; i++) {
    const t = i / sr;
    const tickEnv = Math.exp(-i / tickDecaySamples);
    const noiseEnv = Math.exp(-i / noiseDecaySamples);
    const tick = Math.sin(omega * t) * tickEnv;
    const noise = (Math.random() * 2 - 1) * noiseEnv * NOISE_MIX;
    const v = tick + noise;
    data[i] = v;
    if (Math.abs(v) > peak) peak = Math.abs(v);
  }
  // Normalize so the loudest sample sits at -3 dBFS regardless of how
  // constructive interference fell between the tick and noise terms.
  if (peak > 0) {
    const target = 0.708;
    const k = target / peak;
    for (let i = 0; i < length; i++) data[i] *= k;
  }
  clickBuffer = buf;
  return buf;
}

// Test-only resets.
export function _resetForTests(): void {
  ctx = null;
  clickBuffer = null;
  lastClickAt = 0;
}
