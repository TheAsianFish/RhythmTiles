import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { Chart, Difficulty } from "@/types/chart";
import { CanvasRenderer, DEFAULT_RENDER_CONFIG } from "./canvas-renderer";
import { playHitClick, setHitVolume } from "./sfx";
import { GameLoop } from "@/game/loop";
import type { ClockSource } from "@/game/clock";
import { accuracyPercent } from "@/game/scoring";
import { hitWindowsForOD, type Judgment, type ScoreState } from "@/game/types";
import { pingHealthDetailed, type BackendMlFlags } from "@/api/backend-client";
import {
  loadSettings,
  saveSettings,
  loadBestScore,
  saveBestScore,
  DEFAULT_SETTINGS,
  type UserSettings,
} from "@/utils/storage";

// React's useEffect fires asynchronously after paint. postMessage from the
// parent can arrive before the listener is registered, dropping BB_LOAD_CHART.
// Buffer messages immediately so the first useEffect can drain them.
const _pendingMsgs: unknown[] = [];
const _earlyListener = (ev: MessageEvent) => {
  if (ev.data && typeof ev.data === "object") _pendingMsgs.push(ev.data);
};
window.addEventListener("message", _earlyListener);

// A clock that's fed by postMessage ticks from the parent (content script).
// Ticks arrive every ~16ms; between ticks we interpolate using performance.now()
// so the game loop sees sub-millisecond precision instead of a value stale by
// up to 16ms. Interpolation is clamped to MAX_EXTRAPOLATE_MS forward so a
// dropped or delayed tick doesn't let the clock run off into the future.
class BridgedClock implements ClockSource {
  private static MAX_EXTRAPOLATE_MS = 50;
  private baseCurrentTime = 0;
  private basePerfMs = 0;
  private _paused = true;

  set(t: number, paused: boolean) {
    this.baseCurrentTime = t;
    this.basePerfMs = performance.now();
    this._paused = paused;
  }

  get paused(): boolean {
    return this._paused;
  }

  set paused(v: boolean) {
    // Resume transition: rebase perfMs so interpolation doesn't add the
    // pause interval to currentTime before the next tick arrives.
    if (this._paused && !v) {
      this.basePerfMs = performance.now();
    }
    this._paused = v;
  }

  get currentTime(): number {
    if (this._paused) return this.baseCurrentTime;
    const delta = performance.now() - this.basePerfMs;
    const clamped = Math.min(delta, BridgedClock.MAX_EXTRAPOLATE_MS);
    return this.baseCurrentTime + clamped / 1000;
  }
}

interface ResultsPayload {
  score: ScoreState;
  accuracy: number;
  prevBest?: { score: number; accuracy: number } | null;
}

// In-overlay menu shown when the user clicks the burger button or when a
// new song is auto-detected. Mirrors the popup's settings UI so the player
// can change difficulty / bindings / opacity without reloading the
// extension. Clicking Start posts BB_REQUEST_NEW_CHART; the new chart
// arrives via BB_LOAD_CHART and the menu auto-closes.
// Maps the chip label to a CSS variant. Warning / error states get a
// different color so the user notices they should restart the backend.
function chipClassFor(label: string): string {
  if (label === "Down") return "mode-chip-error";
  if (label === "Restart needed" || label === "ML flag, inactive") return "mode-chip-warn";
  if (label === "Checking") return "mode-chip-neutral";
  return "mode-chip-ok";
}

// Maps the backend's reachability + ml flags to a compact symbolic mode chip.
// Five buckets:
//   ✕  Backend unreachable (ping failed)
//   ⌛ Reachable but no ml field (server is from before this commit; restart
//      to expose the indicator)
//   ≋  Baseline (librosa, no ML)
//   ♫  Beat This! only
//   ♫◓ Beat This! + Demucs
//   ⚠  Flag on but package not active (silent fallback)
function modeChipFor(
  ml: BackendMlFlags | null,
  reachable: boolean | null,
): { symbol: string; label: string; title: string } {
  if (reachable === null) {
    return {
      symbol: "…",
      label: "Checking",
      title: "Querying /healthz to determine the active pipeline.",
    };
  }
  if (!reachable) {
    return {
      symbol: "✕",
      label: "Down",
      title: "Backend not reachable. Check the server is running at the configured URL.",
    };
  }
  if (!ml) {
    // Server responded but didn't include the ml block. This is the
    // pre-mode-indicator build of the backend. The chip explicitly says
    // "restart" so the user knows what to do.
    return {
      symbol: "⌛",
      label: "Restart needed",
      title:
        "Backend is reachable but is running an older build that doesn't expose its ML " +
        "flags. Restart uvicorn to enable the live mode indicator.",
    };
  }
  const beat = ml.beatThisActive;
  const beatFlagOnly = ml.beatThisFlag && !ml.beatThisActive;
  const demucs = ml.demucsFlag;
  const mertActive = !!ml.mertActive;
  const mertFlagOnly = !!ml.mertFlag && !mertActive;
  // Trailing "+✦" when MERT is active. The base chip already conveys
  // beat / stem state; the trailing sigil signals section-aware density.
  const mertSuffix = mertActive ? "✦" : "";
  const mertTitle = mertActive
    ? " MERT section detection drives per-section density (verses sparse, choruses dense)."
    : "";
  if (!beat && !beatFlagOnly && !demucs && !mertActive && !mertFlagOnly) {
    return {
      symbol: "≋",
      label: "Baseline",
      title: "Heuristic pipeline: librosa beats, full-mix onsets, centroid-based lane routing.",
    };
  }
  if (beat && demucs) {
    return {
      symbol: `♫◓${mertSuffix}`,
      label: mertActive ? "ML-max" : "ML-full",
      title:
        "Beat This! beats + downbeats + Demucs per-stem onsets. Drum line on left hand, vocal melody on right." +
        mertTitle,
    };
  }
  if (beat) {
    return {
      symbol: `♫${mertSuffix}`,
      label: mertActive ? "ML-light + sections" : "ML-light",
      title:
        "Beat This! beats and downbeats; full-mix onsets with centroid routing." +
        mertTitle,
    };
  }
  if (demucs && !beat) {
    return {
      symbol: `◓${mertSuffix}`,
      label: "Demucs only",
      title:
        "Demucs per-stem onsets active; librosa beat tracker (no downbeats)." +
        mertTitle,
    };
  }
  if (mertActive) {
    return {
      symbol: "✦",
      label: "MERT only",
      title:
        "MERT section detection drives density; baseline beats + onsets otherwise.",
    };
  }
  // Flag on but inference path not available (package missing, device error).
  return {
    symbol: "⚠",
    label: "ML flag, inactive",
    title:
      "An ML flag is set but the corresponding package is not importable. " +
      "Pipeline silently uses the baseline.",
  };
}

function MenuPanel({
  difficulty,
  onDifficultyChange,
  settings,
  updateSetting,
  onStart,
  onCancel,
  loading,
  error,
  backendMl,
  backendReachable,
}: {
  difficulty: Difficulty;
  onDifficultyChange: (d: Difficulty) => void;
  settings: UserSettings;
  updateSetting: <K extends keyof UserSettings>(key: K, value: UserSettings[K]) => void;
  onStart: () => void;
  onCancel?: () => void;
  loading: boolean;
  error: string | null;
  backendMl: BackendMlFlags | null;
  backendReachable: boolean | null;
}) {
  const w = hitWindowsForOD(settings.overallDifficulty);
  const mode = modeChipFor(backendMl, backendReachable);
  return (
    <div className="menu-overlay" role="dialog" aria-label="Menu">
      <div className="menu-card">
        <div className="menu-card-header">
          <h2>Menu</h2>
          <div
            className={`mode-chip ${chipClassFor(mode.label)}`}
            title={mode.title}
            aria-label={mode.title}
          >
            <span className="mode-chip-sym">{mode.symbol}</span>
            <span className="mode-chip-label">{mode.label}</span>
          </div>
        </div>
        <div className="menu-row">
          <label htmlFor="m-diff">Difficulty</label>
          <select
            id="m-diff"
            value={difficulty}
            onChange={(e) => onDifficultyChange(e.target.value as Difficulty)}
          >
            <option value="easy">Easy</option>
            <option value="normal">Normal</option>
            <option value="hard">Hard</option>
            <option value="expert">Expert</option>
          </select>
        </div>

        <div className="menu-row">
          <label htmlFor="m-od">Timing (OD)</label>
          <select
            id="m-od"
            value={settings.overallDifficulty}
            onChange={(e) => updateSetting("overallDifficulty", Number(e.target.value))}
          >
            <option value={5}>Lenient (5)</option>
            <option value={7}>Standard (7)</option>
            <option value={8}>Challenging (8)</option>
            <option value={9}>Strict (9)</option>
            <option value={10}>Extreme (10)</option>
          </select>
        </div>
        <div className="menu-hint">
          MAX &plusmn;{w.max.toFixed(1)}ms, GREAT &plusmn;{w.great.toFixed(0)}ms,
          GOOD &plusmn;{w.good.toFixed(0)}ms
        </div>

        <div className="menu-row">
          <label htmlFor="m-speed">Note speed</label>
          <span className="menu-value">{settings.noteSpeed.toFixed(2)}x</span>
        </div>
        <input
          id="m-speed"
          type="range"
          min={0.5}
          max={2.0}
          step={0.05}
          value={settings.noteSpeed}
          onChange={(e) => updateSetting("noteSpeed", Number(e.target.value))}
        />

        <div className="menu-row">
          <label htmlFor="m-op">Panel opacity</label>
          <span className="menu-value">{Math.round(settings.opacity * 100)}%</span>
        </div>
        <input
          id="m-op"
          type="range"
          min={0.4}
          max={1.0}
          step={0.05}
          value={settings.opacity}
          onChange={(e) => updateSetting("opacity", Number(e.target.value))}
        />

        <div className="menu-row">
          <label htmlFor="m-sfx">Hit sound</label>
          <input
            id="m-sfx"
            type="checkbox"
            checked={settings.sfxEnabled}
            onChange={(e) => updateSetting("sfxEnabled", e.target.checked)}
          />
        </div>

        <div className="menu-row">
          <label htmlFor="m-vol">Hit volume</label>
          <span className="menu-value">{Math.round(settings.sfxVolume * 100)}%</span>
        </div>
        <input
          id="m-vol"
          type="range"
          min={0}
          max={1}
          step={0.05}
          disabled={!settings.sfxEnabled}
          value={settings.sfxVolume}
          onChange={(e) => updateSetting("sfxVolume", Number(e.target.value))}
        />

        {error && <div className="menu-error">{error}</div>}

        <div className="menu-actions">
          {onCancel && (
            <button className="secondary" disabled={loading} onClick={onCancel}>
              Cancel
            </button>
          )}
          <button className="primary" disabled={loading} onClick={onStart}>
            {loading ? "Generating..." : "Start"}
          </button>
        </div>
        <div className="menu-hint">
          Key bindings and calibration live in the extension popup.
        </div>
      </div>
    </div>
  );
}

function App() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<CanvasRenderer | null>(null);
  const clockRef = useRef<BridgedClock>(new BridgedClock());
  const loopRef = useRef<GameLoop | null>(null);
  const chartTimeoutRef = useRef<number | null>(null);
  const [results, setResults] = useState<ResultsPayload | null>(null);
  const [chartReady, setChartReady] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState("Waiting for content script...");
  const [chart, setChart] = useState<Chart | null>(null);
  const [difficulty, setDifficulty] = useState<Difficulty>("normal");
  const [videoId, setVideoId] = useState<string>("");
  // Menu mode (burger button). When open, the canvas dims and an inline
  // settings panel is shown. Lets the user change difficulty and other
  // settings without reloading the whole extension. The game (if any) is
  // paused while the menu is open.
  const [menuOpen, setMenuOpen] = useState(false);
  // Editable copy of UserSettings while the menu is open. Committed via
  // saveSettings on Start; bindings/opacity/etc apply to the next loop
  // mount (when chart re-arrives after a regenerate).
  const [menuSettings, setMenuSettings] = useState<UserSettings>(DEFAULT_SETTINGS);
  // Pending difficulty selection inside the menu. Defaults to the current
  // difficulty; on Start we send this to the content script which re-fetches
  // the chart and posts BB_LOAD_CHART back.
  const [menuDifficulty, setMenuDifficulty] = useState<Difficulty>("normal");
  // True between "user clicked Start in menu" and "new chart arrived".
  const [menuLoading, setMenuLoading] = useState(false);
  // Snapshot of the backend's active ML flags, fetched when the menu opens.
  // null while the request is in flight or if the backend is unreachable.
  const [backendMl, setBackendMl] = useState<BackendMlFlags | null>(null);
  // Reachability separate from ml info, so the chip can distinguish
  // "backend down" from "backend up but older build with no ml field".
  const [backendReachable, setBackendReachable] = useState<boolean | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragOriginRef = useRef<{ x: number; y: number } | null>(null);
  // Countdown state: null when no countdown is active, 3/2/1/0 (GO!) otherwise.
  // Driven by setTimeout chain in scheduleCountdown.
  const [countdown, setCountdown] = useState<number | null>(null);
  const countdownTimerRef = useRef<number | null>(null);
  // True while we are running the resume-from-pause sequence. Used to ignore
  // the BB_VIDEO_PAUSED / BB_VIDEO_PLAYING ricochets we trigger ourselves.
  const inCountdownRef = useRef(false);
  // Tracks whether the video has ever played for this chart. The first
  // BB_VIDEO_PLAYING skips the countdown (initial start, not a resume).
  const everPlayedRef = useRef(false);
  // Set by restart(): the next chart re-mount should seek the video to 0
  // and start a countdown rather than waiting for the user to press play.
  // Read once by the loop-boot useEffect, cleared immediately after.
  const pendingReplayRef = useRef(false);

  // Resize handler. Canvas is flex:1 inside a flex column, so the size we
  // want is its rendered bounding box, not the iframe viewport.
  useEffect(() => {
    if (!canvasRef.current) return;
    rendererRef.current = new CanvasRenderer(canvasRef.current, DEFAULT_RENDER_CONFIG);
    const r = rendererRef.current;
    const measure = () => {
      const cnv = canvasRef.current!;
      const rect = cnv.getBoundingClientRect();
      r.resize(rect.width, rect.height);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(canvasRef.current);
    return () => ro.disconnect();
  }, []);

  // Drag the panel by posting deltas to the content script. We use screenX/Y
  // because clientX/Y are iframe-relative and the iframe moves while we drag.
  function onHeaderMouseDown(e: React.MouseEvent) {
    if (e.button !== 0) return;
    setDragging(true);
    dragOriginRef.current = { x: e.screenX, y: e.screenY };
    e.preventDefault();
  }

  useEffect(() => {
    if (!dragging) return;
    function onMove(ev: MouseEvent) {
      if (!dragOriginRef.current) return;
      const dx = ev.screenX - dragOriginRef.current.x;
      const dy = ev.screenY - dragOriginRef.current.y;
      if (dx === 0 && dy === 0) return;
      window.parent.postMessage({ type: "BB_DRAG_BY", dx, dy }, "*");
      dragOriginRef.current = { x: ev.screenX, y: ev.screenY };
    }
    function onUp() {
      setDragging(false);
      dragOriginRef.current = null;
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragging]);

  // Pause toggle from inside the iframe. The content script installs a
  // global P handler on the page window, but that only fires when focus
  // is on the YouTube page, not when the user clicked into the overlay.
  // We install a mirror handler here that forwards to the parent so P
  // works no matter who has focus. Only fires when KeyP is NOT bound to
  // a lane (we don't override the lane press if the user remapped to P).
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.code !== "KeyP") return;
      const tgt = ev.target as HTMLElement | null;
      if (tgt && (tgt.tagName === "INPUT" || tgt.tagName === "TEXTAREA" || tgt.isContentEditable)) {
        return;
      }
      ev.preventDefault();
      ev.stopImmediatePropagation();
      if (ev.repeat) return;
      window.parent.postMessage({ type: "BB_REQUEST_VIDEO_TOGGLE" }, "*");
    }
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true } as any);
  }, []);

  // Listen to parent messages.
  useEffect(() => {
    function handler(msg: unknown) {
      if (!msg || typeof msg !== "object") return;
      const m = msg as Record<string, unknown>;
      switch (m.type) {
        case "BB_CLOCK_TICK":
          clockRef.current.set(m.currentTime as number, !!m.paused);
          break;
        case "BB_LOAD_CHART": {
          console.log("[BeatBridge] overlay received BB_LOAD_CHART");
          if (chartTimeoutRef.current !== null) {
            clearTimeout(chartTimeoutRef.current);
            chartTimeoutRef.current = null;
          }
          setErrorMsg(null);
          setStatusMsg("Chart received, starting game...");
          const incomingDifficulty = (m.difficulty as Difficulty) || "normal";
          setChart(m.chart as Chart);
          setDifficulty(incomingDifficulty);
          setMenuDifficulty(incomingDifficulty);
          setVideoId(m.videoId as string);
          setChartReady(true);
          // Close the menu if it was open waiting for this fetch. Reset the
          // ever-played flag so the first play (post-load) skips the auto
          // countdown (the initial start, not a resume).
          setMenuOpen(false);
          setMenuLoading(false);
          everPlayedRef.current = false;
          break;
        }
        case "BB_CHART_ERROR": {
          // Posted by the content script when a chart fetch (initial or
          // menu-driven regenerate) fails. Show the error inside the menu
          // so the user can adjust settings and retry.
          setMenuLoading(false);
          setErrorMsg((m.error as string) || "Chart generation failed.");
          break;
        }
        case "BB_NEW_VIDEO": {
          // SPA navigation: YouTube changed the video without a page reload.
          // We keep the overlay open and drop into the menu state with the
          // previous difficulty pre-selected; the user reviews settings and
          // clicks Start to generate a chart for the new song. The video
          // is already paused (content script paused it before sending us
          // this message).
          console.log("[BeatBridge] overlay received BB_NEW_VIDEO", m.videoId);
          cancelCountdown();
          loopRef.current?.pause();
          setResults(null);
          setChart(null);
          setChartReady(false);
          setVideoId((m.videoId as string) || "");
          everPlayedRef.current = false;
          // Open the menu pre-loaded with current settings and the
          // last-used difficulty. The user clicks Start when they're
          // ready to commit to a chart fetch.
          void (async () => {
            const s = await loadSettings();
            setMenuSettings(s);
            setMenuOpen(true);
            setMenuLoading(false);
            setErrorMsg(null);
            void refreshBackendMode();
          })();
          break;
        }
        case "BB_VIDEO_PAUSED":
          clockRef.current.paused = true;
          // Ignore the pause we triggered ourselves to run the countdown.
          if (!inCountdownRef.current) loopRef.current?.pause();
          break;
        case "BB_VIDEO_PLAYING":
          clockRef.current.paused = false;
          // Three cases:
          //   1. First play of this chart: just resume the loop.
          //   2. We initiated this play to end the countdown: resume the loop,
          //      clear the countdown flag.
          //   3. User unpaused mid-song: kick off a 3-2-1 countdown over the
          //      receptor area before resuming.
          if (inCountdownRef.current) {
            inCountdownRef.current = false;
            loopRef.current?.resume();
          } else if (!everPlayedRef.current) {
            everPlayedRef.current = true;
            loopRef.current?.resume();
          } else {
            scheduleCountdown();
          }
          break;
        case "BB_VIDEO_SEEKED": {
          const seekTime = m.currentTime as number;
          const wasPlaying = !!m.wasPlaying;
          // Rebase the clock and the in-game note cursor at the new position
          // BEFORE deciding whether to run a countdown, so the countdown runs
          // against the post-seek state.
          clockRef.current.set(seekTime, !wasPlaying);
          loopRef.current?.seekToVideoTime(seekTime);
          rendererRef.current?.resetAnim();
          // Any in-flight countdown is now stale (its target time is wrong).
          cancelCountdown();
          // A seek while playing always re-runs the countdown. Pause-state
          // seeks just update position; the user will get a countdown on
          // next unpause via the existing mid-song-resume path.
          if (wasPlaying) {
            // Mark "everPlayed" so the resume after the countdown's internal
            // play() does not get treated as the first start (which would
            // skip the countdown). scheduleCountdown handles the pause +
            // 3-2-1 + resume sequence end-to-end.
            everPlayedRef.current = true;
            scheduleCountdown();
          }
          break;
        }
        case "BB_KEY_DOWN":
          loopRef.current?.injectKey(m.code as string, "press", m.perfMs as number);
          break;
        case "BB_KEY_UP":
          loopRef.current?.injectKey(m.code as string, "release", m.perfMs as number);
          break;
      }
    }
    setStatusMsg("Overlay ready, waiting for chart...");

    // Drain messages that arrived before this effect ran, then switch to live listener.
    _pendingMsgs.splice(0).forEach(handler);
    window.removeEventListener("message", _earlyListener);
    const evHandler = (ev: MessageEvent) => handler(ev.data);
    window.addEventListener("message", evHandler);

    // Tell the content script our listener is registered and ready.
    console.log("[BeatBridge] overlay ready, sending BB_OVERLAY_READY");
    window.parent.postMessage({ type: "BB_OVERLAY_READY" }, "*");

    // If no chart arrives within 15s, show a useful error.
    chartTimeoutRef.current = window.setTimeout(() => {
      chartTimeoutRef.current = null;
      setErrorMsg(
        "Chart not received after 15s. Open DevTools on the YouTube tab and check for [BeatBridge] logs.",
      );
    }, 15000);

    return () => {
      window.removeEventListener("message", evHandler);
      if (chartTimeoutRef.current !== null) {
        clearTimeout(chartTimeoutRef.current);
        chartTimeoutRef.current = null;
      }
    };
  }, []);

  // Boot the loop when chart arrives.
  useEffect(() => {
    if (!chart || !rendererRef.current) return;
    let cancelled = false;

    (async () => {
      const settings = (await loadSettings()) ?? DEFAULT_SETTINGS;
      if (cancelled) return;

      // Apply panel opacity (CSS variable, see overlay.css), note speed
      // scaling on the renderer, and the hit-sound gain. These come from
      // chrome.storage.local on every game start so a popup change picks
      // up on the next play without an explicit reload.
      document.documentElement.style.setProperty("--panel-alpha", String(settings.opacity));
      rendererRef.current?.setConfig({
        pixelsPerMs: DEFAULT_RENDER_CONFIG.pixelsPerMs * settings.noteSpeed,
      });
      setHitVolume(settings.sfxVolume);

      // Load previous best so the results screen can show "new best!" if we beat it.
      const prevBest = videoId ? await loadBestScore(videoId, difficulty) : null;
      if (cancelled) return;

      const lastJudgmentRef: { current?: { judgment: Judgment; deltaMs: number; atMs: number } } = {};
      // Ring buffer of the last 12 hit deltas, used to draw the timing-bar
      // calibration meter. Newest entries pushed at the end.
      const recentDeltas: Array<{ deltaMs: number; tier: string }> = [];
      const RECENT_DELTAS_MAX = 12;
      const sfxEnabled = settings.sfxEnabled;
      const loop = new GameLoop({
        chart,
        clock: clockRef.current,
        bindings: { codes: settings.bindings },
        audioLatencyOffsetMs: settings.audioLatencyOffsetMs,
        overallDifficulty: settings.overallDifficulty,
        callbacks: {
          onHit: ({ judgment, deltaMs }) => {
            if (sfxEnabled) playHitClick();
            recentDeltas.push({ deltaMs, tier: judgment });
            if (recentDeltas.length > RECENT_DELTAS_MAX) recentDeltas.shift();
          },
          onFrame: ({ gameMs, notes, score, lastJudgment, pressedLanes }) => {
            if (lastJudgment) lastJudgmentRef.current = lastJudgment;
            rendererRef.current?.draw({
              gameMs,
              notes,
              score: score.score,
              combo: score.combo,
              multiplier: score.multiplier,
              accuracyPercent: accuracyPercent(score),
              lastJudgment: lastJudgmentRef.current,
              pressedLanes,
              recentDeltas,
            });
          },
          onFinish: ({ score, accuracyPercent: acc }) => {
            setResults({
              score,
              accuracy: acc,
              prevBest: prevBest
                ? { score: prevBest.bestScore, accuracy: prevBest.bestAccuracy }
                : null,
            });
            if (videoId) {
              void saveBestScore({
                videoId,
                difficulty,
                bestScore: score.score,
                bestAccuracy: acc,
                updatedAt: new Date().toISOString(),
              });
            }
          },
        },
      });
      // Replay path: start the loop PAUSED so its first tick doesn't fire
      // synchronously against the stale end-of-song clock (which would mark
      // every note missed and re-fire onFinish, painting the results card
      // back on top of the fresh replay). The countdown's resume() at the
      // end of 3-2-1 starts the rAF for real.
      const isReplay = pendingReplayRef.current;
      loop.start(window, { startPaused: isReplay });
      loopRef.current = loop;

      if (isReplay) {
        pendingReplayRef.current = false;
        everPlayedRef.current = true; // ensure the play() at end of countdown does not skip it
        // Defensive: also reset cursor/notes/score against gameMs=0 so even
        // if a stray BB_CLOCK_TICK with the stale value sneaks in, the loop
        // is in a clean state.
        loop.seekToVideoTime(0);
        window.setTimeout(() => {
          if (cancelled) return;
          window.parent.postMessage({ type: "BB_REQUEST_VIDEO_SEEK", t: 0 }, "*");
          // scheduleCountdown pauses the video (no-op if already), ticks
          // 3-2-1, plays. The play round-trip resumes the loop via the
          // BB_VIDEO_PLAYING handler.
          window.setTimeout(() => {
            if (cancelled) return;
            scheduleCountdown();
          }, 80);
        }, 30);
      }
    })();

    return () => {
      cancelled = true;
      loopRef.current?.stop(window);
      loopRef.current = null;
    };
  }, [chart, videoId, difficulty]);

  // 3-2-1-GO sequence. While running, the YouTube video is paused (we ask
  // the content script to do it) and the game loop stays paused too. When
  // the countdown finishes we ask the content script to play the video,
  // which round-trips back through BB_VIDEO_PLAYING and resumes the loop.
  function scheduleCountdown(): void {
    if (inCountdownRef.current) return; // already running
    inCountdownRef.current = true;
    window.parent.postMessage({ type: "BB_REQUEST_VIDEO_PAUSE" }, "*");
    loopRef.current?.pause();
    rendererRef.current?.resetAnim();
    const STEP_MS = 700;
    const sequence = [3, 2, 1, 0]; // 0 renders as "GO!"
    let i = 0;
    const tick = () => {
      const v = sequence[i++];
      if (v === undefined) {
        setCountdown(null);
        countdownTimerRef.current = null;
        window.parent.postMessage({ type: "BB_REQUEST_VIDEO_PLAY" }, "*");
        return;
      }
      setCountdown(v);
      countdownTimerRef.current = window.setTimeout(tick, STEP_MS);
    };
    tick();
  }

  // Cancel an in-flight countdown (e.g. if the user pauses again mid-countdown).
  function cancelCountdown(): void {
    if (countdownTimerRef.current !== null) {
      clearTimeout(countdownTimerRef.current);
      countdownTimerRef.current = null;
    }
    setCountdown(null);
    inCountdownRef.current = false;
  }

  function close() {
    cancelCountdown();
    loopRef.current?.stop(window);
    window.parent.postMessage({ type: "BB_OVERLAY_CLOSE" }, "*");
  }

  async function openMenu() {
    // Pause the underlying video and the game loop. Snapshot the current
    // settings into the menu's editable copy so the user sees the same
    // values the next loop mount would pick up. Also kick off a backend
    // health ping so the menu can show which ML mode the server is in.
    cancelCountdown();
    window.parent.postMessage({ type: "BB_REQUEST_VIDEO_PAUSE" }, "*");
    const s = await loadSettings();
    setMenuSettings(s);
    setMenuDifficulty(difficulty);
    setErrorMsg(null);
    setMenuOpen(true);
    void refreshBackendMode();
  }

  async function refreshBackendMode() {
    // Re-fetch each time we (re)show the menu so the indicator reflects
    // any flag flip the user made on the backend side since last open.
    setBackendMl(null);
    setBackendReachable(null);
    const ping = await pingHealthDetailed();
    setBackendReachable(ping.ok);
    setBackendMl(ping.ok && ping.ml ? ping.ml : null);
  }

  function closeMenuWithoutApplying() {
    // Cancel button on the menu. Re-applying mid-game without a regenerate
    // is fine if the user only twiddled visual settings (opacity, sfx);
    // those pick up on the next loop mount. If they changed bindings,
    // they'll need to actually click Start to apply. Keep this behaviour
    // simple: close and resume; the visual settings persist via storage.
    setMenuOpen(false);
    setErrorMsg(null);
  }

  async function startFromMenu() {
    // Persist edited settings, then ask the content script to (re)fetch the
    // chart at the chosen difficulty. The new chart arrives via BB_LOAD_CHART
    // which closes the menu and sets up the loop. The first BB_VIDEO_PLAYING
    // for the fresh chart skips the countdown (initial start, not resume),
    // matching the popup's Start Game flow.
    await saveSettings(menuSettings);
    setMenuLoading(true);
    setErrorMsg(null);
    window.parent.postMessage(
      { type: "BB_REQUEST_NEW_CHART", difficulty: menuDifficulty },
      "*",
    );
  }

  function updateMenuSetting<K extends keyof UserSettings>(key: K, value: UserSettings[K]) {
    setMenuSettings((prev) => ({ ...prev, [key]: value }));
  }

  function restart() {
    // Replay: rebuild the loop with a fresh score state, seek the video
    // back to 0, run a 3-2-1 countdown, then resume. The loop-boot
    // useEffect reads pendingReplayRef and orchestrates the seek +
    // countdown once the new loop is mounted.
    cancelCountdown();
    setResults(null);
    setChartReady(false);
    pendingReplayRef.current = true;
    // Force the local clock to 0 paused immediately. Without this the
    // BridgedClock still reports the end-of-song time until the next
    // BB_CLOCK_TICK arrives, and the new loop's first paint sees the
    // stale value. The loop also starts paused as a belt-and-braces,
    // but this prevents the renderer from flashing the song-end state
    // for a frame before the pause takes effect.
    clockRef.current.set(0, true);
    // Pause the video up front so it does not keep playing audio from the
    // previous position while the new loop spins up.
    window.parent.postMessage({ type: "BB_REQUEST_VIDEO_PAUSE" }, "*");
    const c = chart;
    setChart(null);
    setTimeout(() => setChart(c), 30);
  }

  return (
    <>
      <div
        className={`header ${dragging ? "dragging" : ""}`}
        onMouseDown={onHeaderMouseDown}
      >
        <div className="grip"><span /><span /><span /></div>
        <div className="title">BeatBridge</div>
        {chart && (
          <div className="difficulty-chip" title={`Difficulty: ${difficulty}`}>
            {difficulty.toUpperCase()}
          </div>
        )}
        <div className="spacer" />
        {results && <button onClick={restart}>Replay</button>}
        <button
          className="menu-btn"
          onClick={() => (menuOpen ? closeMenuWithoutApplying() : void openMenu())}
          title={menuOpen ? "Back to game" : "Menu / settings"}
          aria-label="Toggle menu"
        >
          {menuOpen ? "←" : "☰"}
        </button>
        <button onClick={close}>Close</button>
      </div>
      <canvas ref={canvasRef} />
      {countdown !== null && (
        <div className="countdown" aria-live="polite">
          {countdown === 0 ? "GO!" : countdown}
        </div>
      )}
      {!chartReady && !errorMsg && !menuOpen && (
        <div className="banner">{statusMsg}</div>
      )}
      {errorMsg && !menuOpen && <div className="banner err">{errorMsg}</div>}
      {menuOpen && (
        <MenuPanel
          difficulty={menuDifficulty}
          onDifficultyChange={setMenuDifficulty}
          settings={menuSettings}
          updateSetting={updateMenuSetting}
          onStart={() => void startFromMenu()}
          onCancel={chartReady ? closeMenuWithoutApplying : undefined}
          loading={menuLoading}
          error={errorMsg}
          backendMl={backendMl}
          backendReachable={backendReachable}
        />
      )}
      {results && (
        <div className="results">
          <div className="results-card">
            <h2>Results</h2>
            <div className="score">{results.score.score.toLocaleString("en-US")}</div>
            {results.prevBest && results.score.score > results.prevBest.score && (
              <div className="new-best">New best!</div>
            )}
            <div className="row">Max combo {results.score.maxCombo}</div>
            <div className="row">Accuracy {results.accuracy.toFixed(2)}%</div>
            {results.prevBest && (
              <div className="row" style={{ opacity: 0.6, marginTop: 4 }}>
                Previous best: {results.prevBest.score.toLocaleString("en-US")} ({results.prevBest.accuracy.toFixed(2)}%)
              </div>
            )}
            <div className="row" style={{ marginTop: 8, opacity: 0.7 }}>
              MAX {results.score.hitCounts.max}, GREAT {results.score.hitCounts.great},
              GOOD {results.score.hitCounts.good}, OK {results.score.hitCounts.ok},
              MEH {results.score.hitCounts.meh}, MISS {results.score.hitCounts.miss}
            </div>
            <div style={{ marginTop: 14, display: "flex", gap: 8, justifyContent: "center" }}>
              <button onClick={restart}>Replay</button>
              <button onClick={close}>Close</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
