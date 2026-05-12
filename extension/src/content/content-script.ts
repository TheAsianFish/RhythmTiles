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

// Panel geometry. Sits on the right side of the YouTube viewport, descending
// from below the top nav bar so the video stays unobstructed.
const PANEL_WIDTH = 360;
const PANEL_MARGIN_RIGHT = 24;
const PANEL_MARGIN_TOP = 72;
const PANEL_MARGIN_BOTTOM = 24;

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
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const left = Math.max(8, vw - PANEL_WIDTH - PANEL_MARGIN_RIGHT);
  const height = Math.max(360, vh - PANEL_MARGIN_TOP - PANEL_MARGIN_BOTTOM);
  Object.assign(overlay.style, {
    position: "fixed",
    top: `${PANEL_MARGIN_TOP}px`,
    left: `${left}px`,
    width: `${PANEL_WIDTH}px`,
    height: `${height}px`,
    border: "0",
    margin: "0",
    padding: "0",
    zIndex: "2147483646",
    pointerEvents: "auto",
    background: "transparent",
    colorScheme: "dark",
    // The panel chrome (rounded corners, shadow) is drawn by overlay.css on
    // the iframe's <body>. The iframe itself stays a transparent rectangle.
  } as Partial<CSSStyleDeclaration>);
  overlay.setAttribute("allowtransparency", "true");
  document.body.appendChild(overlay);
  return overlay;
}

// Move the panel by (dx, dy) and clamp it to the visible viewport so it can
// never slip fully off-screen.
function moveOverlayBy(dx: number, dy: number): void {
  if (!overlay) return;
  const rect = overlay.getBoundingClientRect();
  const newLeft = Math.max(0, Math.min(window.innerWidth - rect.width, rect.left + dx));
  const newTop = Math.max(0, Math.min(window.innerHeight - rect.height, rect.top + dy));
  overlay.style.left = `${newLeft}px`;
  overlay.style.top = `${newTop}px`;
  overlay.style.right = "auto";
  overlay.style.bottom = "auto";
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

  // Send chart once: either when the overlay signals ready (handshake) or after
  // a 1s fallback timeout. The overlay also buffers pre-React messages, so both
  // paths are safe regardless of React's useEffect timing.
  // Note: we do NOT check ev.source here because isolated-world contentWindow
  // proxies don't === compare reliably against ev.source in Chrome extensions.
  let chartSent = false;
  const sendChart = (via: string) => {
    if (chartSent) return;
    chartSent = true;
    window.removeEventListener("message", readyHandler);
    clearTimeout(fallbackTimer);
    console.log(`[BeatBridge] sending BB_LOAD_CHART (via ${via})`);
    iframe.contentWindow?.postMessage(
      { type: "BB_LOAD_CHART", chart, videoId, difficulty },
      "*",
    );
  };
  function readyHandler(ev: MessageEvent) {
    if ((ev.data as { type?: string })?.type === "BB_OVERLAY_READY") {
      console.log("[BeatBridge] received BB_OVERLAY_READY from overlay");
      sendChart("handshake");
    }
  }
  window.addEventListener("message", readyHandler);
  // Fallback: if the overlay never fires BB_OVERLAY_READY, post anyway.
  // The overlay's pre-React message buffer will catch it.
  const fallbackTimer = window.setTimeout(() => sendChart("1s-fallback"), 1000);

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

// Listen for messages from the overlay (close request, drag deltas).
// We intentionally do NOT check ev.source here: in isolated worlds the
// contentWindow proxy doesn't === compare reliably against ev.source.
window.addEventListener("message", (ev) => {
  const msg = ev.data;
  if (!msg || typeof msg !== "object") return;
  switch (msg.type) {
    case "BB_OVERLAY_CLOSE":
      removeOverlay();
      break;
    case "BB_DRAG_BY":
      moveOverlayBy(msg.dx as number, msg.dy as number);
      break;
  }
});
