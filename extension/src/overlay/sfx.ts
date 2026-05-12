// Tiny synth for game SFX. We avoid shipping an audio asset by generating a
// short noise burst on the fly. Lazily creates the AudioContext on first use
// so we don't trip Chrome's autoplay policy (each note press IS a user
// gesture, which is enough to satisfy the policy when ctx.resume() runs).

const CLICK_DURATION_S = 0.035;
const CLICK_GAIN = 0.10;        // ~10% volume so it sits under the music
const CLICK_DECAY_S = 0.006;    // sharp, percussive envelope

let ctx: AudioContext | null = null;
let clickBuffer: AudioBuffer | null = null;
let lastClickAt = 0;

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
  gain.gain.value = CLICK_GAIN;
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
  const decaySamples = Math.max(1, sr * CLICK_DECAY_S);
  // White noise with an exponential decay envelope. Sounds like a soft
  // finger-tap on a pad rather than a metronome click, which sits better
  // under music.
  for (let i = 0; i < length; i++) {
    const env = Math.exp(-i / decaySamples);
    data[i] = (Math.random() * 2 - 1) * env;
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
