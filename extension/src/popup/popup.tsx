import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { pingHealth } from "@/api/backend-client";
import type { Difficulty } from "@/types/chart";

type BackendStatus = "unknown" | "ok" | "down";

function App() {
  const [status, setStatus] = useState<BackendStatus>("unknown");
  const [difficulty, setDifficulty] = useState<Difficulty>("normal");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const ok = await pingHealth();
      if (!cancelled) setStatus(ok ? "ok" : "down");
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function onStart() {
    setBusy(true);
    setError(null);
    try {
      const tab = await getActiveTab();
      if (!tab || !tab.id || !tab.url) {
        setError("No active tab.");
        return;
      }
      if (!/^https:\/\/www\.youtube\.com\/watch/.test(tab.url)) {
        setError("Open a YouTube video first.");
        return;
      }
      await chrome.tabs.sendMessage(tab.id, {
        type: "BB_START_GAME",
        difficulty,
      });
      window.close();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <div>
        <h1>BeatBridge</h1>
        <p className="subtitle">Turn this YouTube tab into a rhythm game.</p>
      </div>

      <div className={`status ${status === "ok" ? "ok" : status === "down" ? "err" : "warn"}`}>
        {status === "ok" && "Backend reachable."}
        {status === "down" && "Backend not reachable on localhost:8000."}
        {status === "unknown" && "Checking backend..."}
      </div>

      <div className="row">
        <label htmlFor="difficulty">Difficulty</label>
        <select
          id="difficulty"
          value={difficulty}
          onChange={(e) => setDifficulty(e.target.value as Difficulty)}
        >
          <option value="easy">Easy</option>
          <option value="normal">Normal</option>
          <option value="hard">Hard</option>
        </select>
      </div>

      <button className="primary" onClick={onStart} disabled={busy || status === "down"}>
        {busy ? "Starting..." : "Start Game"}
      </button>

      {error && <div className="status err">{error}</div>}

      <p className="hint">
        Lanes: D, F, J, K. Press the matching key as a note crosses the line. The popup will close
        when the game starts; bring focus back here to stop.
      </p>

      <button onClick={openCalibration}>Calibrate timing</button>
    </div>
  );
}

function openCalibration() {
  const url = chrome.runtime.getURL("src/calibration/calibration.html");
  chrome.tabs.create({ url });
}

async function getActiveTab(): Promise<chrome.tabs.Tab | undefined> {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      resolve(tabs[0]);
    });
  });
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
