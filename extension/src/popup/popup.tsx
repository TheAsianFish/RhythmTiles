import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  backendUrl,
  pingHealthDetailed,
  type BackendMlFlags,
} from "@/api/backend-client";
import type { Difficulty } from "@/types/chart";
import { hitWindowsForOD } from "@/game/types";
import { DEFAULT_SETTINGS, loadSettings, saveSettings, type UserSettings } from "@/utils/storage";
import {
  AUDIO_OFFSET_MS_MIN,
  AUDIO_OFFSET_MS_MAX,
  clampAudioOffsetMs,
} from "@/utils/audio-offset";
import { describeProgress, formatElapsed } from "@/utils/loading-progress";
import { applyDocumentSkin } from "@/ui/apply-skin";
import { SKIN_IDS, SKIN_LABELS } from "@/ui/skins";

type BackendStatus = "unknown" | "ok" | "down";

// Render a KeyboardEvent.code as a short display label. e.g. "KeyD" -> "D",
// "Semicolon" -> ";", "Space" -> "Space". Falls back to the raw code.
function codeToLabel(code: string): string {
  if (code.startsWith("Key")) return code.slice(3);
  if (code.startsWith("Digit")) return code.slice(5);
  if (code.startsWith("Arrow")) return code.slice(5);
  const map: Record<string, string> = {
    Semicolon: ";",
    Quote: "'",
    Comma: ",",
    Period: ".",
    Slash: "/",
    Backslash: "\\",
    BracketLeft: "[",
    BracketRight: "]",
    Minus: "-",
    Equal: "=",
    Space: "Space",
    Tab: "Tab",
    Enter: "Enter",
    ShiftLeft: "LShift",
    ShiftRight: "RShift",
    ControlLeft: "LCtrl",
    ControlRight: "RCtrl",
    AltLeft: "LAlt",
    AltRight: "RAlt",
  };
  return map[code] ?? code;
}

function KeyCapture({
  value,
  onChange,
  conflict,
}: {
  value: string;
  onChange: (code: string) => void;
  conflict: boolean;
}) {
  const [capturing, setCapturing] = useState(false);
  useEffect(() => {
    if (!capturing) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.code === "Escape") {
        setCapturing(false);
        return;
      }
      onChange(e.code);
      setCapturing(false);
    };
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true } as any);
  }, [capturing, onChange]);

  const cls = ["keycap"];
  if (capturing) cls.push("capturing");
  if (conflict) cls.push("conflict");
  return (
    <button
      type="button"
      className={cls.join(" ")}
      onClick={() => setCapturing((c) => !c)}
      title={conflict ? "Duplicate binding" : "Click then press a key"}
    >
      {capturing ? "Press..." : codeToLabel(value)}
    </button>
  );
}

// Compact progress display shown while the popup waits on the chart
// fetch. Same stage prediction as the in-overlay menu, but sized to the
// popup's narrow column. The popup auto-closes once the content script
// responds with ok, so this is visible only during the actual wait.
function PopupLoadingProgress({ flags }: { flags: BackendMlFlags | null }) {
  const [elapsedMs, setElapsedMs] = useState(0);
  const startRef = useRef<number>(performance.now());
  useEffect(() => {
    startRef.current = performance.now();
    setElapsedMs(0);
    const id = window.setInterval(() => {
      setElapsedMs(performance.now() - startRef.current);
    }, 500);
    return () => window.clearInterval(id);
  }, []);
  const view = describeProgress(elapsedMs, flags);
  return (
    <div className={`popup-progress${view.warn ? " popup-progress-warn" : ""}`}>
      <div className="popup-progress-head">
        <span className="popup-progress-time">{formatElapsed(elapsedMs)}</span>
        <span className="popup-progress-stage">{view.stage}</span>
      </div>
      <div className="popup-progress-hint">{view.hint}</div>
      <div className="popup-progress-eta">Expected: {view.expectedTotal}</div>
    </div>
  );
}

function App() {
  const [status, setStatus] = useState<BackendStatus>("unknown");
  const [healthError, setHealthError] = useState<string | null>(null);
  const [difficulty, setDifficulty] = useState<Difficulty>("normal");
  const [settings, setSettings] = useState<UserSettings>(DEFAULT_SETTINGS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Latest backend ML flag snapshot from /healthz, used by the loading
  // progress display when busy=true so we predict the right stage
  // (Demucs vs not, MERT vs not).
  const [backendMl, setBackendMl] = useState<BackendMlFlags | null>(null);
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
    setBackendMl(ping.ok && ping.ml ? ping.ml : null);
  }, []);

  useEffect(() => {
    void refreshHealth();
    const id = window.setInterval(() => {
      if (statusRef.current !== "ok") void refreshHealth();
    }, 3000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshHealth]);

  // Load persisted settings on open.
  useEffect(() => {
    void (async () => {
      const s = await loadSettings();
      setSettings(s);
    })();
  }, []);

  useEffect(() => {
    applyDocumentSkin(settings.skinId);
  }, [settings.skinId]);

  // Persist on every change. Settings live in chrome.storage.local so the
  // overlay picks them up on next game start.
  const update = useCallback(async (patch: Partial<UserSettings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      void saveSettings(next);
      return next;
    });
  }, []);

  function setBinding(idx: 0 | 1 | 2 | 3, code: string) {
    const next: [string, string, string, string] = [...settings.bindings] as [
      string,
      string,
      string,
      string,
    ];
    next[idx] = code;
    void update({ bindings: next });
  }

  const conflicts = findConflicts(settings.bindings);
  const windows = hitWindowsForOD(settings.overallDifficulty);

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

      let resp = await trySendStart(tab.id, difficulty);
      if (resp === null) {
        await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          files: ["content-script.js"],
        });
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
          <option value="expert">Expert</option>
        </select>
      </div>

      <div className="row">
        <label htmlFor="od">Timing strictness (OD)</label>
        <select
          id="od"
          value={settings.overallDifficulty}
          onChange={(e) => void update({ overallDifficulty: Number(e.target.value) })}
        >
          <option value={5}>Lenient (OD 5)</option>
          <option value={7}>Standard (OD 7)</option>
          <option value={8}>Challenging (OD 8)</option>
          <option value={9}>Strict (OD 9)</option>
          <option value={10}>Extreme (OD 10)</option>
        </select>
      </div>
      <div className="hint" style={{ marginTop: -8 }}>
        At OD {settings.overallDifficulty}: MAX &plusmn;{windows.max.toFixed(1)}ms, GREAT &plusmn;
        {windows.great.toFixed(0)}ms, GOOD &plusmn;{windows.good.toFixed(0)}ms, OK &plusmn;
        {windows.ok.toFixed(0)}ms, MEH &plusmn;{windows.meh.toFixed(0)}ms.
      </div>

      <div className="row">
        <label htmlFor="skin">Appearance</label>
        <select
          id="skin"
          value={settings.skinId}
          onChange={(e) =>
            void update({ skinId: e.target.value as UserSettings["skinId"] })
          }
        >
          {SKIN_IDS.map((id) => (
            <option key={id} value={id}>
              {SKIN_LABELS[id]}
            </option>
          ))}
        </select>
      </div>

      <button className="primary" onClick={onStart} disabled={busy || status === "down"}>
        {busy ? "Starting…" : "Start Game"}
      </button>

      {busy && <PopupLoadingProgress flags={backendMl} />}

      {error && <div className="status err">{error}</div>}

      <button
        type="button"
        className="disclosure"
        onClick={() => setAdvancedOpen((v) => !v)}
        aria-expanded={advancedOpen}
      >
        {advancedOpen ? "▾" : "▸"} Advanced settings
      </button>

      {advancedOpen && (
        <div className="advanced">
          <div className="row">
            <label>Lane keys</label>
            <div className="keycaps">
              <KeyCapture
                value={settings.bindings[0]}
                onChange={(c) => setBinding(0, c)}
                conflict={conflicts.has(0)}
              />
              <KeyCapture
                value={settings.bindings[1]}
                onChange={(c) => setBinding(1, c)}
                conflict={conflicts.has(1)}
              />
              <KeyCapture
                value={settings.bindings[2]}
                onChange={(c) => setBinding(2, c)}
                conflict={conflicts.has(2)}
              />
              <KeyCapture
                value={settings.bindings[3]}
                onChange={(c) => setBinding(3, c)}
                conflict={conflicts.has(3)}
              />
            </div>
          </div>
          {conflicts.size > 0 && (
            <div className="hint" style={{ color: "#ff9090" }}>
              Two lanes are bound to the same key. Only the first will fire in-game.
            </div>
          )}
          <div className="hint" style={{ marginTop: -4 }}>
            Click a key, then press the new binding. Escape to cancel.
          </div>

          <div className="row">
            <label htmlFor="audio-offset-num">Audio offset (ms)</label>
            <input
              id="audio-offset-num"
              type="number"
              className="offset-ms-input"
              min={AUDIO_OFFSET_MS_MIN}
              max={AUDIO_OFFSET_MS_MAX}
              step={1}
              value={settings.audioLatencyOffsetMs}
              onChange={(e) => {
                const v = Number(e.target.value);
                if (Number.isNaN(v)) return;
                void update({ audioLatencyOffsetMs: clampAudioOffsetMs(v) });
              }}
            />
          </div>
          <input
            id="audio-offset-slider"
            aria-label="Audio offset"
            type="range"
            min={AUDIO_OFFSET_MS_MIN}
            max={AUDIO_OFFSET_MS_MAX}
            step={5}
            value={clampAudioOffsetMs(settings.audioLatencyOffsetMs)}
            onChange={(e) =>
              void update({ audioLatencyOffsetMs: clampAudioOffsetMs(Number(e.target.value)) })}
          />
          <div className="hint" style={{ marginTop: -8 }}>
            Milliseconds layered on playback time for judgment. Matches the calibration tab; type a value or scrub (&plusmn;{Math.abs(AUDIO_OFFSET_MS_MIN)} ms). Use{" "}
            <strong>Calibrate timing</strong> below for a measured baseline.
          </div>

          <div className="row">
            <label htmlFor="speed">Note speed</label>
            <span className="value">{settings.noteSpeed.toFixed(2)}x</span>
          </div>
          <input
            id="speed"
            type="range"
            min={0.5}
            max={2.0}
            step={0.05}
            value={settings.noteSpeed}
            onChange={(e) => void update({ noteSpeed: Number(e.target.value) })}
          />

          <div className="row">
            <label htmlFor="opacity">Panel opacity</label>
            <span className="value">{Math.round(settings.opacity * 100)}%</span>
          </div>
          <input
            id="opacity"
            type="range"
            min={0.4}
            max={1.0}
            step={0.05}
            value={settings.opacity}
            onChange={(e) => void update({ opacity: Number(e.target.value) })}
          />

          <div className="row">
            <label htmlFor="sfx">Hit sound</label>
            <input
              id="sfx"
              type="checkbox"
              checked={settings.sfxEnabled}
              onChange={(e) => void update({ sfxEnabled: e.target.checked })}
            />
          </div>

          <div className="row">
            <label htmlFor="sfx-volume">Hit volume</label>
            <span className="value">{Math.round(settings.sfxVolume * 100)}%</span>
          </div>
          <input
            id="sfx-volume"
            type="range"
            min={0}
            max={1}
            step={0.05}
            disabled={!settings.sfxEnabled}
            value={settings.sfxVolume}
            onChange={(e) => void update({ sfxVolume: Number(e.target.value) })}
          />

          <button
            type="button"
            className="secondary"
            onClick={() => void update(DEFAULT_SETTINGS)}
            style={{ marginTop: 4 }}
          >
            Reset to defaults
          </button>
        </div>
      )}

      <p className="hint">
        Press the matching key as a note crosses the line. The popup will close when the game
        starts; bring focus back here to stop.
      </p>

      <button onClick={openCalibration}>Calibrate timing</button>
    </div>
  );
}

// Returns the set of lane indices whose binding is duplicated by another lane.
function findConflicts(bindings: readonly string[]): Set<number> {
  const seen = new Map<string, number[]>();
  bindings.forEach((code, i) => {
    const arr = seen.get(code);
    if (arr) arr.push(i);
    else seen.set(code, [i]);
  });
  const out = new Set<number>();
  for (const indices of seen.values()) {
    if (indices.length > 1) for (const i of indices) out.add(i);
  }
  return out;
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

// Exported for tests.
export { codeToLabel, findConflicts };

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
