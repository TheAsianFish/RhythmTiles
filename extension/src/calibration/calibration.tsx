import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { computeCalibrationOffset, metronomeBeats } from "@/game/calibration";
import { loadSettings, saveSettings } from "@/utils/storage";
import { clampAudioOffsetMs } from "@/utils/audio-offset";
import { applyDocumentSkin } from "@/ui/apply-skin";

const BPM = 120;
const BEATS = 16;

type Phase = "idle" | "running" | "done";

function App() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [beatIdx, setBeatIdx] = useState(-1);
  const [result, setResult] = useState<null | {
    offsetMs: number;
    usableTaps: number;
    stdDevMs: number;
  }>(null);
  const [savedOffsetMs, setSavedOffsetMs] = useState<number | null>(null);
  const [justReset, setJustReset] = useState(false);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const audioStartPerfRef = useRef<number>(0);
  const expectedBeatsRef = useRef<number[]>([]);
  const tapPerfMsRef = useRef<number[]>([]);

  useEffect(() => {
    loadSettings().then((s) => setSavedOffsetMs(s.audioLatencyOffsetMs));
  }, []);

  useEffect(() => {
    void loadSettings().then((s) => applyDocumentSkin(s.skinId));
  }, []);

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (phase !== "running") return;
      if (ev.code !== "Space") return;
      ev.preventDefault();
      tapPerfMsRef.current.push(performance.now());
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase]);

  async function start() {
    setPhase("running");
    setResult(null);
    tapPerfMsRef.current = [];
    const ctx = new AudioContext();
    audioCtxRef.current = ctx;
    const startAudio = ctx.currentTime + 0.2;
    audioStartPerfRef.current = performance.now() + 200;
    const beats = metronomeBeats(BPM, BEATS, 0);
    expectedBeatsRef.current = beats;

    for (let i = 0; i < BEATS; i++) {
      const tAudio = startAudio + (beats[i]! / 1000);
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = i === 0 ? 1200 : 880;
      osc.connect(gain).connect(ctx.destination);
      gain.gain.setValueAtTime(0.0, tAudio);
      gain.gain.linearRampToValueAtTime(0.4, tAudio + 0.005);
      gain.gain.exponentialRampToValueAtTime(0.0001, tAudio + 0.09);
      osc.start(tAudio);
      osc.stop(tAudio + 0.1);
    }

    // Visual indicator scheduling.
    const startVisualPerf = audioStartPerfRef.current;
    for (let i = 0; i < BEATS; i++) {
      const at = startVisualPerf + beats[i]!;
      const delay = at - performance.now();
      setTimeout(() => setBeatIdx(i), Math.max(0, delay));
    }

    // After the last beat plus a tail, compute the result.
    const finishDelay = beats[BEATS - 1]! + 500;
    setTimeout(() => {
      const res = computeCalibrationOffset({
        expectedAudioMs: expectedBeatsRef.current,
        actualPerfMs: tapPerfMsRef.current,
        audioStartPerfMs: audioStartPerfRef.current,
      });
      setResult(res);
      setPhase("done");
      ctx.close().catch(() => {});
      audioCtxRef.current = null;
    }, finishDelay);
  }

  async function save() {
    if (!result) return;
    const settings = await loadSettings();
    const rounded = clampAudioOffsetMs(Math.round(result.offsetMs));
    await saveSettings({ ...settings, audioLatencyOffsetMs: rounded });
    setSavedOffsetMs(rounded);
    setJustReset(false);
  }

  async function resetOffset() {
    const settings = await loadSettings();
    await saveSettings({ ...settings, audioLatencyOffsetMs: 0 });
    setSavedOffsetMs(0);
    setJustReset(true);
  }

  return (
    <div className="card">
      <h1>Calibration</h1>
      <p>
        Tap the spacebar on each click. We drop the first four taps as warm-up and average the rest
        to find your audio latency offset.
      </p>
      <div className="bpm">
        <span>{BPM} BPM</span>
        <span className={`indicator ${beatIdx % 2 === 0 ? "on" : ""}`} />
      </div>
      <div className={`tap-target ${phase === "running" ? "armed" : ""}`}>
        {phase === "idle" && "Click Start, then tap Space on each click."}
        {phase === "running" && `Tap Space on each click. ${tapPerfMsRef.current.length} taps so far.`}
        {phase === "done" && "All beats played. Review below."}
      </div>
      <div className="row">
        {phase !== "running" && <button onClick={start}>{result ? "Re-run" : "Start"}</button>}
        {result && (
          <button className="secondary" onClick={save}>
            Save offset
          </button>
        )}
        {phase !== "running" && savedOffsetMs !== null && savedOffsetMs !== 0 && (
          <button className="secondary" onClick={resetOffset}>
            Reset to 0
          </button>
        )}
      </div>
      {phase !== "running" && savedOffsetMs !== null && (
        <div style={{ marginTop: 8, color: "#98a3b3", fontSize: 13 }}>
          Current saved offset: <strong>{savedOffsetMs} ms</strong>
          {justReset && <span style={{ marginLeft: 8, color: "#7dd87d" }}>reset.</span>}
        </div>
      )}
      {result && (
        <div className="result">
          <div>
            Offset: <strong>{result.offsetMs.toFixed(1)} ms</strong>
          </div>
          <div>Std dev: {result.stdDevMs.toFixed(1)} ms across {result.usableTaps} usable taps.</div>
          <div style={{ marginTop: 6, color: "#98a3b3" }}>
            A negative offset means you were late on average. Save it to apply to all future plays.
          </div>
        </div>
      )}
    </div>
  );
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
