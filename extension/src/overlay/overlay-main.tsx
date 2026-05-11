import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { Chart, Difficulty } from "@/types/chart";
import { CanvasRenderer, DEFAULT_RENDER_CONFIG } from "./canvas-renderer";
import { GameLoop } from "@/game/loop";
import type { ClockSource } from "@/game/clock";
import { accuracyPercent } from "@/game/scoring";
import type { Judgment, ScoreState } from "@/game/types";
import { loadSettings, saveBestScore, DEFAULT_SETTINGS } from "@/utils/storage";

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
  const [chart, setChart] = useState<Chart | null>(null);
  const [difficulty, setDifficulty] = useState<Difficulty>("normal");
  const [videoId, setVideoId] = useState<string>("");

  // Resize handler.
  useEffect(() => {
    if (!canvasRef.current) return;
    rendererRef.current = new CanvasRenderer(canvasRef.current, DEFAULT_RENDER_CONFIG);
    const r = rendererRef.current;
    const onResize = () => r.resize(window.innerWidth, window.innerHeight);
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Listen to parent messages.
  useEffect(() => {
    function handler(ev: MessageEvent) {
      const msg = ev.data;
      if (!msg || typeof msg !== "object") return;
      switch (msg.type) {
        case "BB_CLOCK_TICK":
          clockRef.current.set(msg.currentTime as number, !!msg.paused);
          break;
        case "BB_LOAD_CHART": {
          setChart(msg.chart as Chart);
          setDifficulty((msg.difficulty as Difficulty) || "normal");
          setVideoId(msg.videoId as string);
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
          clockRef.current.currentTime = msg.currentTime as number;
          break;
      }
    }
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
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

  // Show an idle banner before chart arrives or if it failed.
  return (
    <>
      <canvas ref={canvasRef} />
      <div className="toolbar">
        <button onClick={close}>Close</button>
        {results && <button onClick={restart}>Replay</button>}
      </div>
      {!chartReady && !errorMsg && (
        <div className="banner">Loading chart...</div>
      )}
      {errorMsg && <div className="banner">{errorMsg}</div>}
      {results && (
        <div className="results">
          <div className="results-card">
            <h2>Results</h2>
            <div className="score">{results.score.score}</div>
            <div>Max combo {results.score.maxCombo}</div>
            <div>Accuracy {results.accuracy.toFixed(1)}%</div>
            <div style={{ fontSize: 12, marginTop: 10, opacity: 0.75 }}>
              Perfect {results.score.hitCounts.perfect}, Good {results.score.hitCounts.good}, OK{" "}
              {results.score.hitCounts.ok}, Miss {results.score.hitCounts.miss}
            </div>
            <div style={{ marginTop: 18, display: "flex", gap: 8, justifyContent: "center" }}>
              <button onClick={restart}>Replay</button>
              <button onClick={close}>Close</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
  void setErrorMsg; // reserved for future error states
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
