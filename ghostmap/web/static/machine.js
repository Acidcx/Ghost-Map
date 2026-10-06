"use strict";
// Machine dashboards: layouts built on the server from a tag export, filled with live values that the
// server reads (read-only) from the OPC UA gateway. Uses $, esc and api() from app.js.

const mach = { list: [], dash: null, vals: null, timer: null, edit: false };
const POLL_MS = 2000;
const SEV_LABEL = { critical: "E-stop", fault: "fault", warning: "warning" };
const RUN_RE = /(^|_)(line_)?run(ning)?($|_)|autorun|auto_running/i;

const truthy = (v) => v === true || v === 1 || v === "true" || v === "1";

async function machineLoadList(selectId) {
  mach.list = await api("api/dashboards");
  $("#mSelect").innerHTML = mach.list.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
  $("#mEmpty").classList.toggle("hidden", mach.list.length > 0);
  $("#mDelete").classList.toggle("hidden", !mach.list.length);
  let id = selectId;
  try { id = id || localStorage.getItem("gm.dash"); } catch (_) { /* ignore */ }
  if (!mach.list.some((d) => d.id === id)) id = mach.list[0]?.id;
  if (id) {
    $("#mSelect").value = id;
    await machineShow(id);
  } else {
    mach.dash = null;
    ["#mTiles", "#mProd", "#mAreas", "#mNotes", "#mLive"].forEach((s) => { $(s).innerHTML = ""; });
  }
}

async function machineShow(id) {
  mach.dash = await api(`api/dashboards/${encodeURIComponent(id)}`);
  mach.vals = null;
  try { localStorage.setItem("gm.dash", id); } catch (_) { /* ignore */ }
  machineRender();
  machinePoll();
}

window.machineOpen = async (id) => { await machineLoadList(id); };

$("#mSelect").addEventListener("change", () => machineShow($("#mSelect").value));
$("#mEdit").addEventListener("change", () => { mach.edit = $("#mEdit").checked; machineRender(); });

// ------------------------------------------------------------------ live values
function machineSchedule() {
  clearTimeout(mach.timer);
  mach.timer = setTimeout(machinePoll, POLL_MS);
}

async function machinePoll() {
  clearTimeout(mach.timer);
  if (!mach.dash) return;
  // Only poll while someone is looking, so a forgotten browser tab doesn't keep the gateway busy.
  if (document.hidden || $("#tab-machine").classList.contains("hidden")) return machineSchedule();
  const id = mach.dash.id;
  try {
    const v = await api(`api/dashboards/${encodeURIComponent(id)}/values`);
    if (mach.dash?.id !== id) return;
    mach.vals = v;
  } catch (e) {
    mach.vals = { ok: false, error: e.message, values: {}, since: {}, bad: [] };
  }
  machineRender();
  machineSchedule();
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "machine") machinePoll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) machinePoll(); });

// ------------------------------------------------------------------ rendering
function areaState(area) {
  const ov = mach.dash.overrides || {};
  const inv = !!(ov.invert || {})[area.id];
  const hidden = new Set(ov.hidden || []);
  const v = mach.vals?.values || {};
  const bad = new Set(mach.vals?.bad || []);
  const alarms = area.alarms.filter((a) => !hidden.has(a.node_id)).map((a) => {
    const known = mach.vals?.ok && a.node_id in v && !bad.has(a.node_id);
    const on = truthy(v[a.node_id]);
    return { ...a, known, active: known && (inv ? !on : on) };
  });
  return { inv, alarms, active: alarms.filter((a) => a.active), known: alarms.some((a) => a.known) };
}

function fmtSince(nid) {
  const t = mach.vals?.since?.[nid];
  if (!t) return mach.vals?.watching_since ? "already on when Ghost Map started watching" : "";
  const d = new Date(t * 1000);
  return `since ${d.toLocaleTimeString()}`;
}

function machineRender() {
  const d = mach.dash;
  if (!d) return;
  const L = d.layout;
  const vals = mach.vals;
  const v = vals?.values || {};

  $("#mLive").innerHTML = !vals ? `reading <span class="mono">${esc(d.endpoint)}</span>...`
    : vals.ok ? `<span class="chip okchip">live</span> <span class="mono">${esc(d.endpoint)}</span> &middot; ${esc(new Date(vals.at * 1000).toLocaleTimeString())}${
      vals.bad.length ? ` &middot; <span class="errtext">${vals.bad.length} of ${Object.keys(vals.values).length} tags not readable (renamed, or not on this server?)</span>` : ""}`
      : `<span class="chip errchip">no data</span> <span class="errtext">${esc(vals.error)}</span>`;

  const notes = [...(L.notes || [])];
  $("#mNotes").innerHTML = notes.map((n) => `<p class="small muted">${esc(n)}</p>`).join("")
    + (mach.edit ? `<p class="small muted">Edit mode: flip an area when "on" means healthy, hide tags that aren't alarms (spares, test bits).</p>` : "");

  const states = L.areas.map((a) => ({ a, s: areaState(a) }));
  const active = states.flatMap(({ s }) => s.active);
  const count = (pred) => active.filter(pred).length;
  const withAlarms = states.filter(({ a }) => a.alarms.length);
  const okAreas = withAlarms.filter(({ s }) => s.known && !s.active.length).length;
  const runBit = L.areas.flatMap((a) => a.status).find((x) => RUN_RE.test(x.name));
  const tiles = [];
  if (runBit) {
    const on = truthy(v[runBit.node_id]);
    tiles.push({ n: !vals?.ok ? "?" : on ? "Running" : "Stopped", l: `Machine (${runBit.label})`, cls: !vals?.ok ? "" : on ? "ok" : "warn" });
  }
  const known = vals?.ok && vals.bad.length < Object.keys(vals.values).length;
  tiles.push(
    { n: known ? count((x) => x.severity !== "warning") : "?", l: "Active faults", cls: count((x) => x.severity !== "warning") ? "err" : "ok" },
    { n: known ? count((x) => x.severity === "warning") : "?", l: "Warnings", cls: count((x) => x.severity === "warning") ? "warn" : "ok" },
    { n: known ? count((x) => x.category === "estop") : "?", l: "E-stops", cls: count((x) => x.category === "estop") ? "err" : "ok" },
    { n: known ? count((x) => x.category === "comms") : "?", l: "Comms faults", cls: count((x) => x.category === "comms") ? "err" : "ok" },
    { n: known ? `${okAreas}/${withAlarms.length}` : "?", l: "Areas OK", cls: okAreas === withAlarms.length ? "ok" : "warn" },
  );
  $("#mTiles").innerHTML = tiles.map((t) => `<div class="tile ${known ? t.cls : ""}"><div class="n">${esc(t.n)}</div><div class="l">${esc(t.l)}</div></div>`).join("");

  // Production values (counters, analog values, status bits) for areas that have them.
  const prod = L.areas.filter((a) => a.counters.length || a.values.length || (!a.alarms.length && a.status.length));
  $("#mProd").innerHTML = prod.length ? `<div class="panel"><h3>Production</h3><div class="mvals">${prod.flatMap((a) => [
    ...a.status.map((x) => `<div class="mval" title="${esc(x.node_id)}"><span class="l">${esc(x.label)}</span><span class="dot ${truthy(v[x.node_id]) ? "on" : ""}"></span></div>`),
    ...a.counters.filter((x) => x.node_id).map((x) => `<div class="mval" title="${esc(x.node_id)}"><span class="l">${esc(x.label)}</span><span class="n">${esc(fmtNum(v[x.node_id]))}</span></div>`),
    ...a.values.map((x) => `<div class="mval" title="${esc(x.node_id)}"><span class="l">${esc(x.label)}</span><span class="n">${esc(fmtNum(v[x.node_id]))}</span></div>`),
  ]).join("")}</div></div>` : "";

  // Area cards: worst first, active alarms on top.
  const rank = ({ s }) => (s.active.some((x) => x.severity === "critical") ? 0 : s.active.some((x) => x.severity === "fault") ? 1 : s.active.length ? 2 : 3);
  const cards = states.filter(({ a }) => a.alarms.length || a.timers.length || a.words.length)
    .sort((x, y) => rank(x) - rank(y));
  $("#mAreas").innerHTML = cards.map(({ a, s }) => areaCard(a, s, v)).join("");
}

function fmtNum(x) {
  if (x === null || x === undefined) return "-";
  if (typeof x === "number") return Number.isInteger(x) ? x.toLocaleString() : x.toFixed(1);
  return String(x);
}

function areaCard(a, s, v) {
  const bad = new Set(mach.vals?.bad || []);
  const known = mach.vals?.ok && (s.known || !s.alarms.length);
  const worstSev = s.active.find((x) => x.severity === "critical") ? "err" : s.active.find((x) => x.severity === "fault") ? "err" : s.active.length ? "warn" : known ? "ok" : "";
  const alarmRow = (x) => `<div class="malarm ${x.active ? "on" : ""}" title="${esc(x.node_id)}">
      <span class="dot ${x.active ? (x.severity === "warning" ? "warnon" : "erron") : x.known ? "" : "unk"}"></span>
      <span class="l">${esc(x.label)}</span>
      ${x.active ? `<span class="sev ${x.severity === "warning" ? "warning" : "error"}">${esc(SEV_LABEL[x.severity] || x.severity)}</span><span class="small muted">${esc(fmtSince(x.node_id))}</span>` : ""}
      ${mach.edit ? `<button class="btn ghost small" data-hide="${esc(x.node_id)}" title="Hide this tag">Hide</button>` : ""}</div>`;
  const timers = a.timers.map((t) => {
    const acc = Number(v[t.members.ACC]), pre = Number(v[t.members.PRE]);
    const pct = pre > 0 ? Math.min(100, Math.round((acc / pre) * 100)) : 0;
    const dn = truthy(v[t.members.DN]);
    const tk = known && !bad.has(t.members.ACC);
    return `<div class="mtimer" title="${esc(Object.values(t.members)[0] || "")}"><span class="l">${esc(t.label)}</span>
      <span class="bar"><span style="width:${tk ? pct : 0}%"></span></span>
      <span class="small mono">${tk ? `${esc(fmtNum(acc))}/${esc(fmtNum(pre))} ms${dn ? " DN" : ""}` : "-"}</span></div>`;
  }).join("");
  const words = a.words.map((w) => {
    const n = Number(v[w.node_id]);
    return `<div class="malarm ${n ? "on" : ""}" title="${esc(w.node_id)}"><span class="dot ${n ? "erron" : ""}"></span><span class="l">${esc(w.label)}</span>
      <span class="small mono">${known ? esc(fmtNum(v[w.node_id])) : "-"}</span></div>`;
  }).join("");
  const quiet = s.alarms.filter((x) => !x.active);
  return `<div class="panel marea ${worstSev}">
    <div class="uahead"><h3><span class="dot ${worstSev === "ok" ? "okon" : worstSev === "err" ? "erron" : worstSev === "warn" ? "warnon" : "unk"}"></span> ${esc(a.title)}</h3>
      <span class="small muted">${s.active.length ? `${s.active.length} active` : known ? "OK" : "no data"} &middot; ${s.alarms.length} alarms${s.inv ? " &middot; on = healthy" : ""}</span></div>
    ${(a.hints || []).filter(() => !s.inv).map((h) => `<p class="hint small">${esc(h)}</p>`).join("")}
    ${mach.edit ? `<label class="small"><input type="checkbox" data-invert="${esc(a.id)}" ${s.inv ? "checked" : ""}> On means healthy in this area (flip)</label>` : ""}
    ${s.active.map(alarmRow).join("")}
    ${words}${timers}
    ${quiet.length ? `<details ${mach.edit ? "open" : ""}><summary class="small muted">${quiet.length} ${known ? "not active" : "alarms"}</summary>${quiet.map(alarmRow).join("")}</details>` : ""}
  </div>`;
}

// ------------------------------------------------------------------ edits (admin)
async function machineSaveOverrides(change) {
  const ov = { invert: { ...(mach.dash.overrides?.invert || {}) }, hidden: [...(mach.dash.overrides?.hidden || [])] };
  change(ov);
  mach.dash = await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/overrides`, { method: "POST", body: JSON.stringify(ov) });
  machineRender();
}
$("#mAreas").addEventListener("change", (e) => {
  const id = e.target.dataset.invert;
  if (id !== undefined) machineSaveOverrides((ov) => { ov.invert[id] = e.target.checked; });
});
$("#mAreas").addEventListener("click", (e) => {
  const nid = e.target.dataset.hide;
  if (nid) machineSaveOverrides((ov) => { ov.hidden.push(nid); });
});
$("#mDelete").addEventListener("click", async () => {
  if (!mach.dash || !confirm(`Delete the dashboard "${mach.dash.name}"? The PLC is not touched.`)) return;
  await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}`, { method: "DELETE" });
  try { localStorage.removeItem("gm.dash"); } catch (_) { /* ignore */ }
  await machineLoadList();
});

// ------------------------------------------------------------------ build from a CSV export
function parseCsv(text) {
  const rows = [];
  let row = [], field = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; } else if (c === '"') q = false; else field += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  const [head, ...body] = rows.filter((r) => r.length > 1);
  if (!head) return [];
  return body.map((r) => Object.fromEntries(head.map((h, i) => [h.trim().toLowerCase(), r[i] ?? ""])));
}

$("#mImport").addEventListener("click", () => {
  $("#mImportErr").textContent = "";
  $("#mImportDialog").showModal();
});
$("#mImportCancel").addEventListener("click", () => $("#mImportDialog").close());
$("#mImportForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try {
    const tags = parseCsv(await f.file.files[0].text());
    if (!tags.length || !("node_id" in tags[0])) throw new Error("that file has no node_id column; use a CSV from Export tags");
    const d = await api("api/dashboards", { method: "POST", body: JSON.stringify({
      name: f.name.value, endpoint: f.endpoint.value, source: f.file.files[0].name, tags }) });
    $("#mImportDialog").close();
    f.reset();
    await machineLoadList(d.id);
  } catch (e) { $("#mImportErr").textContent = e.message; }
});

machineLoadList().catch(() => { /* not logged in yet, or no dashboards */ });
