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

  // The panel (#119): the host's own numbers, as its puller last sent them, each group next to the idea it shows. Only text nodes are
  // written (no HTML from the server); a value the host did not send is "not live", never a guess.
  const panel = document.getElementById("panel"), pstate = document.getElementById("panelstate");
  function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
  function value(v) { return v === true ? "yes" : v === false ? "no" : typeof v === "number" ? v.toLocaleString() : "-"; }
  async function loadPanel() {
    try {
      const r = await fetch("/api/v1/panel", { cache: "no-store", credentials: "omit" });
      const p = await r.json();
      pstate.textContent = p.live ? `The host reported at ${new Date(p.at * 1000).toLocaleTimeString()}.`
        : "The host is not reporting right now (its cluster is down, or it does not publish its panel): nothing below is live.";
      const fields = p.live ? p.fields : {};
      panel.replaceChildren(...p.groups.map(g => {
        const s = el("section"), dl = el("dl");
        s.append(el("h3", g.title));
        for (const f of g.fields) {
          const has = Object.prototype.hasOwnProperty.call(fields, f.id);
          dl.append(el("dt", has ? value(fields[f.id]) : "-", has ? "" : "off"), el("dd", has ? f.label : f.label + " (not live)", has ? "" : "off"));
        }
        s.append(dl);
        for (const l of g.learn) {
          if (!l.concept) continue;
          const d = el("details"), sum = el("summary", "What this is: " + l.id);
          d.append(sum, el("p", l.concept), el("p", "Like: " + l.analogy), el("p", "For real: " + l.real_world));
          s.append(d);
        }
        return s;
      }));
    } catch (_) { pstate.textContent = "The page did not answer; trying again."; }
    setTimeout(loadPanel, 30000);
  }
  loadPanel();
})();
