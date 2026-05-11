// Content script. Runs on youtube.com/watch*.
// Responsibilities:
//  - listen for BB_START_GAME from the popup
//  - resolve the video element + videoId + duration
//  - fetch a chart from the backend
//  - inject the overlay iframe and post the chart to it
//  - keep the overlay's clock fed from the page's <video>.currentTime

import { generateChart, BackendError } from "@/api/backend-client";
import type { Chart, Difficulty } from "@/types/chart";

const OVERLAY_ID = "beatbridge-overlay-root";
const CLOCK_TICK_MS = 16;

let overlay: HTMLIFrameElement | null = null;
let clockInterval: number | null = null;
let videoEl: HTMLVideoElement | null = null;

function findVideoElement(): HTMLVideoElement | null {
  const v = document.querySelector("video.html5-main-video") as HTMLVideoElement | null;
  return v || (document.querySelector("video") as HTMLVideoElement | null);
}

function getVideoId(): string | null {
  try {
    const url = new URL(location.href);
    return url.searchParams.get("v");
  } catch {
    return null;
  }
}

function ensureOverlay(): HTMLIFrameElement {
  if (overlay && document.body.contains(overlay)) return overlay;
  overlay = document.createElement("iframe");
  overlay.id = OVERLAY_ID;
  overlay.src = chrome.runtime.getURL("src/overlay/overlay.html");
  Object.assign(overlay.style, {
    position: "fixed",
    inset: "0",
    width: "100vw",
    height: "100vh",
    border: "0",
    margin: "0",
    padding: "0",
    zIndex: "2147483646",
    pointerEvents: "auto",
    background: "transparent",
    colorScheme: "dark",
  } as Partial<CSSStyleDeclaration>);
  // allowTransparency is a non-standard prop; setAttribute keeps TS happy
  // and is harmless on browsers that ignore it.
  overlay.setAttribute("allowtransparency", "true");
  document.body.appendChild(overlay);
  return overlay;
}

function removeOverlay() {
  if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
  overlay = null;
  if (clockInterval !== null) {
    clearInterval(clockInterval);
    clockInterval = null;
  }
}

function startClockBridge() {
  if (clockInterval !== null) clearInterval(clockInterval);
  clockInterval = window.setInterval(() => {
    if (!overlay || !overlay.contentWindow || !videoEl) return;
    overlay.contentWindow.postMessage(
      {
        type: "BB_CLOCK_TICK",
        currentTime: videoEl.currentTime,
        paused: videoEl.paused || videoEl.ended,
      },
      "*",
    );
  }, CLOCK_TICK_MS);
}

async function startGame(difficulty: Difficulty) {
  console.log("[BeatBridge] startGame", { difficulty, href: location.href });
  videoEl = findVideoElement();
  if (!videoEl) {
    console.warn("[BeatBridge] no video element on page");
    return { ok: false, error: "no video element on page" };
  }
  const videoId = getVideoId();
  if (!videoId) {
    return { ok: false, error: "could not parse videoId from URL" };
  }
  const duration = isFinite(videoEl.duration) ? videoEl.duration : undefined;
  console.log("[BeatBridge] resolved", { videoId, duration });

  let chart: Chart;
  try {
    chart = await generateChart({ videoId, difficulty, duration });
    console.log("[BeatBridge] chart loaded", {
      notes: chart.notes.length,
      bpm: chart.audio.bpm,
    });
  } catch (e) {
    const msg = e instanceof BackendError ? e.message : (e as Error).message;
    console.warn("[BeatBridge] chart generation failed:", msg);
    return { ok: false, error: `chart generation failed: ${msg}` };
  }

  const iframe = ensureOverlay();
  // Wait for the overlay iframe to load before posting.
  const post = () => {
    iframe.contentWindow?.postMessage(
      { type: "BB_LOAD_CHART", chart, videoId, difficulty },
      "*",
    );
  };
  if (iframe.contentDocument && iframe.contentDocument.readyState === "complete") {
    post();
  } else {
    iframe.addEventListener("load", post, { once: true });
  }

  // Pause/resume game when the user scrubs or pauses the video.
  videoEl.addEventListener("pause", () => {
    iframe.contentWindow?.postMessage({ type: "BB_VIDEO_PAUSED" }, "*");
  });
  videoEl.addEventListener("play", () => {
    iframe.contentWindow?.postMessage({ type: "BB_VIDEO_PLAYING" }, "*");
  });
  videoEl.addEventListener("seeked", () => {
    iframe.contentWindow?.postMessage(
      { type: "BB_VIDEO_SEEKED", currentTime: videoEl?.currentTime ?? 0 },
      "*",
    );
  });

  startClockBridge();
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;
  if (msg.type === "BB_START_GAME") {
    startGame(msg.difficulty || "normal")
      .then(sendResponse)
      .catch((err) => {
        console.error("[BeatBridge] startGame threw", err);
        sendResponse({ ok: false, error: (err as Error).message });
      });
    return true; // async response
  }
  if (msg.type === "BB_STOP_GAME") {
    removeOverlay();
    sendResponse({ ok: true });
    return false;
  }
  return false;
});

console.log("[BeatBridge] content script loaded on", location.href);

// Listen for messages from the overlay (e.g., "close me").
window.addEventListener("message", (ev) => {
  if (ev.source !== overlay?.contentWindow) return;
  const msg = ev.data;
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "BB_OVERLAY_CLOSE") {
    removeOverlay();
  }
});
