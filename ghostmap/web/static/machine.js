"use strict";
// Machine dashboards: layouts built on the server from a tag export, filled with live values that the
// server reads (read-only) from the OPC UA gateway. Uses $, esc and api() from app.js.

const mach = { list: [], dash: null, vals: null, timer: null, edit: false, axisDetail: {}, axisOpen: new Set(), events: [],
  verify: null, verifying: false, filter: "", onlyActive: false };
const POLL_MS = 2000;
const SEV_LABEL = { critical: "E-stop", fault: "fault", warning: "warning" };
const RUN_RE = /(^|_)(line_)?run(ning)?($|_)|autorun|auto_running/i;
const KINDS = ["alarms", "axes", "words", "timers", "counters", "values", "status"];
const KIND_LABEL = { alarms: "Alarm", axes: "Axis", words: "Fault word", timers: "Timer", counters: "Counter", values: "Value", status: "Status bit" };
const CATEGORIES = ["estop", "comms", "guard", "motor", "temperature", "hydraulic", "drive", "power", "air", "other"];
// CIP Motion axis states (CIPAxisState)
const AXIS_STATES = ["Initializing", "Pre-charge", "Stopped", "Starting", "Running", "Testing", "Stopping",
  "Aborting", "Major faulted", "Start inhibited", "Shutdown"];
const AXIS_FAULT_WORDS = ["AxisFault", "CIPAxisFaults", "ModuleFaults", "GuardFaults", "MotionFaultStatus",
  "CIPInitializationFaults", "CIPAPRFaults", "AxisSafetyFaults"];

const shortId = (n) => String(n || "").replace(/^ns=\d+;s=/, "");
const truthy = (v) => v === true || v === 1 || v === "true" || v === "1";
// Full label plus the raw tag name, for the tooltip on truncated labels.
const tip = (x) => esc([x.label, x.name !== x.label ? x.name : "", x.node_id || ""].filter(Boolean).join("\n"));

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
    ["#mTiles", "#mAreas", "#mNotes", "#mLive"].forEach((s) => { $(s).innerHTML = ""; });
  }
}

async function machineShow(id) {
  mach.dash = await api(`api/dashboards/${encodeURIComponent(id)}`);
  mach.vals = null;
  mach.axisDetail = {};
  mach.axisOpen = new Set();
  mach.events = [];
  mach.verify = null;
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
    // A read that hangs counts as no data: better "?" than old values that look live.
    const v = await api(`api/dashboards/${encodeURIComponent(id)}/values`, { timeout: 10000 });
    if (mach.dash?.id !== id) return;
    mach.vals = v;
    if (machineView() === "health") mach.events = (await api(`api/dashboards/${encodeURIComponent(id)}/health`)).events;
    if (machineView() === "drives") await Promise.all([...mach.axisOpen].map(axisLoad));
  } catch (e) {
    mach.vals = { ok: false, error: e.message, values: {}, since: {}, bad: [], health: mach.vals?.health };
  }
  machineRender();
  machineSchedule();
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "machine") machinePoll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) machinePoll(); });

// ------------------------------------------------------------------ state
function axisState(x) {
  const v = mach.vals?.values || {};
  const m = x.members || {};
  const state = m.CIPAxisState in v ? Number(v[m.CIPAxisState]) : null;
  const faultWords = AXIS_FAULT_WORDS.filter((k) => m[k] && Number(v[m[k]]));
  return { state, faulted: state === 8 || faultWords.length > 0, faultWords, known: !!mach.vals?.ok && m.CIPAxisState in v };
}

function areaState(area) {
  const ov = mach.dash.overrides || {};
  const inv = !!(ov.invert || {})[area.id];
  const hidden = new Set(ov.hidden || []);
  const v = mach.vals?.values || {};
  const bad = new Set(mach.vals?.bad || []);
  const alarms = area.alarms.filter((a) => !hidden.has(a.node_id)).map((a) => {
    const known = mach.vals?.ok && a.node_id in v && !bad.has(a.node_id);
    const on = truthy(v[a.node_id]);
    // ok_when_on: a bit like No_Faults that is on when healthy; the area flip turns it around again.
    return { ...a, known, active: known && (inv !== !!a.ok_when_on ? !on : on) };
  });
  const axes = (area.axes || []).map((x) => ({ ...x, st: axisState(x) }));
  return { inv, alarms, axes, active: alarms.filter((a) => a.active), faultedAxes: axes.filter((x) => x.st.faulted),
    known: alarms.some((a) => a.known) || axes.some((x) => x.st.known) };
}

function fmtSince(nid) {
  const t = mach.vals?.since?.[nid];
  if (!t) return mach.vals?.watching_since ? "on at start" : "";
  return `since ${new Date(t * 1000).toLocaleTimeString()}`;
}

function fmtNum(x) {
  if (x === null || x === undefined) return "-";
  if (typeof x === "number") return Number.isInteger(x) ? x.toLocaleString() : x.toFixed(1);
  return String(x);
}

// ------------------------------------------------------------------ rendering
const ITEM_LISTS = ["alarms", "axes", "words", "timers", "counters", "values", "status"];
// Top folder of an area inside its program: "Faults/GC/Mod_Comm" -> "Faults", "Program:X/R05_Track/Loop" -> "R05_Track".
function areaGroup(a) {
  const parts = String(a.id).split("/").filter((p) => !/^program:/i.test(p));
  return parts[0] || "Other";
}
function machGroupOpen(key) {
  try { return localStorage.getItem(`gm.dash.group.${mach.dash.id}.${key}`) === "1"; } catch (_) { return false; }
}
// Three levels so a whole controller stays readable: Overview (tiles, what's active now, one card per
// program), Drives (every axis in one table), and one page per program with its area cards and signals.
const ACTIVE_KINDS = ["alarms", "axes", "words", "timers"];
const sectionName = (s) => (s === "Controller" ? "Controller tags" : s.replace(/_/g, " "));
const hasHealth = (a) => ACTIVE_KINDS.some((k) => (a[k] || []).length);

function machineView() {
  try { return localStorage.getItem(`gm.dash.view.${mach.dash.id}`) || "overview"; } catch (_) { return "overview"; }
}
function machineSetView(view) {
  try { localStorage.setItem(`gm.dash.view.${mach.dash.id}`, view); } catch (_) { /* ignore */ }
  machineRender();
  window.scrollTo(0, 0);
}

function runningState(L, v, known) {
  const m = L.machine || {};
  const ids = m.running || [];
  if (!ids.length) {
    const guess = L.areas.flatMap((a) => a.status).find((x) => RUN_RE.test(x.name));  // dashboards built before
    if (guess) ids.push(guess.node_id);
  }
  if (!ids.length) return null;
  const on = ids.map((n) => truthy(v[n]));
  const running = m.mode === "all" ? on.every(Boolean) : on.some(Boolean);
  return { ids, running, known: known && ids.some((n) => n in v) };
}

function machineRender() {
  const d = mach.dash;
  if (!d) return;
  const L = d.layout;
  const vals = mach.vals;
  const v = vals?.values || {};

  const H = vals?.health || {};
  const frozen = !!(vals?.ok && H.heartbeat?.frozen);
  $("#mLive").innerHTML = !vals ? `reading <span class="mono">${esc(d.endpoint)}</span>...`
    : vals.ok ? `<span class="chip ${frozen ? "errchip" : "okchip"}">${frozen ? "frozen?" : "live"}</span> <span class="mono">${esc(d.endpoint)}</span> &middot; ${esc(new Date(vals.at * 1000).toLocaleTimeString())}
      &middot; ${Object.keys(vals.values).length} tags${H.latency_ms != null ? ` in ${esc(fmtMs(H.latency_ms))}` : ""}${
      vals.bad.length ? ` &middot; <a href="#" data-view="health" class="errtext">${vals.bad.length} not readable</a>` : ""}`
      : `<span class="chip errchip">no data</span> <span class="errtext">${esc(vals.error)}</span>`;

  $("#mNotes").innerHTML = (L.notes || []).map((n) => `<details class="small muted"><summary>What was left out</summary>${esc(n)}</details>`).join("")
    + (mach.edit ? `<p class="small muted">Edit mode: click <b>Edit</b> on any item to rename it, change its tag or remove it.
      Tags are picked from what Ghost Map found when the dashboard was built. <button class="btn ghost small" data-addarea="1">Add area</button>
      <button class="btn ghost small" data-rebuild="1" title="Lay the dashboard out again with Ghost Map's current rules, from the tags stored when it was built">Rebuild layout</button></p>` : "");

  const states = L.areas.map((a, ai) => ({ a, ai, s: areaState(a) }));
  let html0 = "";
  const active = states.flatMap(({ a, s }) => s.active.map((x) => ({ ...x, area: a })));
  const faultedAxes = states.flatMap(({ a, s }) => s.faultedAxes.map((x) => ({ ...x, area: a })));
  const count = (pred) => active.filter(pred).length;
  // Frozen data (the heartbeat stopped) is treated as no data: alarms can't be trusted to show.
  const known = vals?.ok && vals.bad.length < Object.keys(vals.values).length && !frozen;
  const run = runningState(L, v, known);
  const tiles = [];
  tiles.push(run
    ? { n: !run.known ? "?" : run.running ? "Running" : "Stopped", l: "Machine", cls: run.running ? "ok" : "warn",
      title: `${run.ids.length} running tag${run.ids.length === 1 ? "" : "s"} (${(L.machine || {}).mode === "all" ? "all" : "any"} on = running)` }
    : { n: "-", l: "Machine", title: "No running tag yet: tick Edit and set one" });
  const faults = count((x) => x.severity !== "warning") + faultedAxes.length;
  const comms = commsState(vals);
  tiles.push({ n: comms.n, l: comms.sub ? `Comms · ${comms.sub}` : "Comms", cls: comms.cls, view: "health", title: comms.title, always: true });
  tiles.push(
    { n: known ? faults : "?", l: "Active faults", cls: faults ? "err" : "ok" },
    { n: known ? count((x) => x.severity === "warning") : "?", l: "Warnings", cls: count((x) => x.severity === "warning") ? "warn" : "ok" },
    { n: known ? count((x) => x.category === "estop") : "?", l: "E-stops", cls: count((x) => x.category === "estop") ? "err" : "ok" },
    { n: known ? count((x) => x.category === "comms") : "?", l: "Comms faults", cls: count((x) => x.category === "comms") ? "err" : "ok" },
  );
  const axesTotal = states.reduce((n, { s }) => n + s.axes.length, 0);
  if (axesTotal) tiles.push({ n: known ? `${axesTotal - faultedAxes.length}/${axesTotal}` : "?", l: "Axes OK", cls: faultedAxes.length ? "err" : "ok", view: "drives" });
  $("#mTiles").innerHTML = tiles.map((t, i) => `<div class="tile ${known || t.always ? t.cls || "" : ""} ${t.view ? "click" : ""}" ${t.view ? `data-view="${t.view}"` : ""}>
    <div class="n">${esc(t.n)}</div><div class="l" title="${esc(t.title || t.l)}">${esc(t.l)}</div>
    ${i === 0 && mach.edit ? `<button class="btn ghost small" data-editrun="1">Edit</button>` : ""}</div>`).join("");

  // Sections (one per PLC program) for the sub-navigation.
  const sections = [];
  for (const st of states) {
    const name = st.a.section || "Controller";
    let sec = sections.find((x) => x.name === name);
    if (!sec) sections.push(sec = { name, states: [] });
    sec.states.push(st);
  }
  const secInfo = (sec) => {
    const act = sec.states.reduce((n, { s }) => n + s.active.length + s.faultedAxes.length, 0);
    const worst = sec.states.some(({ s }) => s.active.some((x) => x.severity !== "warning") || s.faultedAxes.length) ? "err"
      : act ? "warn" : known ? "ok" : "";
    return { act, worst, alarms: sec.states.reduce((n, { s }) => n + s.alarms.length, 0) };
  };
  let view = machineView();
  if (frozen) {
    html0 = `<div class="mbanner"><b>Data may be frozen.</b> The heartbeat tag hasn't changed for ${esc(Math.round(H.heartbeat.age_s))} s,
      so the gateway may be serving old values. Faults are shown as "?" until it moves again. <a href="#" data-view="health">Health</a></div>`;
  } else if (vals?.ok && H.slow) {
    html0 = `<div class="mbanner warn">Reads from the gateway are slow: the last one took ${esc(fmtMs(H.latency_ms))} (limit ${esc(fmtMs(H.max_read_ms))}).
      Values may be late. <a href="#" data-view="health">Health</a></div>`;
  } else if (vals?.ok && vals.bad.length) {
    html0 = `<div class="mbanner warn">${vals.bad.length} of ${Object.keys(vals.values).length} tags aren't readable right now, so their alarms show grey (no data), not OK.
      <a href="#" data-view="health">Which ones</a></div>`;
  }
  if (view.startsWith("sec:") && !sections.some((x) => `sec:${x.name}` === view)) view = "overview";
  if (view === "drives" && !axesTotal) view = "overview";
  const dotFor = (w) => (w === "ok" ? "okon" : w === "err" ? "erron" : w === "warn" ? "warnon" : "unk");
  $("#mNav").innerHTML = [["overview", "Overview", null], ["health", "Health", comms.cls], ...(axesTotal ? [["drives", `Drives (${axesTotal})`, faultedAxes.length ? "err" : known ? "ok" : ""]] : []),
    ...sections.map((sec) => [`sec:${sec.name}`, sectionName(sec.name), secInfo(sec).worst])]
    .map(([id, label, w]) => `<button class="${view === id ? "active" : ""}" data-view="${esc(id)}">${w !== null ? `<span class="dot ${dotFor(w)}"></span> ` : ""}${esc(label)}</button>`).join("");

  let html = html0;
  if (view === "overview") {
    const rows = [...faultedAxes.map((x) => `<div class="malarm on"><span class="dot erron"></span><span class="l" title="${tip(x)}">${esc(x.label)}</span>
        <span class="sev error">axis ${esc(AXIS_STATES[x.st.state] || "fault")}</span><a href="#" class="small" data-view="drives">Drives</a></div>`),
      ...active.map((x) => `<div class="malarm on"><span class="dot ${x.severity === "warning" ? "warnon" : "erron"}"></span>
        <span class="l" title="${tip(x)}">${esc(x.label)}</span>
        <a href="#" class="small muted nowrap mwhere" data-view="sec:${esc(x.area.section || "Controller")}" title="${esc(x.area.title)}">${esc(sectionName(x.area.section || "Controller"))} &rsaquo; ${esc(x.area.title)}</a>
        <span class="sev ${x.severity === "warning" ? "warning" : "error"}">${esc(SEV_LABEL[x.severity] || x.severity)}</span>
        <span class="small muted nowrap">${esc(fmtSince(x.node_id))}</span></div>`)];
    html += `<div class="panel"><h3>Active now</h3>${!known ? `<p class="muted small">Waiting for data.</p>`
      : rows.length ? rows.join("") : `<p class="small"><span class="dot okon"></span> Nothing active.</p>`}</div>`;
    html += `<div class="mgrid">${sections.map((sec) => {
      const i = secInfo(sec);
      const axes = sec.states.reduce((n, { s }) => n + s.axes.length, 0);
      const health = sec.states.filter(({ a }) => hasHealth(a)).length;
      return `<div class="panel marea ${i.worst} click" data-view="sec:${esc(sec.name)}">
        <div class="uahead"><h3 title="${esc(sec.name)}"><span class="dot ${dotFor(i.worst)}"></span> ${esc(sectionName(sec.name))}</h3>
          <span class="small muted nowrap">${i.act ? `${i.act} active` : known ? "OK" : ""}</span></div>
        <div class="small muted">${[i.alarms && `${i.alarms} alarms`, axes && `${axes} ${axes === 1 ? "axis" : "axes"}`,
          `${sec.states.length} areas`, health !== sec.states.length && `${sec.states.length - health} with signals only`].filter(Boolean).join(" &middot; ")}</div>
      </div>`;
    }).join("")}</div>`;
  } else if (view === "drives") {
    html = drivesTable(states, v, known);
  } else if (view === "health") {
    html = healthView(L, vals);
  } else {
    const sec = sections.find((x) => `sec:${x.name}` === view);
    const rank = ({ s }) => (s.active.some((x) => x.severity === "critical") ? 0
      : s.active.some((x) => x.severity === "fault") || s.faultedAxes.length ? 1 : s.active.length ? 2 : 3);
    const q = mach.filter.trim().toLowerCase();
    const match = ({ a }) => !q || `${a.title} ${a.id}`.toLowerCase().includes(q)
      || ITEM_LISTS.some((k) => (a[k] || []).some((x) => `${x.label} ${x.name}`.toLowerCase().includes(q)));
    const busy = ({ s }) => s.active.length || s.faultedAxes.length;
    let shown = sec.states.filter(match);
    if (mach.onlyActive) shown = shown.filter(busy);
    const cards = mach.edit ? shown : shown.filter(({ a }) => hasHealth(a)).sort((x, y) => rank(x) - rank(y));
    const signals = mach.edit || mach.onlyActive ? [] : shown.filter(({ a }) => !hasHealth(a));
    html += `<div class="mtools"><input type="search" id="mFilter" placeholder="Filter areas and tags" value="${esc(mach.filter)}">
      <label class="small"><input type="checkbox" id="mOnlyActive" ${mach.onlyActive ? "checked" : ""}> Only areas with something active</label>
      <span class="small muted">${shown.length} of ${sec.states.length} areas</span></div>`;
    // Group the cards by their top folder (Faults, CL1_Flags, R05_ExecQueue...), so a program with dozens of
    // areas reads as a handful of groups. Groups with something active open first; quiet ones start closed.
    const groups = [];
    for (const st of cards) {
      const key = areaGroup(st.a);
      let g = groups.find((x) => x.key === key);
      if (!g) groups.push(g = { key, states: [] });
      g.states.push(st);
    }
    // A folder with a single area isn't worth its own group: those go together under "Other".
    const other = { key: "Other", states: [] };
    for (const g of groups.filter((x) => x.states.length === 1 || x.key === "Other")) other.states.push(...g.states);
    groups.splice(0, groups.length, ...groups.filter((x) => x.states.length > 1 && x.key !== "Other"), ...(other.states.length ? [other] : []));
    const gRank = (g) => Math.min(...g.states.map(rank));
    groups.sort((x, y) => gRank(x) - gRank(y) || x.key.localeCompare(y.key));
    if (groups.length <= 1 || cards.length <= 8) {
      html += `<div class="mgrid">${cards.map(({ a, s, ai }) => areaCard(a, s, v, ai)).join("")}</div>`;
    } else {
      html += groups.map((g) => {
        const act = g.states.reduce((n, { s }) => n + s.active.length + s.faultedAxes.length, 0);
        const alarms = g.states.reduce((n, { s }) => n + s.alarms.length, 0);
        const w = gRank(g) <= 1 ? "err" : act ? "warn" : known ? "ok" : "";
        const open = act || q || mach.onlyActive || machGroupOpen(g.key);
        return `<details class="mgroup" data-group="${esc(g.key)}" ${open ? "open" : ""}><summary><span class="dot ${dotFor(w)}"></span>
          <b>${esc(g.key.replace(/_/g, " "))}</b> <span class="small muted">${g.states.length} area${g.states.length === 1 ? "" : "s"}${alarms ? ` &middot; ${alarms} alarms` : ""}${act ? ` &middot; <span class="errtext">${act} active</span>` : known ? " &middot; OK" : ""}</span></summary>
          <div class="mgrid">${g.states.map(({ a, s, ai }) => areaCard(a, s, v, ai)).join("")}</div></details>`;
      }).join("");
    }
    if (!cards.length) html += `<p class="muted small">${mach.onlyActive ? "Nothing active in this program." : "No areas match."}</p>`;
    if (signals.length) {
      html += `<div class="panel"><h3>Signals</h3><p class="small muted">Status bits, counters and values from areas with no alarms.</p>
        <div class="msignals">${signals.map(({ a }) => `<div class="msig"><div class="small muted msigh" title="${esc(a.id)}">${esc(a.title)}</div>
          <div class="mvals">${[...a.counters, ...a.values, ...a.status].map((x) => `<div class="mval"><span class="l" title="${tip(x)}">${esc(x.label)}</span>${a.status.includes(x)
            ? `<span class="dot ${truthy(v[x.node_id]) ? "on" : ""}"></span>`
            : `<span class="n">${esc(fmtNum(x.node_id ? v[x.node_id] : v[(x.members || {}).ACC]))}</span>`}</div>`).join("")}</div></div>`).join("")}</div></div>`;
    }
  }
  $("#mAreas").innerHTML = html;
}

function fmtMs(ms) { return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`; }
function fmtAge(s) { return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`; }
const fmtClock = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : "-");

// Comms tile: share of the dashboard's tags that read Good, and whether the heartbeat is moving.
function commsState(vals) {
  const H = vals?.health;
  if (!vals) return { n: "...", cls: "", title: "Reading" };
  if (!vals.ok || !H || !H.total) return { n: "down", cls: "err", title: vals.error || "No data from the gateway" };
  const pct = Math.floor((H.good / H.total) * 1000) / 10;
  if (H.heartbeat?.frozen) return { n: "frozen", cls: "err", title: "The heartbeat tag stopped changing" };
  const cls = H.good === H.total ? (H.slow ? "warn" : "ok") : pct >= 95 ? "warn" : "err";
  return { n: `${pct}%`, cls, sub: fmtMs(H.latency_ms),
    title: `${H.good} of ${H.total} tags read Good; last read ${fmtMs(H.latency_ms)}${H.slow ? " (slow)" : ""}` };
}

const EVENT_LABEL = { connected: "Connected", drop: "Dropped", reconnected: "Reconnected", error: "Error", coverage: "Tags" };

function healthView(L, vals) {
  const H = vals?.health || {};
  const hb = H.heartbeat || {};
  const m = L.machine || {};
  const now = Date.now() / 1000;
  const freshness = m.freshness || (m.heartbeat?.length ? "heartbeat" : "response");
  const hbState = freshness === "response" ? `<span class="muted">not used</span>` : !m.heartbeat?.length ? `<span class="muted">not set</span>`
    : !hb.good ? `<span class="errtext">not readable</span>`
      : hb.frozen ? `<span class="errtext">frozen ${esc(fmtAge(hb.age_s))}</span>`
        : `<span class="oktext">moving</span> <span class="small muted">changed ${esc(fmtAge(hb.age_s))} ago</span>`;
  const cell = (k, val, tip = "") => `<div title="${esc(tip)}"><div class="k">${k}</div><div class="v">${val}</div></div>`;
  const pct = H.total ? `${H.good} of ${H.total} (${Math.floor((H.good / H.total) * 1000) / 10}%)` : "-";
  let html = `<div class="panel"><h3>Comms with the gateway</h3>
    <p class="small muted">Measured on the reads behind this dashboard, while someone has it open. A tag that doesn't read Good shows grey (no data), never OK.</p>
    <div class="mhealth">
      ${cell("Connection", vals?.ok ? `<span class="oktext">reading</span> <span class="small muted">since ${esc(fmtClock(H.connected_at))}</span>` : `<span class="errtext">no data</span>`, vals?.error || "")}
      ${cell("Tags read Good", `<span class="${H.good === H.total ? "" : "errtext"}">${esc(pct)}</span>`)}
      ${cell("Read time", H.latency_ms != null ? `<span class="${H.slow ? "errtext" : ""}">${esc(fmtMs(H.latency_ms))}</span> <span class="small muted">avg ${esc(fmtMs(H.latency_avg_ms))}, max ${esc(fmtMs(H.latency_max_ms))}</span>` : "-", `Slow above ${m.max_read_ms || 2000} ms`)}
      ${cell("Last good read", H.last_ok ? `${esc(fmtClock(H.last_ok))} <span class="small muted">${esc(fmtAge(now - H.last_ok))} ago</span>` : "-")}
      ${cell("Drops / reconnects", `${H.drops || 0} / ${H.reconnects || 0}`, "Since this dashboard was first opened after Ghost Map started")}
      ${cell("Heartbeat", hbState, m.heartbeat?.[0] || "A tag the PLC changes all the time")}
      ${cell("Any tag changed", H.last_any_change ? `${esc(fmtAge(now - H.last_any_change))} ago` : "-", "Last time any value on this dashboard changed")}
    </div>
    <div class="mtools small">
      <label>Freshness check <select id="mFreshness" ${mach.edit ? "" : "disabled"}>
        <option value="heartbeat" ${freshness === "heartbeat" ? "selected" : ""}>Heartbeat tag</option>
        <option value="response" ${freshness === "response" ? "selected" : ""}>Response time only</option></select></label>
      <label>Slow above <input type="number" id="mMaxMs" min="100" max="60000" step="100" value="${esc(m.max_read_ms || 2000)}" ${mach.edit ? "" : "disabled"}> ms</label>
      ${mach.edit ? "" : `<span class="muted admin-only">Tick Edit to change these.</span>`}
    </div>
    ${freshness === "heartbeat" ? `<p class="small">Heartbeat tag: <span class="mono" title="${esc(m.heartbeat?.[0] || "")}">${esc(shortId(m.heartbeat?.[0])) || "none"}</span>
      ${mach.edit ? `<button class="btn ghost small" data-hbpick="1">Change</button>${m.heartbeat?.length ? ` <button class="btn ghost small" data-hbclear="1">Clear</button>` : ""}` : ""}</p>
      ${!m.heartbeat?.length ? `<p class="hint small">No heartbeat tag yet. Pick a tag the PLC changes all the time (a free-running counter is best),
        or switch to response time only.</p>` : ""}`
    : `<p class="small muted">Response time only: data counts as fresh while every read comes back from the gateway within the limit.
      This can't tell if the gateway itself is serving stale values from a PLC it has lost; tags it marks bad still show grey.</p>`}`;
  const bad = Object.entries(H.bad_status || {});
  if (bad.length) {
    html += `<h4>Not reading Good</h4><p class="small">${Object.entries(H.bad_by_plc || {}).map(([p, n]) => `<b>${esc(p)}</b>: ${n}`).join(" &middot; ")}</p>
      <div class="tablewrap"><table><tr><th>Tag</th><th>Status</th></tr>${bad.map(([nid, st]) => `<tr><td class="mono small" title="${esc(nid)}">${esc(shortId(nid))}</td><td class="small errtext">${esc(st)}</td></tr>`).join("")}</table></div>
      ${(vals?.bad || []).length > bad.length ? `<p class="small muted">Showing the first ${bad.length}.</p>` : ""}`;
  }
  html += `<h4>Connection history</h4>${mach.events.length ? `<div class="tablewrap"><table><tr><th>Time</th><th>Event</th><th>Detail</th></tr>
    ${mach.events.slice(0, 50).map((e) => `<tr><td class="small nowrap">${esc(new Date(e.at * 1000).toLocaleString())}</td>
      <td class="small ${e.kind === "drop" || e.kind === "error" ? "errtext" : ""}">${esc(EVENT_LABEL[e.kind] || e.kind)}</td>
      <td class="small">${esc(e.detail)}${e.repeats ? ` <span class="muted">(&times;${e.repeats}, last ${esc(fmtClock(e.last))})</span>` : ""}</td></tr>`).join("")}</table></div>`
    : `<p class="small muted">Nothing yet.</p>`}</div>`;

  const r = mach.verify;
  const sevCls = { error: "error", warning: "warning", info: "info" };
  html += `<div class="panel"><div class="uahead"><h3>Alarm check</h3>
      <button class="btn small" data-verify="1" ${mach.verifying ? "disabled" : ""}>${mach.verifying ? "Checking..." : r ? "Check again" : "Check all alarms"}</button></div>
    <p class="small muted">Reads every alarm, running and heartbeat tag once and flags the ones that can't be trusted to show a fault:
      tags the gateway doesn't know, tags it can't read, alarms that aren't BOOLs, and areas where most bits are on (probably "on = OK").</p>
    ${!r ? "" : r.error ? `<p class="errtext">${esc(r.error)}</p>` : `<p>${r.summary.readable} of ${r.summary.alarms} alarms readable &middot; ${r.summary.active} active now
      &middot; ${r.summary.changed} changed while watched${r.summary.watching_s ? ` (${esc(fmtAge(r.summary.watching_s))})` : ""}
      &middot; <b class="${r.summary.errors ? "errtext" : "oktext"}">${r.summary.errors} problems</b>, ${r.summary.warnings} warnings
      <span class="small muted">at ${esc(r.at)}</span></p>
      ${r.findings.length ? `<div class="tablewrap"><table class="mfind">${r.findings.map((f) => `<tr><td><span class="sev ${sevCls[f.severity]}">${esc(f.severity)}</span></td>
        <td><b title="${esc(f.node_id || "")}">${esc(f.target)}</b><div class="small">${esc(f.message)}</div><div class="hint small">${esc(f.hint)}</div></td>
        <td class="small muted mono">${esc(f.code)}</td></tr>`).join("")}</table></div>` : `<p class="small"><span class="dot okon"></span> Every alarm read Good and looks right.</p>`}`}
  </div>`;
  html += `<div class="panel admin-only"><h3>Debug</h3><p class="small muted">Ghost Map's own log (errors, drops, reconnects) and a bundle to send when reporting a problem.</p>
    <button class="btn ghost small" data-debuglog="1">Show the log</button> <a class="btn ghost small" href="api/debug/bundle" download>Download debug bundle</a></div>`;
  return html;
}

const AXIS_SORT = (x) => (x.st.faulted ? 0 : x.st.state === null ? 3 : x.st.state === 4 ? 2 : 1);

function drivesTable(states, v, known) {
  const axes = states.flatMap(({ a, s, ai }) => s.axes.map((x, i) => ({ x, a, ai, i })));
  const n = (x, k) => (x.members[k] && known ? esc(fmtNum(v[x.members[k]])) : "-");
  const flag = (x, k) => (x.members[k] ? `<span class="dot ${truthy(v[x.members[k]]) ? "on" : ""}"></span>` : "");
  const count = (pred) => axes.filter(({ x }) => pred(x.st)).length;
  const summary = known ? [[count((st) => st.faulted), "faulted", "errchip"], [count((st) => !st.faulted && st.state === 4), "running", "okchip"],
    [count((st) => !st.faulted && st.state !== 4 && st.state !== null), "not running", ""], [count((st) => st.state === null), "no state (virtual or not read)", ""]]
    .filter(([c]) => c).map(([c, l, cls]) => `<span class="chip ${cls}">${c} ${l}</span>`).join(" ") : "";
  // One block per program (or controller scope), faulted axes first.
  const bySec = {};
  for (const r of axes) (bySec[r.a.section || "Controller"] ||= []).push(r);
  const secs = Object.keys(bySec).sort();
  const rows = (list) => list.sort((p, q) => AXIS_SORT(p.x) - AXIS_SORT(q.x) || p.x.label.localeCompare(q.x.label)).map(({ x, ai, i }) => {
    const st = x.st;
    const key = `${mach.dash.id}|${x.name}`;
    const detail = mach.axisDetail[key];
    const open = mach.axisOpen.has(x.name);
    const since = x.members.CIPAxisState && mach.vals?.since?.[x.members.CIPAxisState];
    return `<tr class="${st.faulted ? "bad" : ""}"><td class="l" title="${tip(x)}"><b>${esc(x.label)}</b></td>
      <td class="nowrap"><span class="chip ${st.faulted ? "errchip" : st.state === 4 ? "okchip" : ""}">${st.state === null ? "-" : esc(AXIS_STATES[st.state] || `state ${st.state}`)}</span>
        ${since ? `<span class="small muted" title="State last changed">${esc(new Date(since * 1000).toLocaleTimeString())}</span>` : ""}</td>
      <td>${flag(x, "DriveEnableStatus")}</td><td>${flag(x, "ServoActionStatus")}</td><td>${flag(x, "AxisHomedStatus")}</td>
      <td class="mono">${n(x, "ActualPosition")}</td><td class="mono">${n(x, "ActualVelocity")}</td><td class="mono">${n(x, "MotorCapacity")}</td>
      <td class="mono">${n(x, "CurrentFeedback")}</td><td class="mono">${n(x, "DCBusVoltage")}</td>
      <td class="small ${st.faultWords.length ? "errtext" : "muted"}">${st.faultWords.length ? st.faultWords.map((k) => `${esc(k)}=${esc(fmtNum(v[x.members[k]]))}`).join(" ") : known ? "none" : "-"}</td>
      <td class="nowrap"><button class="btn ghost small" data-axis="${esc(x.name)}" aria-expanded="${open}">${open ? "Hide" : "Details"}</button>${editBtn(ai, "axes", i)}</td></tr>
      ${open ? `<tr class="maxisdetail"><td colspan="12">${axisDetailHtml(detail)}</td></tr>` : ""}`;
  }).join("");
  const head = `<thead><tr><th>Axis</th><th>State</th><th title="Drive enabled">En</th><th title="Servo action">Servo</th>
    <th>Homed</th><th>Position</th><th>Velocity</th><th>Motor %</th><th>Current</th><th>DC bus V</th><th>Fault words</th><th></th></tr></thead>`;
  return `<div class="panel"><div class="uahead"><h3>Drives</h3><span>${summary}</span></div>
    <p class="small muted">Live, read with the dashboard. <b>Details</b> reads the axis's fault, alarm and inhibit bits plus motion, power, limit and tuning values, and keeps them updated while it's open.</p>
    ${secs.map((sec) => `${secs.length > 1 ? `<h4 class="msubhead">${esc(sectionName(sec))}</h4>` : ""}
      <div class="tablewrap"><table class="mdrives">${head}<tbody>${rows(bySec[sec])}</tbody></table></div>`).join("")}</div>`;
}

function axisDetailHtml(d) {
  if (!d) return `<span class="muted small">Reading...</span>`;
  if (d.error) return `<span class="errtext small">${esc(d.error)}</span>`;
  const faults = d.active.length ? d.active.map((f) => `<span class="sev ${f.kind === "fault" ? "error" : "warning"}" title="${esc(f.name)}">${esc(f.label)}</span>`).join(" ")
    : `<span class="small"><span class="dot okon"></span> None of ${d.checked} fault, alarm and inhibit bits are on.</span>`;
  const groups = Object.entries(d.groups || {}).map(([g, items]) => `<div class="maxgroup"><div class="k">${esc(g)}</div>
    ${items.map((it) => `<div class="mval"><span class="l" title="${esc(it.name)}">${esc(it.label)}</span><span class="n mono ${g === "Fault words" && Number(it.value) ? "errtext" : ""}">${esc(fmtNum(it.value))}</span></div>`).join("")}</div>`).join("");
  return `<div class="maxdetail"><div>${faults}</div>
    ${d.status_on?.length ? `<div class="small"><span class="muted">On:</span> ${d.status_on.map((x) => `<span class="flag on">${esc(x)}</span>`).join(" ")}</div>` : ""}
    <div class="maxgroups">${groups}</div>
    <div class="small muted">Read ${esc(new Date((d.at || Date.now() / 1000) * 1000).toLocaleTimeString())}</div></div>`;
}

async function saveFreshness() {
  const L = layoutCopy();
  L.machine = { ...(L.machine || {}), freshness: $("#mFreshness").value, max_read_ms: Number($("#mMaxMs").value) || 2000 };
  try { await machineSaveLayout(L); } catch (err) { alert(err.message); }
}

async function axisLoad(name) {
  const key = `${mach.dash.id}|${name}`;
  try {
    mach.axisDetail[key] = await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/axis?name=${encodeURIComponent(name)}`, { timeout: 15000 });
  } catch (err) {
    mach.axisDetail[key] = { error: err.message };
  }
}

function editBtn(ai, kind, i) {
  return mach.edit ? `<button class="btn ghost small" data-edit="${ai}|${kind}|${i}">Edit</button>` : "";
}

function areaCard(a, s, v, ai) {
  const bad = new Set(mach.vals?.bad || []);
  const known = mach.vals?.ok && s.known;
  const worst = s.active.some((x) => x.severity !== "warning") || s.faultedAxes.length ? "err" : s.active.length ? "warn" : known ? "ok" : "";
  const idx = (kind, x) => a[kind].indexOf(a[kind].find((y) => y.name === x.name && y.node_id === x.node_id));
  const alarmRow = (x) => `<div class="malarm ${x.active ? "on" : ""}">
      <span class="dot ${x.active ? (x.severity === "warning" ? "warnon" : "erron") : x.known ? "" : "unk"}"></span>
      <span class="l" title="${tip(x)}">${esc(x.label)}</span>
      ${x.active ? `<span class="sev ${x.severity === "warning" ? "warning" : "error"}">${esc(SEV_LABEL[x.severity] || x.severity)}</span><span class="small muted nowrap" title="${mach.vals?.since?.[x.node_id] ? "" : "Already on when Ghost Map started watching this dashboard"}">${esc(fmtSince(x.node_id))}</span>` : ""}
      ${editBtn(ai, "alarms", idx("alarms", x))}</div>`;
  const axes = s.axes.map((x, i) => {
    const m = x.members || {};
    const st = x.st;
    const name = st.state === null ? "-" : AXIS_STATES[st.state] || `state ${st.state}`;
    const dot = !st.known ? "unk" : st.faulted ? "erron" : st.state === 4 ? "okon" : "";
    const detail = mach.axisDetail[`${mach.dash.id}|${x.name}`];
    const flags = [["DriveEnableStatus", "enabled"], ["ServoActionStatus", "servo on"], ["AxisHomedStatus", "homed"]]
      .filter(([k]) => m[k]).map(([k, l]) => `<span class="flag ${truthy(v[m[k]]) ? "on" : ""}">${l}</span>`).join("");
    const nums = [["ActualPosition", "pos"], ["ActualVelocity", "vel"], ["MotorCapacity", "motor %"], ["DCBusVoltage", "DC bus V"]]
      .filter(([k]) => m[k]).map(([k, l]) => `<span class="small muted">${l}</span> <span class="mono small">${st.known ? esc(fmtNum(v[m[k]])) : "-"}</span>`).join(" &middot; ");
    return `<div class="maxis ${st.faulted ? "on" : ""}">
      <div class="malarm"><span class="dot ${dot}"></span><span class="l" title="${tip(x)}"><b>${esc(x.label)}</b></span>
        <span class="chip ${st.faulted ? "errchip" : st.state === 4 ? "okchip" : ""}">${esc(name)}</span>${editBtn(ai, "axes", i)}</div>
      <div class="maxisrow">${flags} ${nums}</div>
      ${st.faultWords.length ? `<div class="maxisrow errtext small">${st.faultWords.map((k) => `${esc(k)} = ${esc(fmtNum(v[m[k]]))}`).join(" &middot; ")}</div>` : ""}
      <div class="maxisrow"><button class="btn ghost small" data-axis="${esc(x.name)}">${mach.axisOpen.has(x.name) ? "Hide" : "Details"}</button></div>
      ${mach.axisOpen.has(x.name) ? axisDetailHtml(detail) : ""}
    </div>`;
  }).join("");
  const timers = a.timers.map((t, i) => {
    const acc = Number(v[t.members.ACC]), pre = Number(v[t.members.PRE]);
    const pct = pre > 0 ? Math.min(100, Math.round((acc / pre) * 100)) : 0;
    const tk = known && t.members.ACC && !bad.has(t.members.ACC);
    return `<div class="mtimer"><span class="l" title="${tip(t)}">${esc(t.label)}</span>
      <span class="bar"><span style="width:${tk ? pct : 0}%"></span></span>
      <span class="small mono nowrap">${tk ? `${esc(fmtNum(acc))}/${esc(fmtNum(pre))}${truthy(v[t.members.DN]) ? " DN" : ""}` : "-"}</span>${editBtn(ai, "timers", i)}</div>`;
  }).join("");
  const words = a.words.map((w, i) => {
    const n = Number(v[w.node_id]);
    return `<div class="malarm ${n ? "on" : ""}"><span class="dot ${n ? "erron" : ""}"></span><span class="l" title="${tip(w)}">${esc(w.label)}</span>
      <span class="small mono">${known ? esc(fmtNum(v[w.node_id])) : "-"}</span>${editBtn(ai, "words", i)}</div>`;
  }).join("");
  const valRow = (kind) => (x, i) => `<div class="mval"><span class="l" title="${tip(x)}">${esc(x.label)}</span>${kind === "status"
    ? `<span class="dot ${truthy(v[x.node_id]) ? "on" : ""}"></span>`
    : `<span class="n">${esc(fmtNum(x.node_id ? v[x.node_id] : v[(x.members || {}).ACC]))}</span>`}${editBtn(ai, kind, i)}</div>`;
  const vals = [...a.counters.map(valRow("counters")), ...a.values.map(valRow("values")), ...a.status.map(valRow("status"))].join("");
  const quiet = s.alarms.filter((x) => !x.active);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const counts = [s.alarms.length && plural(s.alarms.length, "alarm"), s.axes.length && plural(s.axes.length, "axis").replace("axiss", "axes")].filter(Boolean).join(", ");
  return `<div class="panel marea ${worst}">
    <div class="uahead"><h3 title="${esc(a.title)}"><span class="dot ${worst === "ok" ? "okon" : worst === "err" ? "erron" : worst === "warn" ? "warnon" : "unk"}"></span> ${esc(a.title)}</h3>
      <span class="small muted nowrap">${s.active.length ? `${s.active.length} active` : s.faultedAxes.length ? "axis fault" : known ? "OK" : counts ? "no data" : ""}${counts ? ` &middot; ${counts}` : ""}${s.inv ? " &middot; on = healthy" : ""}</span></div>
    ${(a.hints || []).filter(() => !s.inv).map((h) => `<p class="hint small">${esc(h)}</p>`).join("")}
    ${mach.edit ? `<div class="medit small"><label><input type="checkbox" data-invert="${esc(a.id)}" ${s.inv ? "checked" : ""}> On means healthy (flip)</label>
      <button class="btn ghost small" data-addtag="${ai}">Add tag</button> <button class="btn ghost small" data-renamearea="${ai}">Rename</button>
      <button class="btn ghost small" data-rmarea="${ai}">Remove area</button></div>` : ""}
    ${s.active.map(alarmRow).join("")}
    ${axes}${words}${timers}
    ${vals ? `<div class="mvals">${vals}</div>` : ""}
    ${quiet.length ? `<details ${mach.edit ? "open" : ""}><summary class="small muted">${quiet.length} ${known ? "not active" : "alarms"}</summary>${quiet.map(alarmRow).join("")}</details>` : ""}
  </div>`;
}

// ------------------------------------------------------------------ axis fault detail (read on request)
$("#mAreas").addEventListener("click", async (e) => {
  const name = e.target.dataset.axis;
  if (!name) return;
  if (mach.axisOpen.has(name)) {
    mach.axisOpen.delete(name);
    machineRender();
    return;
  }
  mach.axisOpen.add(name);
  machineRender();
  await axisLoad(name);
  machineRender();
});

// ------------------------------------------------------------------ edits (admin)
async function machineSaveOverrides(change) {
  const ov = { invert: { ...(mach.dash.overrides?.invert || {}) }, hidden: [...(mach.dash.overrides?.hidden || [])] };
  change(ov);
  mach.dash = await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/overrides`, { method: "POST", body: JSON.stringify(ov) });
  machineRender();
}
async function machineSaveLayout(layout) {
  mach.dash = await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/layout`, { method: "POST", body: JSON.stringify({ layout }) });
  machineRender();
}
const layoutCopy = () => JSON.parse(JSON.stringify(mach.dash.layout));

$("#mAreas").addEventListener("input", (e) => {
  if (e.target.id !== "mFilter") return;
  mach.filter = e.target.value;
  clearTimeout(mach.filterTimer);
  mach.filterTimer = setTimeout(() => {
    const pos = e.target.selectionStart;
    machineRender();
    const f = $("#mFilter");
    if (f) { f.focus(); f.setSelectionRange(pos, pos); }
  }, 250);
});
$("#mAreas").addEventListener("toggle", (e) => {
  const key = e.target.dataset?.group;
  if (key === undefined) return;
  try { localStorage.setItem(`gm.dash.group.${mach.dash.id}.${key}`, e.target.open ? "1" : "0"); } catch (_) { /* ignore */ }
}, true);
$("#mAreas").addEventListener("change", (e) => {
  if (e.target.id === "mOnlyActive") { mach.onlyActive = e.target.checked; machineRender(); return; }
  if (e.target.id === "mFreshness" || e.target.id === "mMaxMs") { saveFreshness(); return; }
  const id = e.target.dataset.invert;
  if (id !== undefined) machineSaveOverrides((ov) => { ov.invert[id] = e.target.checked; });
});
document.querySelector("#tab-machine").addEventListener("click", async (e) => {
  const el = e.target.closest("[data-view],[data-editrun]");
  if (el?.dataset.view && !e.target.dataset.axis && !e.target.closest("button[data-edit]")) {
    e.preventDefault();
    machineSetView(el.dataset.view);
    return;
  }
  const t = e.target.dataset;
  try {
    if (t.verify) {
      mach.verifying = true;
      machineRender();
      try {
        mach.verify = { ...(await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/verify`)), at: new Date().toLocaleTimeString() };
      } catch (err) { mach.verify = { error: err.message }; }
      mach.verifying = false;
      machineRender();
    } else if (t.rebuild) {
      if (!confirm("Lay this dashboard out again with the current rules? Area edits (renames, added or removed items) are replaced; "
        + "the running tags, heartbeat, comms settings and flipped areas are kept. The PLC is not touched.")) return;
      mach.dash = await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/rebuild`, { method: "POST" });
      machineRender();
    } else if (t.debuglog) {
      $("#debugBtn").click();
    } else if (t.hbpick || t.hbclear) {
      const L = layoutCopy();
      L.machine = { ...(L.machine || {}), heartbeat: [] };
      if (t.hbpick) {
        const tag = await pickTag("Pick a tag the PLC changes all the time (a counter or a running timer)", "heartbeat");
        if (!tag) return;
        L.machine.heartbeat = [tag.node_id];
      }
      await machineSaveLayout(L);
    } else if (t.editrun) {
      e.stopPropagation();
      runningDialog();
    } else if (t.edit) {
      const [ai, kind, i] = t.edit.split("|");
      itemDialog(Number(ai), kind, Number(i));
    } else if (t.addtag) {
      const tag = await pickTag("Add a tag to this area");
      if (!tag) return;
      const L = layoutCopy();
      const name = tag.path.split("/").pop();
      const item = { name, label: name.replace(/_/g, " "), node_id: tag.node_id };
      const kind = tag.type === "Boolean" ? "alarms" : "values";
      if (kind === "alarms") Object.assign(item, { severity: "fault", category: "other" });
      L.areas[Number(t.addtag)][kind].push(item);
      await machineSaveLayout(L);
    } else if (t.renamearea) {
      const L = layoutCopy();
      const a = L.areas[Number(t.renamearea)];
      const title = prompt("Area name", a.title);
      if (!title) return;
      a.title = title.slice(0, 200);
      await machineSaveLayout(L);
    } else if (t.rmarea) {
      const L = layoutCopy();
      if (!confirm(`Remove the area "${L.areas[Number(t.rmarea)].title}" from this dashboard? The PLC is not touched.`)) return;
      L.areas.splice(Number(t.rmarea), 1);
      await machineSaveLayout(L);
    } else if (t.addarea) {
      const title = prompt("Name of the new area");
      if (!title) return;
      const L = layoutCopy();
      const area = { id: `custom-${Date.now()}`, title: title.slice(0, 200), hints: [] };
      KINDS.forEach((k) => { area[k] = []; });
      L.areas.unshift(area);
      await machineSaveLayout(L);
    }
  } catch (err) { alert(err.message); }
});

$("#mDelete").addEventListener("click", async () => {
  if (!mach.dash || !confirm(`Delete the dashboard "${mach.dash.name}"? The PLC is not touched.`)) return;
  await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}`, { method: "DELETE" });
  try { localStorage.removeItem("gm.dash"); } catch (_) { /* ignore */ }
  await machineLoadList();
});

// Which tags say the machine is running (any or all of them on).
function runningDialog() {
  const L = layoutCopy();
  L.machine = { running: [...((L.machine || {}).running || [])], mode: (L.machine || {}).mode || "any" };
  const dlg = $("#mRunDialog");
  const draw = () => {
    $("#mRunList").innerHTML = L.machine.running.length ? L.machine.running.map((n, i) => `<div class="mtagfield">
      <span class="mono small l" title="${esc(n)}">${esc(shortId(n))}</span><button type="button" class="btn ghost small" data-rmrun="${i}">Remove</button></div>`).join("")
      : `<p class="small muted">No running tag. The Machine tile shows "-".</p>`;
  };
  draw();
  $("#mRunMode").value = L.machine.mode;
  $("#mRunErr").textContent = "";
  $("#mRunList").onclick = (e) => {
    const i = e.target.dataset.rmrun;
    if (i !== undefined) { L.machine.running.splice(Number(i), 1); draw(); }
  };
  $("#mRunAdd").onclick = async () => {
    const tag = await pickTag("Pick a tag that is on while the machine runs", "running");
    if (tag && !L.machine.running.includes(tag.node_id)) { L.machine.running.push(tag.node_id); draw(); }
  };
  $("#mRunCancel").onclick = () => dlg.close();
  $("#mRunSave").onclick = async () => {
    L.machine.mode = $("#mRunMode").value;
    try { await machineSaveLayout(L); dlg.close(); } catch (err) { $("#mRunErr").textContent = err.message; }
  };
  dlg.showModal();
}

// Item editor: label, tag(s) picked from the dashboard's own tag export, alarm severity/category, remove.
function itemDialog(ai, kind, i) {
  const L = layoutCopy();
  const item = L.areas[ai][kind][i];
  const dlg = $("#mItemDialog");
  const f = $("#mItemForm");
  $("#mItemTitle").textContent = `${KIND_LABEL[kind]} in ${L.areas[ai].title}`;
  f.label.value = item.label || "";
  $("#mItemAlarm").classList.toggle("hidden", kind !== "alarms");
  f.severity.value = item.severity || "fault";
  f.category.innerHTML = CATEGORIES.map((c) => `<option>${c}</option>`).join("");
  f.category.value = item.category || "other";
  const fields = item.members ? Object.keys(item.members) : ["node_id"];
  const current = (k) => (item.members ? item.members[k] : item.node_id) || "";
  const renderTags = () => {
    $("#mItemTags").innerHTML = fields.map((k) => `<div class="mtagfield"><span class="small muted">${item.members ? esc(k) : "Tag"}</span>
      <span class="mono small l" title="${esc(current(k))}">${esc(shortId(current(k))) || "-"}</span>
      <button type="button" class="btn ghost small" data-pick="${esc(k)}">Change</button></div>`).join("");
  };
  renderTags();
  $("#mItemTags").onclick = async (e) => {
    const k = e.target.dataset.pick;
    if (!k) return;
    const tag = await pickTag(`Pick the tag for ${item.label || item.name}${item.members ? ` (${k})` : ""}`, item.name);
    if (!tag) return;
    if (item.members) item.members[k] = tag.node_id; else item.node_id = tag.node_id;
    renderTags();
  };
  $("#mItemErr").textContent = "";
  $("#mItemRemove").onclick = async () => {
    L.areas[ai][kind].splice(i, 1);
    try { await machineSaveLayout(L); dlg.close(); } catch (err) { $("#mItemErr").textContent = err.message; }
  };
  $("#mItemCancel").onclick = () => dlg.close();
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    item.label = f.label.value.trim() || item.name;
    if (kind === "alarms") { item.severity = f.severity.value; item.category = f.category.value; }
    try { await machineSaveLayout(L); dlg.close(); } catch (err) { $("#mItemErr").textContent = err.message; }
  };
  dlg.showModal();
}

// Tag picker over the tags discovered when the dashboard was built. Resolves to a tag or null.
function pickTag(title, query = "") {
  const dlg = $("#mPickDialog");
  $("#mPickTitle").textContent = title;
  const input = $("#mPickQ");
  input.value = query;
  let timer = null;
  const search = async () => {
    $("#mPickList").innerHTML = `<tr><td class="muted small">Searching...</td></tr>`;
    try {
      const rows = await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/tags?q=${encodeURIComponent(input.value)}&limit=200`);
      $("#mPickList").innerHTML = rows.length ? rows.map((r, n) => `<tr data-n="${n}"><td class="l" title="${esc(r.node_id)}">${esc(r.path)}</td>
        <td class="small muted">${esc(r.type)}</td><td class="small mono">${esc(fmtNum(r.value))}</td></tr>`).join("")
        : `<tr><td class="muted small">No tags match. This only searches what was exported when the dashboard was built.</td></tr>`;
      $("#mPickList")._tags = rows;
    } catch (e) { $("#mPickList").innerHTML = `<tr><td class="errtext">${esc(e.message)}</td></tr>`; }
  };
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(search, 200); };
  search();
  return new Promise((resolve) => {
    $("#mPickList").onclick = (e) => {
      const tr = e.target.closest("tr[data-n]");
      if (!tr) return;
      dlg.close();
      resolve($("#mPickList")._tags[Number(tr.dataset.n)]);
    };
    $("#mPickCancel").onclick = () => { dlg.close(); resolve(null); };
    dlg.oncancel = () => resolve(null);
    dlg.showModal();
    input.focus();
  });
}

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
