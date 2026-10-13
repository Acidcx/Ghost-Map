"use strict";
// Maintenance pages: service items (a counter, an interval and a log of the work done) and the counters behind
// them (feet, pieces, production hours and hole hits from TSC; run hours measured from the PLC).
// Uses $, esc and api from app.js, and pNum, fmtHm and prodLoadLines' line list from production.js.

const MAINT_POLL_MS = 60000;
const maint = { data: null, error: null, timer: null, lines: null, dashboards: null, counters: {}, ctLine: null, editing: null, open: new Set(),
  ctTimer: null, ctTries: 0 };
const CT_RETRY_MS = [5000, 10000, 20000, 40000, 60000];
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
      ? "Press <b>Add item</b> for each job with an interval: a shear blade every so many cuts, a punch every so many hole hits, rollformer grease every so many feet, hydraulic oil every so many run hours."
      : "Ask an admin to add the jobs that have a service interval."}</p></div>`;
    return;
  }
  const n = maint.data.items.length, healthy = Math.round((count("ok") / n) * 100);
  const tile = (st, cls, label) => `<div class="tile ringtile ${count(st) ? cls : ""}">${ring((count(st) / n) * 100, count(st), count(st) ? cls : "", 54, 6)}
    <div><div class="l">${label}</div><div class="small muted">of ${n} items</div></div></div>`;
  const tiles = `<div class="tiles">${tile("overdue", "err", "Overdue")}${tile("soon", "warn", "Due soon")}
    <div class="tile ringtile ok">${ring(healthy, `${healthy}%`, "ok", 54, 6)}<div><div class="l">On schedule</div><div class="small muted">${count("ok")} of ${n} items</div></div></div></div>`;
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
      <div class="svhead">${ring(it.pct ?? 0, it.pct == null ? "?" : `${Math.round(it.pct)}%`, it.state || "", 46, 5)}<div><b>${esc(it.name)}</b> <span class="chip ${chip}">${esc(word)}</span>
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
  clearTimeout(maint.ctTimer);
  if ($("#tab-counters").classList.contains("hidden")) return;
  const lines = await maintLines();
  $("#ctLine").innerHTML = lines.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
  if (!lines.some((l) => l.id === maint.ctLine)) maint.ctLine = lines[0]?.id ?? null;
  if (maint.ctLine != null) $("#ctLine").value = maint.ctLine;
  $("#ctLine").closest("label").classList.toggle("hidden", !lines.length);
  // A first read of a big TSC database can take a while: count the seconds so it doesn't look stuck.
  const t0 = Date.now();
  const tick = () => {
    const el = $("#ctWait");
    if (!el) return;
    el.textContent = `${Math.round((Date.now() - t0) / 1000)} s`;
    maint.ctTimer = setTimeout(tick, 1000);
  };
  if (!$("#ctBody").querySelector(".odos")) {
    $("#ctBody").innerHTML = `<div class="panel probescan"><div class="scanline"></div><p class="muted small">Adding up the counters... <span id="ctWait" class="mono"></span></p>
      <p class="small muted">The first read of a big TSC database can take a minute; after that the page shows the last answer at once and refreshes it in the background.</p></div>`;
    tick();
  }
  try {
    const c = await ctFetch(maint.ctLine);
    clearTimeout(maint.ctTimer);
    maint.ctTries = 0;
    ctRender(c);
    // A stale answer is being refreshed on the server: look again shortly for the new one.
    if (c.tsc && c.tsc.stale) { delete maint.counters[maint.ctLine == null ? "-" : String(maint.ctLine)]; maint.ctTimer = setTimeout(ctPoll, 15000); }
  } catch (e) {
    clearTimeout(maint.ctTimer);
    const wait = CT_RETRY_MS[Math.min(maint.ctTries++, CT_RETRY_MS.length - 1)];
    $("#ctBody").innerHTML = `<div class="panel"><p class="errtext small">${esc(e.message)}</p>
      <p class="small muted">Trying again in ${Math.round(wait / 1000)} s. <button class="btn ghost small" id="ctRetry" type="button">Try now</button></p></div>`;
    $("#ctRetry").addEventListener("click", ctPoll);
    maint.ctTimer = setTimeout(ctPoll, wait);
  }
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "counters") ctPoll(); else clearTimeout(maint.ctTimer); });
$("#ctLine").addEventListener("change", (e) => { maint.ctLine = Number(e.target.value); $("#ctBody").innerHTML = ""; ctPoll(); });

function ctRender(c) {
  const t = c.tsc;
  let html = "";
  $("#ctNote").innerHTML = t && t.ok && t.source === "demo"
    ? `<p class="small muted">Simulated TSC line (server set to <b>demo</b>): longer periods are extrapolated from the last 14 hours.</p>` : "";
  $("#ctLive").innerHTML = t && t.ok ? `Added up ${esc(fmtHm(t.at))}${t.stale ? ` <span class="chip pulse">refreshing</span>` : ""}` : "";
  if (!t) html += `<div class="panel"><h3>Production counters</h3><p class="small muted">No TSC connection: set one up under Production &gt; Connection for feet, pieces and hole hits.</p></div>`;
  else if (!t.ok) html += `<div class="panel"><h3>Production counters</h3><p class="errtext small">Can't read the TSC database: ${esc(t.error)}</p></div>`;
  else {
    const strokesAll = (t.strokes || []).reduce((a, s) => a + (s.all || 0), 0);
    const runAll = c.machines.reduce((a, m) => a + (m.run_h || 0), 0);
    html += `<div class="odos">${odo("Feet made, all time", t.periods.all.feet)}${odo("Pieces made, all time", t.periods.all.pieces)}
      ${t.strokes_ok ? odo("Hole hits (estimate)", strokesAll) : ""}${odo("Production hours", t.periods.all.prod_hours, 1)}
      ${c.machines.length ? odo("PLC run hours", runAll, 1) : ""}</div>`;
    const P = t.periods, cols = [["24h", "Last 24 h"], ["7d", "Last 7 days"], ["30d", "Last 30 days"], ["all", "All time"]];
    const row = (label, k, dp = 0) => `<tr><td>${esc(label)}</td>${cols.map(([c2]) => `<td>${pNum(P[c2][k], dp)}</td>`).join("")}</tr>`;
    html += `<div class="panel"><h3>Production counters (from TSC)</h3><div class="tablewrap"><table class="ptable ctable"><thead><tr><th></th>
      ${cols.map(([, l]) => `<th>${esc(l)}</th>`).join("")}</tr></thead><tbody>
      ${row("Feet made", "feet")}${row("Pieces made", "pieces")}${row("Parts finished", "parts")}
      ${row("Production hours", "prod_hours", 1)}${row("Scrap pieces", "scrap_pieces")}</tbody></table></div>
      <p class="small muted">From completed parts in TSC. Production hours add up start to end of each part, so stops between parts don't count.
        ${P.all.first ? `TSC's completed parts go back to ${esc(fmtDate(P.all.first))}.` : ""}</p></div>`;
    if (!t.strokes_ok) html += `<div class="panel"><h3>Hole hits per tool</h3><p class="small muted">The SQL login can't read TSC's pattern, hole and notch tables yet.
      See <b>What Ghost Map can read</b> on Production &gt; Connection for the grant.</p></div>`;
    else {
      const max = Math.max(1, ...t.strokes.map((s) => s.all));
      html += `<div class="panel"><h3>Hole hits per tool (estimate)</h3><div class="tablewrap"><table class="ptable"><thead><tr><th>Station</th><th>Tool</th><th></th>
        <th>Last 30 days</th><th>All time</th><th></th></tr></thead><tbody>${t.strokes.map((s) => `<tr><td>${esc(s.station_name)}</td>
        <td class="mono">${esc(s.tool || "-")}</td><td>${esc(s.tool_name)}</td><td>${pNum(s["30d"])}</td><td><b>${pNum(s.all)}</b></td>
        <td><div class="pbarwrap sm"><span style="width:${Math.round((s.all / max) * 100)}%"></span></div></td></tr>`).join("")
        || `<tr><td colspan="6" class="muted">No holes or notches on the completed parts.</td></tr>`}</tbody></table></div>
        <p class="small muted">Pieces made times the holes and notches on each part, counting repeat patterns along the length: one hit per hole, per tool type. A gang die that punches several holes in one stroke is counted once per hole, so set die-life intervals in hits, not press strokes. Pieces made include scrap.</p></div>`;
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

// A mechanical-counter look for the all-time totals: one box per digit.
function odo(label, v, dp = 0) {
  const txt = Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
  return `<div class="odo"><div class="l">${esc(label)}</div><div class="digits${txt.length > 9 ? " long" : ""}" aria-label="${esc(txt)}">${[...txt].map((ch) =>
    /\d/.test(ch) ? `<span>${ch}</span>` : `<span class="sep">${esc(ch)}</span>`).join("")}</div></div>`;
}
