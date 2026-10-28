// The only script of the public page (poc/page/page.py): the proof of work and three calls to the JSON API. No third party, no
// cookies, no storage; every answer is written with textContent, never as HTML. The same API works without a browser (visit.py, curl).
"use strict";

function zeroBits(buf) {
  const b = new Uint8Array(buf);
  let n = 0;
  for (const x of b) {
    if (x === 0) { n += 8; continue; }
    return n + Math.clz32(x) - 24;
  }
  return n;
}

// find a decimal nonce so that sha256(challenge + ":" + nonce) starts with `bits` zero bits (the same rule as page.py and visit.py)
async function solve(challenge, bits, progress) {
  const enc = new TextEncoder();
  const batch = 4096;
  for (let start = 0; start < 1e15; start += batch) {
    const tries = [];
    for (let n = start; n < start + batch; n++) tries.push(crypto.subtle.digest("SHA-256", enc.encode(challenge + ":" + n)));
    const res = await Promise.all(tries);
    for (let i = 0; i < res.length; i++) if (zeroBits(res[i]) >= bits) return String(start + i);
    if (progress) progress(start + batch);
  }
  throw new Error("no nonce found");
}

async function api(path, body) {
  const opt = { credentials: "omit", cache: "no-store" };
  if (body !== undefined) Object.assign(opt, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { j = {}; }
  return { ok: r.ok, status: r.status, body: j };
}

function init() {
  const $ = (id) => document.getElementById(id);
  const say = (id, text) => { $(id).textContent = text; };

  async function live() {
    const r = await api("/api/v1/status");
    if (!r.ok) return;
    const s = r.body;
    say("live", s.open ? `Now: ${s.slots_free} of ${s.slots_total} places free, ${s.waiting} in the list.` : `Closed: ${s.closed_reason}`);
  }
  live();
  setInterval(live, 30000);

  $("ask").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    say("asked", "Getting a puzzle...");
    const c = await api("/api/v1/challenge");
    if (!c.ok) { say("asked", c.body.message || "the page did not answer"); return; }
    const nonce = await solve(c.body.challenge, c.body.difficulty, (n) => say("asked", `Solving the puzzle... ${n} tries`));
    const r = await api("/api/v1/requests", {
      name: $("name").value, nebula_public_key: $("neb").value, seal_public_key: $("seal").value,
      challenge: c.body.challenge, nonce: nonce,
    });
    if (!r.ok) { say("asked", `Not taken: ${r.body.message || r.status}`); return; }
    say("asked", `Your claim code: ${r.body.claim_code} (write it down: it is shown only once). ${r.body.message}`);
  });

  $("check").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const r = await api("/api/v1/requests/status", { claim_code: $("code").value });
    say("where", r.ok ? `${r.body.name}: ${r.body.message}` : (r.body.message || String(r.status)));
  });
}

if (typeof module !== "undefined") module.exports = { zeroBits, solve };
if (typeof document !== "undefined") init();
