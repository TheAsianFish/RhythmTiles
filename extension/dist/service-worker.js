chrome.runtime.onInstalled.addListener(e=>{console.log("[BeatBridge] installed",e.reason)});chrome.runtime.onMessage.addListener((e,n,r)=>(e&&e.type==="BB_PING"&&r({ok:!0}),!1));
//# sourceMappingURL=service-worker.js.map
