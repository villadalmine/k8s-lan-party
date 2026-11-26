"use strict";
// The live page (docs/LIVE-PAGE.md): the house match's last frame, about twice a second, while it is on the air. No input, no cookie:
// the page only reads /api/v1/live and /api/v1/live/frame (an <img> of this server; the query only defeats caches).
(function () {
  const img = document.getElementById("frame"), state = document.getElementById("state");
  let live = false, timer = null, wait = 500;
  function say(text, on) { state.textContent = text; state.className = on ? "onair on" : "onair"; }
  async function check() {
    try {
      const r = await fetch("/api/v1/live", { cache: "no-store", credentials: "omit" });
      const s = await r.json();
      live = Boolean(s.live);
      if (live) {
        const since = s.since ? new Date(s.since * 1000).toLocaleTimeString() : "";
        say(`On the air${since ? " since " + since : ""} - ${s.players} in the match, ${s.viewers} watching`, true);
        if (!timer) next();
      } else {
        say("The house match is not on the air right now. This page checks again by itself.", false);
      }
    } catch (_) { say("The page did not answer; trying again.", false); }
    setTimeout(check, live ? 10000 : 15000);
  }
  function next() {
    timer = null;
    if (!live) return;
    img.src = "/api/v1/live/frame?t=" + Date.now();
  }
  img.addEventListener("load", () => { wait = 500; timer = setTimeout(next, wait); });
  img.addEventListener("error", () => { wait = Math.min(8000, wait * 2); timer = setTimeout(next, wait); });   // offline or too many viewers: slower
  check();
})();
