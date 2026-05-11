// MV3 service worker. Lifecycle is event-driven; do not hold long state here.
// For v1 we only need to log basic events.

chrome.runtime.onInstalled.addListener((details) => {
  console.log("[BeatBridge] installed", details.reason);
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Reserved for future bridging. The popup currently talks to the content
  // script directly via chrome.tabs.sendMessage, so the worker is a passthrough.
  if (msg && msg.type === "BB_PING") {
    sendResponse({ ok: true });
    return false;
  }
  void sender;
  return false;
});
