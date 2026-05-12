import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { Chart, Difficulty } from "@/types/chart";
import { CanvasRenderer, DEFAULT_RENDER_CONFIG } from "./canvas-renderer";
import { playHitClick, setHitVolume } from "./sfx";
import { GameLoop } from "@/game/loop";
import type { ClockSource } from "@/game/clock";
import { accuracyPercent } from "@/game/scoring";
import type { Judgment, ScoreState } from "@/game/types";
import { loadSettings, loadBestScore, saveBestScore, DEFAULT_SETTINGS } from "@/utils/storage";

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
          setChart(m.chart as Chart);
          setDifficulty((m.difficulty as Difficulty) || "normal");
          setVideoId(m.videoId as string);
          setChartReady(true);
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
          // currentTime is a getter now (interpolating); rebase the clock
          // via set() so the next read returns the seeked time exactly.
          clockRef.current.set(seekTime, clockRef.current.paused);
          loopRef.current?.seekToVideoTime(seekTime);
          rendererRef.current?.resetAnim();
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
      loop.start(window);
      loopRef.current = loop;
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

  function restart() {
    setResults(null);
    setChartReady(false);
    // Re-trigger by clearing chart momentarily.
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
        <button onClick={close}>Close</button>
      </div>
      <canvas ref={canvasRef} />
      {countdown !== null && (
        <div className="countdown" aria-live="polite">
          {countdown === 0 ? "GO!" : countdown}
        </div>
      )}
      {!chartReady && !errorMsg && (
        <div className="banner">{statusMsg}</div>
      )}
      {errorMsg && <div className="banner err">{errorMsg}</div>}
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
