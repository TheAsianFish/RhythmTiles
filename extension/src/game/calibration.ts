// Pure offset math for the calibration flow.
//
// The flow records `expectedAudioMs` (when each click happened in audio time)
// and `actualPerfMs` (performance.now() at each user tap). After dropping
// warm-up taps, we compute the mean signed delta.
//
// A positive offset means the user tends to press AFTER the click, so we add
// the offset to game time to keep judgment fair.

export interface CalibrationInputs {
  expectedAudioMs: number[];   // when each beat was scheduled to play
  actualPerfMs: number[];      // performance.now() of each registered tap
  // Anchor used to align both clocks. Typically performance.now() at the
  // moment audio time 0 corresponds to.
  audioStartPerfMs: number;
  warmupCount?: number;        // taps to drop from the front (default 4)
}

export interface CalibrationResult {
  offsetMs: number;             // mean of usable deltas
  usableTaps: number;
  rejectedTaps: number;
  stdDevMs: number;
}

const MAX_PAIR_DELTA_MS = 250;   // taps further than this from any beat are noise

export function computeCalibrationOffset(input: CalibrationInputs): CalibrationResult {
  const warmup = input.warmupCount ?? 4;
  const taps = input.actualPerfMs.slice(warmup);
  if (taps.length === 0 || input.expectedAudioMs.length === 0) {
    return { offsetMs: 0, usableTaps: 0, rejectedTaps: input.actualPerfMs.length, stdDevMs: 0 };
  }

  // Translate each tap into audio-relative ms, then pair to the nearest beat.
  const tapsAudioMs = taps.map((p) => p - input.audioStartPerfMs);

  const deltas: number[] = [];
  let rejected = input.actualPerfMs.length - taps.length;

  for (const tapMs of tapsAudioMs) {
    let bestDelta = Infinity;
    for (const beat of input.expectedAudioMs) {
      const d = tapMs - beat;
      if (Math.abs(d) < Math.abs(bestDelta)) bestDelta = d;
    }
    if (Math.abs(bestDelta) > MAX_PAIR_DELTA_MS) {
      rejected++;
      continue;
    }
    deltas.push(bestDelta);
  }

  if (deltas.length === 0) {
    return { offsetMs: 0, usableTaps: 0, rejectedTaps: rejected, stdDevMs: 0 };
  }

  const mean = deltas.reduce((a, b) => a + b, 0) / deltas.length;
  const variance = deltas.reduce((acc, d) => acc + (d - mean) ** 2, 0) / deltas.length;
  return {
    offsetMs: -mean, // positive => user is late, so game time needs +offset to align
    usableTaps: deltas.length,
    rejectedTaps: rejected,
    stdDevMs: Math.sqrt(variance),
  };
}

// Helper to enumerate `count` beats at a given bpm, in audio-ms.
export function metronomeBeats(bpm: number, count: number, startMs = 0): number[] {
  const period = 60_000 / bpm;
  const out: number[] = [];
  for (let i = 0; i < count; i++) out.push(startMs + i * period);
  return out;
}
