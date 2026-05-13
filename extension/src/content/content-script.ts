// Content script. Runs on youtube.com/watch*.
// Responsibilities:
//  - listen for BB_START_GAME from the popup
//  - resolve the video element + videoId + duration
//  - fetch a chart from the backend
//  - inject the overlay iframe and post the chart to it
//  - keep the overlay's clock fed from the page's <video>.currentTime

import { generateChart, isPlaceholderChart } from "@/api/backend-client";
import type { Chart, Difficulty } from "@/types/chart";
import { loadSettings, subscribeSettingsChange } from "@/utils/storage";

const OVERLAY_ID = "beatbridge-overlay-root";
const CLOCK_TICK_MS = 16;

// Panel geometry. Sits on the right side of the YouTube viewport, descending
// from below the top nav bar so the video stays unobstructed. Width and max
// height are capped so the panel reads as a floating widget rather than a
// full-screen takeover. Taller panel (~20% vs the 620/420 caps) gives more
// pixels of runway above the hit line so notes are on-screen longer at 1x speed.
const PANEL_WIDTH = 320;
const PANEL_MAX_HEIGHT = 744;
const PANEL_MIN_HEIGHT = 504;
const PANEL_MARGIN_RIGHT = 24;
const PANEL_MARGIN_TOP = 72;
const PANEL_MARGIN_BOTTOM = 24;

let overlay: HTMLIFrameElement | null = null;
let clockInterval: number | null = null;
let videoEl: HTMLVideoElement | null = null;
// Track listeners we attach to the <video> element so successive Start Game
// presses don't pile duplicates onto the same element.
let videoListeners: Array<{ type: string; fn: EventListener }> = [];

// Last-confirmed-running game, used by the SPA-navigation watcher to decide
// whether to auto-restart for the next video. Both are set when startGame
// succeeds and cleared by removeOverlay().
let activeDifficulty: Difficulty | null = null;
let activeVideoId: string | null = null;
// Navigation watch state. YouTube fires `yt-navigate-finish` on its document
// after each SPA route, but we also poll `location.href` because that event
// has historically been unreliable across YouTube redesigns.
let lastSeenHref = location.href;
let navPollInterval: number | null = null;
let ytNavListener: (() => void) | null = null;

// Lane bindings snapshot taken on startGame. Used to decide which keys to
// intercept at the document level before YouTube's player gets them.
let boundCodes: Set<string> = new Set();
// Unsubscribe handle for the chrome.storage.onChanged listener that keeps
// boundCodes in sync with popup changes while a game is running. Null
// when no listener is registered.
let bindingsUnsubscribe: (() => void) | null = null;
// Codes currently held, for repeat-key dedup at the page level. Independent
// from the iframe's InputCapture pressed-set because the iframe only ever
// sees the deduplicated stream.
const pageHeldCodes = new Set<string>();
let keyDownHandler: ((ev: KeyboardEvent) => void) | null = null;
let keyUpHandler: ((ev: KeyboardEvent) => void) | null = null;

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
  const available = vh - PANEL_MARGIN_TOP - PANEL_MARGIN_BOTTOM;
  const height = Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_MAX_HEIGHT, available));
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

function detachVideoListeners() {
  if (!videoEl) {
    videoListeners = [];
    return;
  }
  for (const { type, fn } of videoListeners) videoEl.removeEventListener(type, fn);
  videoListeners = [];
}

// Install document-level keydown/keyup capture so YouTube's own shortcuts
// (k = pause, j/l = seek 10s, etc.) cannot fire when the player presses a
// lane-bound key. Events are forwarded to the overlay iframe via postMessage
// so the game receives them regardless of which window has focus.
function attachKeyCapture() {
  if (keyDownHandler) return;
  keyDownHandler = (ev: KeyboardEvent) => {
    if (isTypingInEditable(ev.target)) return; // let users type into inputs normally
    // P toggles the underlying video. We swallow the keypress so YouTube's
    // own P shortcut (picture-in-picture) doesn't also fire. The video's
    // play/pause events round-trip through onPlay/onPause below, and the
    // overlay's BB_VIDEO_PLAYING handler runs the 3-2-1 countdown on resume.
    // Only intercept if the user hasn't rebound KeyP to a lane.
    if (ev.code === "KeyP" && !boundCodes.has("KeyP") && overlay && videoEl) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      if (ev.repeat) return;
      if (videoEl.paused || videoEl.ended) {
        void videoEl.play().catch(() => {});
      } else {
        videoEl.pause();
      }
      return;
    }
    if (!boundCodes.has(ev.code)) return;
    // stopImmediatePropagation prevents any other listeners on the same
    // target (including YouTube's player shortcuts) from running. capture:
    // true ensures we fire before bubble-phase listeners.
    ev.preventDefault();
    ev.stopImmediatePropagation();
    if (ev.repeat || pageHeldCodes.has(ev.code)) return;
    pageHeldCodes.add(ev.code);
    overlay?.contentWindow?.postMessage(
      { type: "BB_KEY_DOWN", code: ev.code, perfMs: performance.now() },
      "*",
    );
  };
  keyUpHandler = (ev: KeyboardEvent) => {
    if (!boundCodes.has(ev.code)) return;
    if (isTypingInEditable(ev.target)) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    if (!pageHeldCodes.has(ev.code)) return;
    pageHeldCodes.delete(ev.code);
    overlay?.contentWindow?.postMessage(
      { type: "BB_KEY_UP", code: ev.code, perfMs: performance.now() },
      "*",
    );
  };
  window.addEventListener("keydown", keyDownHandler, { capture: true });
  window.addEventListener("keyup", keyUpHandler, { capture: true });
  // Keep boundCodes in sync if the popup rebinds keys while the game is
  // running. Previously the popup change would silently land in storage
  // and the running content script would keep intercepting the old keys.
  if (!bindingsUnsubscribe) {
    bindingsUnsubscribe = subscribeSettingsChange(() => {
      void refreshBoundCodes();
    });
  }
}

function isTypingInEditable(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable === true;
}

function detachKeyCapture() {
  if (keyDownHandler) {
    window.removeEventListener("keydown", keyDownHandler, { capture: true } as any);
    keyDownHandler = null;
  }
  if (keyUpHandler) {
    window.removeEventListener("keyup", keyUpHandler, { capture: true } as any);
    keyUpHandler = null;
  }
  if (bindingsUnsubscribe) {
    bindingsUnsubscribe();
    bindingsUnsubscribe = null;
  }
  pageHeldCodes.clear();
  boundCodes = new Set();
}

// Refresh boundCodes from storage so a popup-side keybinding change takes
// effect on the running game without an extension reload. Called by the
// chrome.storage.onChanged listener installed below.
async function refreshBoundCodes(): Promise<void> {
  try {
    const settings = await loadSettings();
    boundCodes = new Set(settings.bindings);
  } catch {
    // Ignore: stale boundCodes is preferable to throwing in a storage
    // listener callback.
  }
}

function removeOverlay() {
  if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
  overlay = null;
  if (clockInterval !== null) {
    clearInterval(clockInterval);
    clockInterval = null;
  }
  detachVideoListeners();
  detachKeyCapture();
  detachNavigationWatch();
  activeDifficulty = null;
  activeVideoId = null;
}

function isWatchUrl(): boolean {
  return location.pathname === "/watch";
}

// Called when YouTube navigates between videos in the same tab (autoplay
// queue, suggested-video click, prev/next, channel page → video). The user
// explicitly asked NOT to tear down the overlay between songs: keep it
// open, pause the new video, swap the <video> element bindings, and ask
// the overlay to drop into its menu state so the user can review settings
// and click Start when ready. The new chart is NOT fetched automatically;
// the menu's Start button triggers BB_REQUEST_NEW_CHART which routes to
// the same regenerateChart() path as the in-game menu's "change
// difficulty" flow.
async function handleNavigationChange(): Promise<void> {
  if (!activeDifficulty || !overlay) return;
  const newVideoId = getVideoId();
  if (newVideoId === activeVideoId) return;
  console.log("[BeatBridge] SPA navigation detected", {
    from: activeVideoId, to: newVideoId, href: location.href,
  });
  // Snapshot and pause the new video element NOW so autoplay can't start
  // playing audio we have no chart for yet.
  const incomingVideo = findVideoElement();
  try { incomingVideo?.pause(); } catch { /* ignore */ }
  if (!isWatchUrl() || !newVideoId) {
    // Navigated off /watch entirely. Close the overlay since there's no
    // video to play against.
    removeOverlay();
    return;
  }
  // Give YouTube a tick to finish swapping <video> internals before we
  // re-bind listeners and the clock bridge.
  await new Promise((r) => setTimeout(r, 350));
  const refreshed = findVideoElement();
  if (!refreshed) {
    console.warn("[BeatBridge] no video element after navigation");
    return;
  }
  // Re-bind: detach listeners from the OLD videoEl reference, attach to
  // the new one. The clock-bridge interval reads videoEl on each tick so
  // updating the variable is enough.
  detachVideoListeners();
  videoEl = refreshed;
  attachVideoListenersFor(videoEl);
  try { videoEl.pause(); } catch { /* ignore */ }
  // Tell the overlay a new video is loaded and to open the menu so the
  // player can choose settings and click Start. We forget the active
  // chart's videoId so the overlay's chartReady flag resets.
  activeVideoId = newVideoId;
  overlay.contentWindow?.postMessage(
    { type: "BB_NEW_VIDEO", videoId: newVideoId }, "*",
  );
}

// Attach the pause / play / seeked listeners to a given <video> element.
// Extracted from startGame so handleNavigationChange can re-bind to the
// new video element without rerunning chart generation.
function attachVideoListenersFor(v: HTMLVideoElement): void {
  if (!overlay) return;
  const iframe = overlay;
  const onPause = () => iframe.contentWindow?.postMessage({ type: "BB_VIDEO_PAUSED" }, "*");
  const onPlay = () => iframe.contentWindow?.postMessage({ type: "BB_VIDEO_PLAYING" }, "*");
  const onSeek = () => {
    iframe.contentWindow?.postMessage(
      {
        type: "BB_VIDEO_SEEKED",
        currentTime: v.currentTime,
        wasPlaying: !v.paused && !v.ended,
      },
      "*",
    );
  };
  v.addEventListener("pause", onPause);
  v.addEventListener("play", onPlay);
  v.addEventListener("seeked", onSeek);
  videoListeners = [
    { type: "pause", fn: onPause },
    { type: "play", fn: onPlay },
    { type: "seeked", fn: onSeek },
  ];
}

function attachNavigationWatch(): void {
  if (ytNavListener || navPollInterval !== null) return;
  ytNavListener = () => { void handleNavigationChange(); };
  document.addEventListener("yt-navigate-finish", ytNavListener);
  lastSeenHref = location.href;
  navPollInterval = window.setInterval(() => {
    if (location.href === lastSeenHref) return;
    lastSeenHref = location.href;
    void handleNavigationChange();
  }, 500);
}

function detachNavigationWatch(): void {
  if (ytNavListener) {
    document.removeEventListener("yt-navigate-finish", ytNavListener);
    ytNavListener = null;
  }
  if (navPollInterval !== null) {
    clearInterval(navPollInterval);
    navPollInterval = null;
  }
}

// Re-fetch a chart for the currently-active video at a new difficulty,
// then post it to the existing overlay iframe. Used by the in-overlay
// menu's Start button and by the SPA-navigation auto-restart. Pauses
// the video for the duration of the fetch so audio doesn't run ahead.
async function regenerateChart(difficulty: Difficulty): Promise<void> {
  const iframe = overlay;
  if (!iframe) return;
  if (!videoEl) videoEl = findVideoElement();
  if (!videoEl) {
    iframe.contentWindow?.postMessage(
      { type: "BB_CHART_ERROR", error: "No video element on page" }, "*",
    );
    return;
  }
  const videoId = getVideoId();
  if (!videoId) {
    iframe.contentWindow?.postMessage(
      { type: "BB_CHART_ERROR", error: "Could not parse videoId from URL" }, "*",
    );
    return;
  }
  try { videoEl.pause(); } catch { /* ignore */ }
  // Refresh the lane bindings snapshot (the user may have rebound keys in
  // the menu). The page-level interceptor reads this set on every keydown.
  const settings = await loadSettings();
  boundCodes = new Set(settings.bindings);
  attachKeyCapture();
  const duration = isFinite(videoEl.duration) ? videoEl.duration : undefined;
  let chart: Chart;
  try {
    chart = await generateChart({ videoId, difficulty, duration });
  } catch (e) {
    iframe.contentWindow?.postMessage(
      { type: "BB_CHART_ERROR", error: (e as Error).message }, "*",
    );
    return;
  }
  if (isPlaceholderChart(chart)) {
    iframe.contentWindow?.postMessage(
      {
        type: "BB_CHART_ERROR",
        error:
          "Backend returned the 20-note demo chart. Real audio generation is " +
          "disabled or failed. Restart the backend with BACKEND_ALLOW_YTDLP=1.",
      },
      "*",
    );
    return;
  }
  activeDifficulty = difficulty;
  activeVideoId = videoId;
  iframe.contentWindow?.postMessage(
    { type: "BB_LOAD_CHART", chart, videoId, difficulty }, "*",
  );
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

  // Snapshot the current key bindings and install the keyboard interceptor
  // before the chart request goes out, so even early presses are captured.
  const settings = await loadSettings();
  boundCodes = new Set(settings.bindings);
  attachKeyCapture();

  let chart: Chart;
  try {
    chart = await generateChart({ videoId, difficulty, duration });
    console.log("[BeatBridge] chart loaded", {
      notes: chart.notes.length,
      bpm: chart.audio.bpm,
      pipelineVersion: chart.metadata.pipelineVersion,
    });
  } catch (e) {
    const msg = (e as Error).message;
    console.warn("[BeatBridge] chart generation failed:", msg);
    // Tear down the key capture we installed above. Otherwise lane keys
    // (and YouTube's own k/f shortcuts on the same codes) stay swallowed
    // by the page-level listener with no overlay to forward to.
    detachKeyCapture();
    return { ok: false, error: `chart generation failed: ${msg}` };
  }

  if (isPlaceholderChart(chart)) {
    const msg =
      "Backend returned the 20-note demo chart. Real audio generation is " +
      "disabled or failed. Restart the backend with BACKEND_ALLOW_YTDLP=1, " +
      "or check the backend terminal for an error trace.";
    console.warn("[BeatBridge]", msg);
    detachKeyCapture();
    return { ok: false, error: msg };
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

  // Pause/resume/seek listeners. Re-bound on SPA nav via the same helper.
  detachVideoListeners();
  attachVideoListenersFor(videoEl);

  startClockBridge();

  // Remember the running game so the SPA-navigation watcher can auto-restart
  // when YouTube switches videos. Attach the watcher last so it can't fire
  // mid-setup against stale state.
  activeDifficulty = difficulty;
  activeVideoId = videoId;
  lastSeenHref = location.href;
  attachNavigationWatch();
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
    case "BB_REQUEST_VIDEO_PAUSE":
      // Used by the overlay's pause-countdown sequence. videoEl.pause() will
      // fire a 'pause' event which bounces back as BB_VIDEO_PAUSED; the
      // overlay ignores that ricochet while inCountdown is true.
      videoEl?.pause();
      break;
    case "BB_REQUEST_VIDEO_PLAY":
      // The returned promise can reject if the browser rejects autoplay; we
      // ignore that since the user has already interacted with the page.
      void videoEl?.play().catch(() => {});
      break;
    case "BB_REQUEST_VIDEO_TOGGLE":
      // Posted by the overlay's P-key handler. The overlay can't reach the
      // page's <video> directly, so it asks us to flip state. Resume from
      // pause round-trips back as BB_VIDEO_PLAYING which triggers the
      // existing 3-2-1 countdown on the overlay side.
      if (!videoEl) break;
      if (videoEl.paused || videoEl.ended) {
        void videoEl.play().catch(() => {});
      } else {
        videoEl.pause();
      }
      break;
    case "BB_REQUEST_VIDEO_SEEK":
      // Posted by the overlay's Replay button (and any future "jump to
      // section" UI). Sets the video's currentTime; YouTube fires a
      // 'seeked' event which round-trips through onSeek as BB_VIDEO_SEEKED.
      if (!videoEl) break;
      try {
        videoEl.currentTime = Math.max(0, Number(msg.t) || 0);
      } catch { /* ignore */ }
      break;
    case "BB_REQUEST_NEW_CHART": {
      // Posted by the overlay's in-game menu when the user clicks Start
      // after changing difficulty (or simply confirming the same one).
      // Re-fetch the chart for the current videoId at the chosen
      // difficulty and post it back as BB_LOAD_CHART. The overlay closes
      // its menu and rebuilds the loop when the new chart arrives.
      const newDifficulty = (msg.difficulty as Difficulty) || activeDifficulty || "normal";
      void regenerateChart(newDifficulty);
      break;
    }
  }
});
