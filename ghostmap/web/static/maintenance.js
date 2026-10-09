"use strict";
// Maintenance pages: service items (a counter, an interval and a log of the work done) and the counters behind
// them (feet, pieces, production hours and punch strokes from TSC; run hours measured from the PLC).
// Uses $, esc and api from app.js, and pNum, fmtHm and prodLoadLines' line list from production.js.

const MAINT_POLL_MS = 60000;
const maint = { data: null, error: null, timer: null, lines: null, dashboards: null, counters: {}, ctLine: null, editing: null, open: new Set() };
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" }) : "-");
const STATE_WORD = { overdue: ["errchip", "overdue"], soon: ["warnchip", "due soon"], ok: ["okchip", "ok"] };
const STATE_ORDER = { overdue: 0, soon: 1, ok: 2 };

async function maintLines() {
  if (maint.lines) return maint.lines;
  try {
    const r = await api("api/tsc/lines", { timeout: 40000 });
    maint.lines = r.ok ? r.lines : [];
  } catch (_) { maint.lines = []; }
  return maint.lines;
}

async function maintDashboards() {
  if (!maint.dashboards) {
    try { maint.dashboards = await api("api/dashboards"); } catch (_) { maint.dashboards = []; }
  }
  return maint.dashboards;
}

// ------------------------------------------------------------------ service items
async function svPoll() {
  clearTimeout(maint.timer);
  if (document.hidden || $("#tab-service").classList.contains("hidden")) return;
  try {
    maint.data = await api("api/maintenance", { timeout: 90000 });
    maint.error = null;
  } catch (e) { maint.error = e.message; }
  await Promise.all([maintLines(), maintDashboards()]);
  svRender();
  maint.timer = setTimeout(svPoll, MAINT_POLL_MS);
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "service") svPoll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#tab-service").classList.contains("hidden")) svPoll(); });
$("#svFilter").addEventListener("change", svRender);

function svWhere(it) {
  const line = (maint.lines || []).find((l) => l.id === it.line);
  const lineName = line ? line.name : it.line != null ? `Line ${it.line}` : "";
  if (it.kind === "strokes") return `${lineName}, station ${it.station}${it.tool ? `, ${it.tool}` : ", every tool"}`;
  if (it.kind === "run_hours" || it.kind === "online_hours") {
    const d = (maint.dashboards || []).find((x) => x.id === it.dash);
    return d ? d.name : "machine dashboard";
  }
  return it.kind === "days" ? "" : lineName;
}

function svRender() {
  if (maint.error) { $("#svBody").innerHTML = `<div class="panel"><p class="errtext small">${esc(maint.error)}</p></div>`; return; }
  if (!maint.data) { $("#svBody").innerHTML = `<div class="panel"><p class="muted small">Reading the counters...</p></div>`; return; }
  const admin = !document.body.classList.contains("viewer");
  let items = [...maint.data.items].sort((a, b) => (STATE_ORDER[a.state] ?? 3) - (STATE_ORDER[b.state] ?? 3) || (b.pct ?? 0) - (a.pct ?? 0));
  const count = (st) => items.filter((x) => x.state === st).length;
  if ($("#svFilter").value === "due") items = items.filter((x) => x.state === "overdue" || x.state === "soon");
  if (!maint.data.items.length) {
    $("#svBody").innerHTML = `<div class="panel"><h3>No service items yet</h3><p class="small muted">${admin
      ? "Press <b>Add item</b> for each job with an interval: a shear blade every so many cuts, a punch every so many strokes, rollformer grease every so many feet, hydraulic oil every so many run hours."
      : "Ask an admin to add the jobs that have a service interval."}</p></div>`;
    return;
  }
  const tiles = `<div class="tiles">
    <div class="tile ${count("overdue") ? "err" : ""}"><div class="n">${count("overdue")}</div><div class="l">Overdue</div></div>
    <div class="tile ${count("soon") ? "warn" : ""}"><div class="n">${count("soon")}</div><div class="l">Due soon</div></div>
    <div class="tile ok"><div class="n">${count("ok")}</div><div class="l">OK</div></div></div>`;
  const rows = items.map((it) => {
    const [chip, word] = STATE_WORD[it.state] || ["", "can't read"];
    const pct = Math.min(it.pct ?? 0, 100);
    const unit = it.unit === "h" ? "h" : ` ${it.unit}`;
    const value = it.value == null ? `<span class="errtext">${esc(it.error || "can't read the counter")}</span>`
      : `<b>${pNum(it.value, it.unit === "h" || it.unit === "days" ? 1 : 0)}</b> of ${pNum(it.interval)}${esc(unit)}`;
    const left = it.remaining == null ? "" : it.remaining >= 0 ? `${pNum(it.remaining, it.unit === "h" || it.unit === "days" ? 1 : 0)}${esc(unit)} to go`
      : `${pNum(-it.remaining, it.unit === "h" || it.unit === "days" ? 1 : 0)}${esc(unit)} past due`;
    const last = it.log && it.log.length ? it.log[0] : null;
    const open = maint.open.has(it.id);
    const log = open ? `<div class="svlog">${it.log.length ? `<table class="ptable"><thead><tr><th>Done</th><th>By</th><th>Reading</th><th>Note</th></tr></thead><tbody>${it.log.map((l) =>
      `<tr><td>${esc(fmtDate(l.at))} ${esc(fmtHm(l.at))}</td><td>${esc(l.by || "-")}</td><td>${l.reading != null ? `${pNum(l.reading)}${esc(unit)}` : "-"}</td><td>${esc(l.note || "")}</td></tr>`).join("")}</tbody></table>`
      : `<p class="small muted">Not marked done in Ghost Map yet.</p>`}${it.notes ? `<p class="small">${esc(it.notes)}</p>` : ""}</div>` : "";
    return `<div class="svitem ${esc(it.state || "unknown")}">
      <div class="svhead"><div><b>${esc(it.name)}</b> <span class="chip ${chip}">${esc(word)}</span>
        <div class="small muted">${esc(it.kind_label)}${svWhere(it) ? ` &middot; ${esc(svWhere(it))}` : ""} &middot; every ${pNum(it.interval)}${esc(unit)}</div></div>
        <span class="grow"></span>
        <button class="btn ghost small" data-svlog="${esc(it.id)}">${open ? "Hide" : "History"}</button>
        ${admin ? `<button class="btn ghost small" data-svedit="${esc(it.id)}">Edit</button><button class="btn small" data-svdone="${esc(it.id)}">Mark done</button>` : ""}</div>
      <div class="svbar"><div class="pbarwrap"><span class="${esc(it.state || "")}" style="width:${pct}%"></span></div>
        <span class="small">${value}</span><span class="small muted">${esc(left)}</span></div>
      <div class="small muted">Last done ${esc(fmtDate(it.last_service))}${last && last.by ? ` by ${esc(last.by)}` : ""}</div>${log}</div>`;
  }).join("");
  $("#svBody").innerHTML = tiles + `<div class="panel">${rows || `<p class="small muted">Nothing due.</p>`}</div>`;
}

$("#svBody").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.svlog) {
    maint.open.has(b.dataset.svlog) ? maint.open.delete(b.dataset.svlog) : maint.open.add(b.dataset.svlog);
    svRender();
  } else if (b.dataset.svedit) svOpen(maint.data.items.find((x) => x.id === b.dataset.svedit));
  else if (b.dataset.svdone) svDone(maint.data.items.find((x) => x.id === b.dataset.svdone));
});

// ------------------------------------------------------------------ add / edit dialog
async function svStations(line) {
  const f = $("#svForm");
  f.station.innerHTML = "";
  $("#svTools").innerHTML = "";
  if (line == null || Number.isNaN(line)) return;
  const c = await ctFetch(line);
  const t = c && c.tsc && c.tsc.ok ? c.tsc : null;
  const stations = t ? t.stations : [];
  f.station.innerHTML = (stations.length ? stations : [1, 2, 3].map((id) => ({ id, name: `Station ${id}` })))
    .map((s) => `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.id)})</option>`).join("");
  $("#svTools").innerHTML = (t ? t.tools : []).map((x) => `<option value="${esc(x.tool)}">${esc(x.name)}</option>`).join("");
}

function svKindToggle() {
  const f = $("#svForm"), kind = f.kind.value, src = maint.data?.kinds?.[kind]?.source;
  document.querySelector("#svForm .svtsc").classList.toggle("hidden", src !== "tsc");
  document.querySelectorAll("#svForm .svstroke").forEach((x) => x.classList.toggle("hidden", kind !== "strokes"));
  document.querySelector("#svForm .svplc").classList.toggle("hidden", src !== "plc");
  const unit = maint.data?.kinds?.[kind]?.unit || "";
  f.interval.closest("label").firstChild.textContent = `Every (${unit}) `;
}

async function svOpen(item) {
  if (!maint.data) return;
  maint.editing = item || null;
  const f = $("#svForm");
  f.reset();
  $("#svErr").textContent = "";
  $("#svTitle").textContent = item ? "Edit service item" : "Add a service item";
  f.kind.innerHTML = Object.entries(maint.data.kinds).map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join("");
  const lines = await maintLines();
  f.line.innerHTML = lines.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("") || `<option value="">no TSC connection</option>`;
  const dashes = await maintDashboards();
  f.dash.innerHTML = dashes.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("") || `<option value="">no machine dashboards yet</option>`;
  if (item) {
    for (const k of ["name", "kind", "interval", "warn_pct", "tool", "notes"]) f.elements[k].value = item[k] ?? "";
    if (item.line != null) f.line.value = item.line;
    if (item.dash) f.dash.value = item.dash;
  }
  // What an item counts and where can't change after it is made (its history would stop making sense).
  for (const k of ["kind", "line", "station", "dash"]) f.elements[k].disabled = !!item;
  document.querySelector("#svForm .svnew").classList.toggle("hidden", !!item);
  $("#svDelete").classList.toggle("hidden", !item);
  svKindToggle();
  $("#svDialog").showModal();
  await svStations(Number(f.line.value));
  if (item && item.station != null) f.station.value = item.station;
}
$("#svAdd").addEventListener("click", () => svOpen(null));
$("#svCancel").addEventListener("click", () => $("#svDialog").close());
$("#svForm").kind.addEventListener("change", svKindToggle);
$("#svForm").line.addEventListener("change", (e) => svStations(Number(e.target.value)));

$("#svForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = $("#svForm");
  $("#svErr").textContent = "";
  try {
    if (maint.editing) {
      await api(`api/maintenance/items/${encodeURIComponent(maint.editing.id)}`, { method: "POST", body: JSON.stringify({
        name: f.elements.name.value.trim(), interval: Number(f.interval.value), warn_pct: Number(f.warn_pct.value) || 90,
        tool: f.tool.value.trim(), notes: f.notes.value.trim() }) });
    } else {
      const src = maint.data.kinds[f.kind.value].source;
      const last = f.last_service.value ? new Date(`${f.last_service.value}T00:00:00`).getTime() / 1000 : null;
      await api("api/maintenance/items", { method: "POST", body: JSON.stringify({
        name: f.elements.name.value.trim(), kind: f.kind.value, interval: Number(f.interval.value), warn_pct: Number(f.warn_pct.value) || 90,
        line: src === "tsc" && f.line.value !== "" ? Number(f.line.value) : null,
        station: f.kind.value === "strokes" && f.station.value !== "" ? Number(f.station.value) : null,
        tool: f.kind.value === "strokes" ? f.tool.value.trim() : "", dash: src === "plc" ? f.dash.value : "",
        notes: f.notes.value.trim(), offset: Number(f.offset.value) || 0, last_service: last }) });
    }
    $("#svDialog").close();
    svPoll();
  } catch (e) { $("#svErr").textContent = e.message; }
});

$("#svDelete").addEventListener("click", async () => {
  const it = maint.editing;
  if (!it || !confirm(`Delete "${it.name}" and its service history?`)) return;
  try {
    await api(`api/maintenance/items/${encodeURIComponent(it.id)}`, { method: "DELETE" });
    $("#svDialog").close();
    svPoll();
  } catch (e) { $("#svErr").textContent = e.message; }
});

// ------------------------------------------------------------------ mark done
function svDone(item) {
  maint.editing = item;
  $("#svDoneForm").reset();
  $("#svDoneErr").textContent = "";
  $("#svDoneTitle").textContent = `Mark done: ${item.name}`;
  $("#svDoneInfo").textContent = item.value != null
    ? `The count is at ${pNum(item.value)} ${item.unit} of ${pNum(item.interval)}. It is logged with your note and starts again from 0.`
    : "It is logged with your note and the count starts again from 0.";
  $("#svDoneDialog").showModal();
}
$("#svDoneCancel").addEventListener("click", () => $("#svDoneDialog").close());
$("#svDoneForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api(`api/maintenance/items/${encodeURIComponent(maint.editing.id)}/service`, { method: "POST",
      body: JSON.stringify({ note: $("#svDoneForm").note.value }), timeout: 90000 });
    maint.open.add(maint.editing.id);
    $("#svDoneDialog").close();
    svPoll();
  } catch (e) { $("#svDoneErr").textContent = e.message; }
});

// ------------------------------------------------------------------ counters
async function ctFetch(line) {
  const key = line == null ? "-" : String(line);
  const hit = maint.counters[key];
  if (hit && Date.now() - hit.at < 60000) return hit.data;
  const data = await api(`api/maintenance/counters${line == null ? "" : `?line=${encodeURIComponent(line)}`}`, { timeout: 120000 });
  maint.counters[key] = { at: Date.now(), data };
  return data;
}

async function ctPoll() {
  if ($("#tab-counters").classList.contains("hidden")) return;
  const lines = await maintLines();
  $("#ctLine").innerHTML = lines.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
  if (!lines.some((l) => l.id === maint.ctLine)) maint.ctLine = lines[0]?.id ?? null;
  if (maint.ctLine != null) $("#ctLine").value = maint.ctLine;
  $("#ctLine").closest("label").classList.toggle("hidden", !lines.length);
  $("#ctBody").innerHTML = `<div class="panel"><p class="muted small">Adding up the counters...</p></div>`;
  try {
    ctRender(await ctFetch(maint.ctLine));
  } catch (e) { $("#ctBody").innerHTML = `<div class="panel"><p class="errtext small">${esc(e.message)}</p></div>`; }
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "counters") ctPoll(); });
$("#ctLine").addEventListener("change", (e) => { maint.ctLine = Number(e.target.value); ctPoll(); });

function ctRender(c) {
  const t = c.tsc;
  let html = "";
  $("#ctNote").innerHTML = t && t.ok && t.source === "demo"
    ? `<p class="small muted">Simulated TSC line (server set to <b>demo</b>): longer periods are extrapolated from the last 14 hours.</p>` : "";
  $("#ctLive").textContent = t && t.ok ? `Added up ${fmtHm(t.at)}` : "";
  if (!t) html += `<div class="panel"><h3>Production counters</h3><p class="small muted">No TSC connection: set one up under Production &gt; Connection for feet, pieces and punch strokes.</p></div>`;
  else if (!t.ok) html += `<div class="panel"><h3>Production counters</h3><p class="errtext small">Can't read the TSC database: ${esc(t.error)}</p></div>`;
  else {
    const P = t.periods, cols = [["24h", "Last 24 h"], ["7d", "Last 7 days"], ["30d", "Last 30 days"], ["all", "All time"]];
    const row = (label, k, dp = 0) => `<tr><td>${esc(label)}</td>${cols.map(([c2]) => `<td>${pNum(P[c2][k], dp)}</td>`).join("")}</tr>`;
    html += `<div class="panel"><h3>Production counters (from TSC)</h3><div class="tablewrap"><table class="ptable ctable"><thead><tr><th></th>
      ${cols.map(([, l]) => `<th>${esc(l)}</th>`).join("")}</tr></thead><tbody>
      ${row("Feet made", "feet")}${row("Pieces made (shear cuts)", "pieces")}${row("Part lines finished", "parts")}
      ${row("Production hours", "prod_hours", 1)}${row("Scrap pieces", "scrap_pieces")}</tbody></table></div>
      <p class="small muted">From completed parts in TSC. Production hours add up start to end of each part, so stops between parts don't count.
        ${P.all.first ? `TSC's completed parts go back to ${esc(fmtDate(P.all.first))}.` : ""}</p></div>`;
    if (!t.strokes_ok) html += `<div class="panel"><h3>Punch strokes</h3><p class="small muted">The SQL login can't read TSC's pattern, hole and notch tables yet.
      See <b>What Ghost Map can read</b> on Production &gt; Connection for the grant.</p></div>`;
    else {
      const max = Math.max(1, ...t.strokes.map((s) => s.all));
      html += `<div class="panel"><h3>Punch strokes</h3><div class="tablewrap"><table class="ptable"><thead><tr><th>Station</th><th>Tool</th><th></th>
        <th>Last 30 days</th><th>All time</th><th></th></tr></thead><tbody>${t.strokes.map((s) => `<tr><td>${esc(s.station_name)}</td>
        <td class="mono">${esc(s.tool || "-")}</td><td>${esc(s.tool_name)}</td><td>${pNum(s["30d"])}</td><td><b>${pNum(s.all)}</b></td>
        <td><div class="pbarwrap sm"><span style="width:${Math.round((s.all / max) * 100)}%"></span></div></td></tr>`).join("")
        || `<tr><td colspan="6" class="muted">No holes or notches on the completed parts.</td></tr>`}</tbody></table></div>
        <p class="small muted">Pieces made times the holes and notches on each part, counting repeat patterns along the length. One stroke per hole, per tool type.</p></div>`;
    }
  }
  const M = c.machines;
  html += `<div class="panel"><h3>Machine hour meters (from the PLC)</h3>${M.length ? `<div class="tablewrap"><table class="ptable"><thead><tr><th>Machine</th>
    <th>Run hours</th><th>Powered-on hours</th><th>Run, last 7 days</th><th>Running while on, 7 days</th><th>Counting since</th></tr></thead><tbody>
    ${M.map((m) => `<tr><td>${esc(m.name)} ${m.recording ? "" : `<span class="chip warnchip">not recording</span>`}</td><td><b>${pNum(m.run_h, 1)}</b></td>
      <td>${pNum(m.online_h, 1)}</td><td>${pNum(m.run_7d, 1)}</td><td>${m.online_7d ? `${Math.round((m.run_7d / m.online_7d) * 100)}%` : "-"}</td>
      <td>${esc(fmtDate(m.first))}</td></tr>`).join("")}</tbody></table></div>
    <p class="small muted">Measured by Ghost Map from each dashboard's running bit while it is recording. Time Ghost Map wasn't running isn't counted,
      so these trail the machine's own hour meter.</p>`
    : `<p class="small muted">No machine dashboards yet. Build one under Machine &gt; Tags and Ghost Map starts counting its run hours.</p>`}</div>`;
  $("#ctBody").innerHTML = html;
}
