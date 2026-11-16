"use strict";
const $ = (id) => document.getElementById(id);
const views = { status: "Status", events: "My events", parties: "Parties", games: "Games I can join" };
let view = "status", timer = null, expiresAt = null;

function token() {
  const m = location.hash.match(/^#t=([A-Za-z0-9_-]{20,})$/);
  if (m) { try { sessionStorage.setItem("player-token", m[1]); } catch (_) { window.__playerToken = m[1]; }
    history.replaceState(null, "", location.pathname); }
  try { return sessionStorage.getItem("player-token") || window.__playerToken || ""; }
  catch (_) { return window.__playerToken || ""; }
}
async function api(method, path, body) {
  const opts = { method, cache: "no-store", credentials: "omit", headers: { Authorization: "Bearer " + token() } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch("/api/v1/" + path, opts);
  let data; try { data = await r.json(); } catch (_) { data = { message: "the player console returned no JSON" }; }
  if (r.status === 401 || r.status === 403 || r.status >= 500) banner(data.message || "the API refused the request");
  else if (r.ok) banner("");
  return { status: r.status, data };
}
function banner(text) { $("banner").textContent = text; $("banner").hidden = !text; }
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; }
function table(rows) {
  if (!Array.isArray(rows) || !rows.length) return el("p", "nothing here", "muted");
  const cols = []; rows.slice(0, 50).forEach((r) => Object.keys(r || {}).forEach((k) => { if (!cols.includes(k) && cols.length < 10) cols.push(k); }));
  const t = el("table"), h = el("tr"); cols.forEach((c) => h.appendChild(el("th", c))); t.appendChild(h);
  rows.slice(0, 100).forEach((r) => { const tr = el("tr"); cols.forEach((c) => { let v = r[c]; if (v && typeof v === "object") v = JSON.stringify(v); tr.appendChild(el("td", v)); }); t.appendChild(tr); });
  const wrap = el("div", null, "scroll"); wrap.appendChild(t); return wrap;
}
function drawStatus(s) {
  expiresAt = s.expires_at;
  const seconds = Math.max(0, expiresAt - Math.floor(Date.now() / 1000));
  const time = `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  $("summary").textContent = `${s.name} · ${time} left · Nebula ${s.node.nebula} · API ${s.node.api}`;
  const box = $("view"); box.appendChild(el("p", `Namespace: ${s.namespace}`));
  box.appendChild(el("p", `Lease expiry from your token: ${new Date(expiresAt * 1000).toISOString()} (${time} remaining). This is informational; revocation can end access sooner.`));
  box.appendChild(el("p", `Your node link: Nebula ${s.node.nebula}; own namespace API ${s.node.api}; visible workloads ${s.workloads}.`));
  box.appendChild(el("p", s.node.note, "muted"));
}
function draw(rows) {
  const box = $("view"); box.replaceChildren(el("h2", views[view]));
  if (view === "status") return drawStatus(rows);
  if (view === "events") {
    const items = (rows.items || []).map((e) => ({ type: e.type, reason: e.reason, object: e.involvedObject && `${e.involvedObject.kind}/${e.involvedObject.name}`, message: e.message, time: e.lastTimestamp || e.eventTime }));
    return box.appendChild(table(items));
  }
  box.appendChild(table(rows));
}
async function poll() {
  clearTimeout(timer);
  try {
    const r = await api("GET", view);
    if (view === "status" && r.status === 200) draw(r.data);
    else if (view !== "status" && r.status === 200) draw(r.data);
    else if (r.status !== 200) { const box = $("view"); box.replaceChildren(el("p", r.data.message || `HTTP ${r.status}`, "error")); }
  } catch (e) { banner("the player console did not answer: " + e.message); }
  finally { timer = setTimeout(poll, 5000); }
}
async function showHostVerbs() {
  const r = await api("GET", "verbs"); if (r.status !== 200 || !Array.isArray(r.data)) return;
  const box = $("hostverbs");
  r.data.filter((v) => !v.allowed).forEach((v) => { const b = el("button", `${v.verb} — host only`); b.disabled = true; b.title = v.why || "not available to the player role"; box.appendChild(b); });
}
async function action(verb, peer, ports) {
  const body = {};
  if (peer !== undefined && peer !== "") body.peer = peer;
  if (ports !== undefined && ports !== "") body.ports = ports;
  const r = await api("POST", "actions/" + verb, body);
  if (r.status !== 202) return banner((r.data && r.data.message) || `HTTP ${r.status}`);
  banner(r.data.message || "done"); poll();
}
document.addEventListener("DOMContentLoaded", () => {
  token();
  if (!token()) banner("No console token: run edge-mesh console and open the one-time link it prints.");
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]"); if (!b) return;
    view = b.dataset.view; $("tabs").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); poll();
  });
  $("invite").addEventListener("click", () => action("party-invite", $("peer").value.trim(), $("ports").value.trim()));
  $("accept").addEventListener("click", () => action("party-accept", $("peer").value.trim(), $("ports").value.trim()));
  $("leave").addEventListener("click", () => action("party-leave", $("peer").value.trim()));
  $("leaveall").addEventListener("click", () => action("party-leave", ""));
  showHostVerbs(); poll();
});
