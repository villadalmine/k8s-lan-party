// The host console's web page (issue #89, docs/CONSOLE.md). No build, no framework, no third-party file. Everything that came from the
// server is written with textContent, never as HTML. The token comes in the link's fragment (#t=...), moves to sessionStorage (a reload
// keeps working, closing the tab forgets it) and leaves the address bar at once.
"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000, JOB_MS = 1000;
let view = "status", timer = null, current = null;   // current: {verb, action, args, plan}

function token() {
  const m = location.hash.match(/^#t=([A-Za-z0-9_-]{20,})$/);
  if (m) {
    try { sessionStorage.setItem("t", m[1]); } catch (e) { /* storage off: the token lives in this page only */ window.__t = m[1]; }
    history.replaceState(null, "", location.pathname);
  }
  try { return sessionStorage.getItem("t") || window.__t || ""; } catch (e) { return window.__t || ""; }
}

async function api(method, path, body) {
  const opts = { method, cache: "no-store", credentials: "omit", headers: { Authorization: "Bearer " + token() } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch("/api/v1/" + path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { data = { message: "no JSON from the console (HTTP " + r.status + ")" }; }
  if (r.status === 401 || r.status === 429) banner(data.message || "not allowed");
  else if (r.ok) banner("");
  return { status: r.status, data };
}

function banner(text) {
  $("banner").textContent = text;
  $("banner").hidden = !text;
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

function cell(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number" && v > 1e9 && v < 1e11) return new Date(v * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function table(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return el("p", "nothing here", "muted");
  const cols = [];
  rows.slice(0, 50).forEach((r) => Object.keys(r || {}).forEach((k) => { if (!cols.includes(k) && cols.length < 9) cols.push(k); }));
  const t = el("table"), head = el("tr");
  cols.forEach((c) => head.appendChild(el("th", c)));
  t.appendChild(head);
  rows.forEach((r) => {
    const tr = el("tr");
    cols.forEach((c) => tr.appendChild(el("td", cell((r || {})[c]))));
    t.appendChild(tr);
  });
  const wrap = el("div", null, "scroll");
  wrap.appendChild(t);
  return wrap;
}

function pairs(obj) {
  return table(Object.keys(obj || {}).map((k) => ({ field: k, value: obj[k] })));
}

function listOf(data) {
  if (Array.isArray(data)) return data;
  for (const k of ["requests", "leases", "players", "parties", "events", "lines", "items"]) if (Array.isArray((data || {})[k])) return data[k];
  return null;
}

async function show() {
  const path = view === "events" || view === "audit" ? view + "?limit=100" : view;
  const { status, data } = await api("GET", path);
  const box = $("view");
  box.replaceChildren(el("h2", $("tabs").querySelector(`[data-view="${view}"]`).textContent));
  if (status !== 200) { box.appendChild(el("p", (data && data.message) || "HTTP " + status, "error")); return; }
  if (view === "status") {   // the switches and counters; the leases and the queue have views of their own
    const flat = {};
    Object.keys(data || {}).forEach((k) => { if (data[k] === null || typeof data[k] !== "object") flat[k] = data[k]; });
    box.appendChild(pairs(flat));
    summary(data);
    return;
  }
  const rows = listOf(data);
  box.appendChild(rows ? table(view === "events" || view === "audit" ? rows.slice().reverse() : rows) : pairs(data));
}

function summary(s) {
  const bits = [];
  if ("slots_used" in s) bits.push(`${s.slots_used}/${s.slots_total} places`);
  if ("waiting" in s) bits.push(`${s.waiting} waiting`);
  if ("auto" in s) bits.push(s.auto ? "auto on" : "auto off");
  if (s.panic) bits.push("PANIC");
  $("summary").textContent = bits.join(" · ") || "connected";
}

function poll() {
  clearTimeout(timer);
  show().catch((e) => banner("the console did not answer: " + e.message)).finally(() => { timer = setTimeout(poll, POLL_MS); });
}

async function loadActions() {
  const { status, data } = await api("GET", "actions");
  const box = $("actions");
  box.replaceChildren();
  if (status !== 200 || !Array.isArray(data)) return;
  data.forEach((a) => {
    const b = el("button", a.verb, a.confirm === "none" ? "" : "danger");
    b.type = "button";
    b.title = a.summary;
    b.addEventListener("click", () => openAction(a));
    box.appendChild(b);
  });
}

function openAction(a) {
  current = { verb: a.verb, action: a, args: {}, plan: null };
  $("dtitle").textContent = a.verb;
  $("dsummary").textContent = a.summary;
  $("fields").replaceChildren();
  Object.keys(a.params || {}).forEach((k) => {
    const schema = a.params[k], label = el("label", k + (a.required.includes(k) ? " (required)" : "") + " ");
    let input;
    if (schema.enum) {
      input = el("select");
      schema.enum.forEach((v) => {
        const o = el("option", v);
        o.value = v;
        const only = ((a.arg_roles || {})[k] || {})[v];
        if (only && !only.includes("console")) { o.disabled = true; o.textContent = v + " (on the host only)"; }
        if (schema.default === v) o.selected = true;
        input.appendChild(o);
      });
    } else {
      input = el("input");
      input.autocomplete = "off";
      if (schema.maxLength) input.maxLength = schema.maxLength;
    }
    input.name = k;
    label.appendChild(input);
    $("fields").appendChild(label);
  });
  $("plan").hidden = true;
  $("typedlabel").hidden = true;
  $("typed").value = "";
  error("");
  $("go").textContent = a.confirm === "none" ? "Run" : "Plan";
  $("dialog").showModal();
}

function error(text) {
  $("derror").textContent = text;
  $("derror").hidden = !text;
}

function args() {
  const out = {};
  $("fields").querySelectorAll("input, select").forEach((i) => { if (i.value !== "") out[i.name] = i.value; });
  return out;
}

function showPlan(p) {
  current.plan = p;
  $("will").replaceChildren(...(p.will || []).map((w) => el("li", w)));
  $("plan").hidden = false;
  $("typedlabel").hidden = !p.type_to_confirm;
  $("typedwant").textContent = p.type_to_confirm || "";
  $("go").textContent = current.verb === "panic" ? "Panic now" : "Confirm";
  $("fields").querySelectorAll("input, select").forEach((i) => { i.disabled = true; });
}

async function go() {
  error("");
  $("go").disabled = true;
  try {
    let r;
    if (!current.plan) {
      current.args = args();
      r = await api("POST", "actions/" + current.verb, { args: current.args });
      if (r.status === 200) { showPlan(r.data); return; }
    } else {
      const body = { args: current.args, confirm: current.plan.confirm };
      if (current.plan.type_to_confirm) body.typed = $("typed").value;
      r = await api("POST", "actions/" + current.verb, body);
      if (r.status === 409 && r.data.error === "plan-changed") {
        const again = await api("POST", "actions/" + current.verb, { args: current.args });
        if (again.status === 200) { showPlan(again.data); error("The state changed: this is the new plan."); return; }
        r = again;
      }
    }
    if (r.status === 202) { $("dialog").close(); follow(r.data); return; }
    error((r.data && r.data.message) || "HTTP " + r.status);
  } catch (e) {
    error("the console did not answer: " + e.message);
  } finally {
    $("go").disabled = false;
  }
}

async function follow(job) {
  $("jobbox").hidden = false;
  for (;;) {
    $("jobstate").textContent = `${job.verb} (job ${job.id}): ${job.state}` + (job.exit !== undefined && job.exit !== null ? `, exit ${job.exit}` : "") +
      (job.error ? `, ${job.error}` : "");
    $("joboutput").textContent = job.output || [job.stdout, job.stderr].filter(Boolean).join("\n");
    if (job.state !== "queued" && job.state !== "running") break;
    await new Promise((ok) => setTimeout(ok, JOB_MS));
    const r = await api("GET", "jobs/" + job.id);
    if (r.status !== 200) { $("jobstate").textContent = (r.data && r.data.message) || "HTTP " + r.status; return; }
    job = r.data;
  }
  poll();
}

document.addEventListener("DOMContentLoaded", () => {
  token();
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (!b) return;
    view = b.dataset.view;
    $("tabs").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    poll();
  });
  $("cancel").addEventListener("click", () => $("dialog").close());
  $("go").addEventListener("click", go);
  if (!token()) banner("No token: on the host, run `mesh host console url` and open the link it prints.");
  loadActions();
  poll();
});
