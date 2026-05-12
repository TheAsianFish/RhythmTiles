import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { Chart, Difficulty } from "@/types/chart";
import { CanvasRenderer, DEFAULT_RENDER_CONFIG } from "./canvas-renderer";
import { GameLoop } from "@/game/loop";
import type { ClockSource } from "@/game/clock";
import { accuracyPercent } from "@/game/scoring";
import type { Judgment, ScoreState } from "@/game/types";
import { loadSettings, saveBestScore, DEFAULT_SETTINGS } from "@/utils/storage";

// React's useEffect fires asynchronously after paint. postMessage from the
// parent can arrive before the listener is registered, dropping BB_LOAD_CHART.
// Buffer messages immediately so the first useEffect can drain them.
const _pendingMsgs: unknown[] = [];
const _earlyListener = (ev: MessageEvent) => {
  if (ev.data && typeof ev.data === "object") _pendingMsgs.push(ev.data);
};
window.addEventListener("message", _earlyListener);

// A clock that's fed by postMessage ticks from the parent (content script).
class BridgedClock implements ClockSource {
  currentTime = 0;
  paused = true;
  set(t: number, paused: boolean) {
    this.currentTime = t;
    this.paused = paused;
  }
}

interface ResultsPayload {
  score: ScoreState;
  accuracy: number;
}

function App() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<CanvasRenderer | null>(null);
  const clockRef = useRef<BridgedClock>(new BridgedClock());
  const loopRef = useRef<GameLoop | null>(null);
  const [results, setResults] = useState<ResultsPayload | null>(null);
  const [chartReady, setChartReady] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState("Waiting for content script...");
  const [chart, setChart] = useState<Chart | null>(null);
  const [difficulty, setDifficulty] = useState<Difficulty>("normal");
  const [videoId, setVideoId] = useState<string>("");
  const [dragging, setDragging] = useState(false);
  const dragOriginRef = useRef<{ x: number; y: number } | null>(null);

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
          setStatusMsg("Chart received, starting game...");
          setChart(m.chart as Chart);
          setDifficulty((m.difficulty as Difficulty) || "normal");
          setVideoId(m.videoId as string);
          setChartReady(true);
          break;
        }
        case "BB_VIDEO_PAUSED":
          clockRef.current.paused = true;
          break;
        case "BB_VIDEO_PLAYING":
          clockRef.current.paused = false;
          break;
        case "BB_VIDEO_SEEKED":
          clockRef.current.currentTime = m.currentTime as number;
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
    const timeout = window.setTimeout(() => {
      setErrorMsg(
        "Chart not received after 15s. Open DevTools on the YouTube tab and check for [BeatBridge] logs.",
      );
    }, 15000);

    return () => {
      window.removeEventListener("message", evHandler);
      clearTimeout(timeout);
    };
  }, []);

  // Boot the loop when chart arrives.
  useEffect(() => {
    if (!chart || !rendererRef.current) return;
    let cancelled = false;

    (async () => {
      const settings = (await loadSettings()) ?? DEFAULT_SETTINGS;
      if (cancelled) return;

      const lastJudgmentRef: { current?: { judgment: Judgment; deltaMs: number; atMs: number } } = {};
      const loop = new GameLoop({
        chart,
        clock: clockRef.current,
        bindings: { codes: settings.bindings },
        audioLatencyOffsetMs: settings.audioLatencyOffsetMs,
        callbacks: {
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
            });
          },
          onFinish: ({ score, accuracyPercent: acc }) => {
            setResults({ score, accuracy: acc });
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

  function close() {
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
        <div className="spacer" />
        {results && <button onClick={restart}>Replay</button>}
        <button onClick={close}>Close</button>
      </div>
      <canvas ref={canvasRef} />
      {!chartReady && !errorMsg && (
        <div className="banner">{statusMsg}</div>
      )}
      {errorMsg && <div className="banner err">{errorMsg}</div>}
      {results && (
        <div className="results">
          <div className="results-card">
            <h2>Results</h2>
            <div className="score">{results.score.score.toLocaleString("en-US")}</div>
            <div className="row">Max combo {results.score.maxCombo}</div>
            <div className="row">Accuracy {results.accuracy.toFixed(1)}%</div>
            <div className="row" style={{ marginTop: 8, opacity: 0.7 }}>
              Perfect {results.score.hitCounts.perfect}, Good {results.score.hitCounts.good},
              OK {results.score.hitCounts.ok}, Miss {results.score.hitCounts.miss}
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
