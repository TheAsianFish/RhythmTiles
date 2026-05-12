import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { backendUrl, pingHealthDetailed } from "@/api/backend-client";
import type { Difficulty } from "@/types/chart";
import { hitWindowsForOD } from "@/game/types";
import { loadSettings, saveSettings } from "@/utils/storage";

type BackendStatus = "unknown" | "ok" | "down";

function App() {
  const [status, setStatus] = useState<BackendStatus>("unknown");
  const [healthError, setHealthError] = useState<string | null>(null);
  const [difficulty, setDifficulty] = useState<Difficulty>("normal");
  const [od, setOD] = useState<number>(8);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Keep a ref so the interval callback can read the latest status without
  // being in the effect deps (which would cause an infinite re-run loop).
  const statusRef = useRef<BackendStatus>("unknown");

  const refreshHealth = useCallback(async () => {
    setStatus("unknown");
    statusRef.current = "unknown";
    setHealthError(null);
    const ping = await pingHealthDetailed();
    const next: BackendStatus = ping.ok ? "ok" : "down";
    setStatus(next);
    statusRef.current = next;
    setHealthError(ping.ok ? null : ping.error ?? "unknown error");
  }, []);

  useEffect(() => {
    void refreshHealth();
    // Re-check while the popup is open so the user doesn't get stuck on
    // a stale "down" if they boot the server after opening the popup.
    // statusRef avoids including status in the deps, which would cause
    // refreshHealth to be called on every state change and loop forever.
    const id = window.setInterval(() => {
      if (statusRef.current !== "ok") void refreshHealth();
    }, 3000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshHealth]);

  // Load persisted OD setting on open; persist on change.
  useEffect(() => {
    void (async () => {
      const s = await loadSettings();
      setOD(s.overallDifficulty);
    })();
  }, []);
  async function updateOD(next: number) {
    setOD(next);
    const s = await loadSettings();
    await saveSettings({ ...s, overallDifficulty: next });
  }

  const windows = hitWindowsForOD(od);

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

      // Try sending the message. If the content script isn't injected yet
      // (tab was open before the extension loaded), inject it first then retry.
      let resp = await trySendStart(tab.id, difficulty);
      if (resp === null) {
        // Content script not present - inject it now.
        await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          files: ["content-script.js"],
        });
        // Give the script a moment to register its listener.
        await new Promise((r) => setTimeout(r, 150));
        resp = await trySendStart(tab.id, difficulty);
      }

      if (!resp || !resp.ok) {
        setError(resp?.error || "Content script did not respond. Reload the YouTube tab and try again.");
        return;
      }
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
        {status === "ok" && `Backend reachable at ${backendUrl()}.`}
        {status === "down" && (
          <>
            <div>Backend not reachable at {backendUrl()}.</div>
            {healthError && (
              <div style={{ marginTop: 4, opacity: 0.8 }}>Reason: {healthError}</div>
            )}
            <button
              className="secondary"
              style={{ marginTop: 8, padding: "4px 10px", fontSize: 12 }}
              onClick={() => void refreshHealth()}
            >
              Retry
            </button>
          </>
        )}
        {status === "unknown" && "Checking backend..."}
      </div>

      <div className="row">
        <label htmlFor="difficulty">Chart density</label>
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

      <div className="row">
        <label htmlFor="od">Timing strictness (OD)</label>
        <select
          id="od"
          value={od}
          onChange={(e) => void updateOD(Number(e.target.value))}
        >
          <option value={5}>Lenient (OD 5)</option>
          <option value={7}>Standard (OD 7)</option>
          <option value={8}>Challenging (OD 8)</option>
          <option value={9}>Strict (OD 9)</option>
          <option value={10}>Extreme (OD 10)</option>
        </select>
      </div>
      <div className="hint" style={{ marginTop: -8 }}>
        At OD {od}: MAX &plusmn;{windows.max.toFixed(1)}ms, GREAT &plusmn;{windows.great.toFixed(0)}ms,
        GOOD &plusmn;{windows.good.toFixed(0)}ms, OK &plusmn;{windows.ok.toFixed(0)}ms,
        MEH &plusmn;{windows.meh.toFixed(0)}ms.
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

// Returns the response on success, null if the receiving end doesn't exist yet,
// or throws on any other error.
async function trySendStart(
  tabId: number,
  difficulty: string,
): Promise<{ ok: boolean; error?: string } | null> {
  try {
    return (await chrome.tabs.sendMessage(tabId, {
      type: "BB_START_GAME",
      difficulty,
    })) as { ok: boolean; error?: string } | undefined ?? null;
  } catch (e) {
    if ((e as Error).message?.includes("Receiving end does not exist")) return null;
    throw e;
  }
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
