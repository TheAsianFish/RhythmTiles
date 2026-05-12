// Client-side estimator for chart-generation progress.
//
// The backend doesn't stream progress events (no Server-Sent Events, no
// status endpoint). What we DO know on the client:
//   - When the user clicked Start (we capture performance.now()).
//   - Which ML flags the backend is running in, from /healthz.
// That's enough to predict what stage the pipeline is in based on
// elapsed time and the known per-stage costs of each flag combination.
//
// The numbers below are rough but useful: their goal is to give the
// player something better than a frozen spinner, not to be wall-clock
// accurate. On a hot cache the actual generation might finish in 200ms
// even with all flags on; the UI just won't be visible long enough to
// confuse anyone in that case.

import type { BackendMlFlags } from "@/api/backend-client";

export interface ProgressView {
  stage: string;          // short label, e.g. "Detecting beats"
  hint: string;           // longer explanation under the stage label
  expectedTotal: string;  // human-readable ETA, e.g. "~2-5 min on CPU"
  warn: boolean;          // true when the long Demucs phase is active
}

/** Returns a stage description for the given elapsed time and flag set. */
export function describeProgress(
  elapsedMs: number,
  flags: BackendMlFlags | null,
): ProgressView {
  const demucs = !!flags?.demucsFlag;
  const beatThis = !!flags?.beatThisActive;
  const mert = !!flags?.mertActive;

  // Bucket boundaries are tuned for "what the player should expect to
  // see next". They're not precise (the actual stage runtime depends on
  // song length + CPU speed) but they're the right order of magnitude.
  const s = elapsedMs / 1000;

  // Very first 2 seconds: still connecting / queueing.
  if (s < 2) {
    return {
      stage: "Connecting to backend",
      hint: "Sending request and starting the pipeline.",
      expectedTotal: expectedTotal(demucs, beatThis, mert),
      warn: false,
    };
  }

  // Always-on stages: fetch audio (yt-dlp) takes 5-15s on first request.
  if (s < 15) {
    return {
      stage: "Fetching audio",
      hint: "Pulling the song from YouTube to disk. Skipped on cache hit.",
      expectedTotal: expectedTotal(demucs, beatThis, mert),
      warn: false,
    };
  }

  if (demucs) {
    // Demucs dominates the budget. On CPU, htdemucs is roughly 0.25x
    // realtime, so a 4-minute song takes 16+ minutes. We keep showing
    // "Separating stems" until the request returns or 8 minutes pass.
    // After 8 min we switch to "Almost there" so we don't keep telling
    // the user we're still on the slow step when we've probably moved on.
    if (s < 25) {
      return {
        stage: "Detecting beats",
        hint: "Beat This! is finding bar starts and tempo curves.",
        expectedTotal: expectedTotal(demucs, beatThis, mert),
        warn: false,
      };
    }
    if (s < 8 * 60) {
      return {
        stage: "Separating drums and vocals",
        hint:
          "Demucs is isolating the drum and vocal tracks so notes route by " +
          "instrument. This is the slow step on CPU; on GPU it takes seconds.",
        expectedTotal: expectedTotal(demucs, beatThis, mert),
        warn: true,
      };
    }
    return {
      stage: "Building chart",
      hint:
        "Stems are done, finishing up onsets, sections, lanes, and difficulty.",
      expectedTotal: expectedTotal(demucs, beatThis, mert),
      warn: false,
    };
  }

  // No Demucs. Sub-30s total.
  if (beatThis || mert) {
    if (s < 25) {
      return {
        stage: "Detecting beats and onsets",
        hint: beatThis
          ? "Beat This! finds bar starts; onset detection finds note events."
          : "Onset detection is finding note events from the spectrogram.",
        expectedTotal: expectedTotal(demucs, beatThis, mert),
        warn: false,
      };
    }
    return {
      stage: mert ? "Detecting sections and shaping difficulty" : "Shaping difficulty",
      hint: mert
        ? "MERT is clustering the song into verses/choruses to set per-section density."
        : "Picking which onsets to keep based on the chosen difficulty.",
      expectedTotal: expectedTotal(demucs, beatThis, mert),
      warn: false,
    };
  }

  // Baseline.
  return {
    stage: "Building chart",
    hint: "Detecting onsets, assigning lanes, applying difficulty.",
    expectedTotal: expectedTotal(demucs, beatThis, mert),
    warn: false,
  };
}

/** Human-readable ETA for the given flag set. */
function expectedTotal(demucs: boolean, beatThis: boolean, mert: boolean): string {
  if (demucs) return "~2-5 min on CPU (first time), instant on cache hit";
  if (mert) return "~25-40s on first run, instant on cache hit";
  if (beatThis) return "~20s on first run, instant on cache hit";
  return "~10s on first run, instant on cache hit";
}

/** Format a duration in ms as M:SS. */
export function formatElapsed(ms: number): string {
  const totalS = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(totalS / 60);
  const s = totalS % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}
