"use strict";
// Machine dashboards: layouts built on the server from a tag export, filled with live values that the
// server reads (read-only) from the OPC UA gateway. Uses $, esc and api() from app.js.

const mach = { list: [], dash: null, vals: null, timer: null, edit: false, axisDetail: {} };
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
    return { ...a, known, active: known && (inv ? !on : on) };
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

  $("#mLive").innerHTML = !vals ? `reading <span class="mono">${esc(d.endpoint)}</span>...`
    : vals.ok ? `<span class="chip okchip">live</span> <span class="mono">${esc(d.endpoint)}</span> &middot; ${esc(new Date(vals.at * 1000).toLocaleTimeString())}
      &middot; ${Object.keys(vals.values).length} tags${
      vals.bad.length ? ` &middot; <span class="errtext">${vals.bad.length} not readable (renamed, or not on this server?)</span>` : ""}`
      : `<span class="chip errchip">no data</span> <span class="errtext">${esc(vals.error)}</span>`;

  $("#mNotes").innerHTML = (L.notes || []).map((n) => `<details class="small muted"><summary>What was left out</summary>${esc(n)}</details>`).join("")
    + (mach.edit ? `<p class="small muted">Edit mode: click <b>Edit</b> on any item to rename it, change its tag or remove it.
      Tags are picked from what Ghost Map found when the dashboard was built. <button class="btn ghost small" data-addarea="1">Add area</button></p>` : "");

  const states = L.areas.map((a, ai) => ({ a, ai, s: areaState(a) }));
  const active = states.flatMap(({ a, s }) => s.active.map((x) => ({ ...x, area: a })));
  const faultedAxes = states.flatMap(({ a, s }) => s.faultedAxes.map((x) => ({ ...x, area: a })));
  const count = (pred) => active.filter(pred).length;
  const known = vals?.ok && vals.bad.length < Object.keys(vals.values).length;
  const run = runningState(L, v, known);
  const tiles = [];
  tiles.push(run
    ? { n: !run.known ? "?" : run.running ? "Running" : "Stopped", l: "Machine", cls: run.running ? "ok" : "warn",
      title: `${run.ids.length} running tag${run.ids.length === 1 ? "" : "s"} (${(L.machine || {}).mode === "all" ? "all" : "any"} on = running)` }
    : { n: "-", l: "Machine", title: "No running tag yet: tick Edit and set one" });
  const faults = count((x) => x.severity !== "warning") + faultedAxes.length;
  tiles.push(
    { n: known ? faults : "?", l: "Active faults", cls: faults ? "err" : "ok" },
    { n: known ? count((x) => x.severity === "warning") : "?", l: "Warnings", cls: count((x) => x.severity === "warning") ? "warn" : "ok" },
    { n: known ? count((x) => x.category === "estop") : "?", l: "E-stops", cls: count((x) => x.category === "estop") ? "err" : "ok" },
    { n: known ? count((x) => x.category === "comms") : "?", l: "Comms faults", cls: count((x) => x.category === "comms") ? "err" : "ok" },
  );
  const axesTotal = states.reduce((n, { s }) => n + s.axes.length, 0);
  if (axesTotal) tiles.push({ n: known ? `${axesTotal - faultedAxes.length}/${axesTotal}` : "?", l: "Axes OK", cls: faultedAxes.length ? "err" : "ok", view: "drives" });
  $("#mTiles").innerHTML = tiles.map((t, i) => `<div class="tile ${known ? t.cls || "" : ""} ${t.view ? "click" : ""}" ${t.view ? `data-view="${t.view}"` : ""}>
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
  if (view.startsWith("sec:") && !sections.some((x) => `sec:${x.name}` === view)) view = "overview";
  if (view === "drives" && !axesTotal) view = "overview";
  const dotFor = (w) => (w === "ok" ? "okon" : w === "err" ? "erron" : w === "warn" ? "warnon" : "unk");
  $("#mNav").innerHTML = [["overview", "Overview", null], ...(axesTotal ? [["drives", `Drives (${axesTotal})`, faultedAxes.length ? "err" : known ? "ok" : ""]] : []),
    ...sections.map((sec) => [`sec:${sec.name}`, sectionName(sec.name), secInfo(sec).worst])]
    .map(([id, label, w]) => `<button class="${view === id ? "active" : ""}" data-view="${esc(id)}">${w !== null ? `<span class="dot ${dotFor(w)}"></span> ` : ""}${esc(label)}</button>`).join("");

  let html = "";
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
  } else {
    const sec = sections.find((x) => `sec:${x.name}` === view);
    const rank = ({ s }) => (s.active.some((x) => x.severity === "critical") ? 0
      : s.active.some((x) => x.severity === "fault") || s.faultedAxes.length ? 1 : s.active.length ? 2 : 3);
    const cards = mach.edit ? sec.states : sec.states.filter(({ a }) => hasHealth(a)).sort((x, y) => rank(x) - rank(y));
    const signals = mach.edit ? [] : sec.states.filter(({ a }) => !hasHealth(a));
    html = `<div class="mgrid">${cards.map(({ a, s, ai }) => areaCard(a, s, v, ai)).join("")}</div>`;
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

function drivesTable(states, v, known) {
  const axes = states.flatMap(({ a, s, ai }) => s.axes.map((x, i) => ({ x, a, ai, i })));
  const n = (x, k) => (x.members[k] && known ? esc(fmtNum(v[x.members[k]])) : "-");
  const flag = (x, k) => (x.members[k] ? `<span class="dot ${truthy(v[x.members[k]]) ? "on" : ""}"></span>` : "");
  return `<div class="panel"><div class="tablewrap"><table class="mdrives"><thead><tr><th>Axis</th><th>State</th><th title="Drive enabled">En</th><th title="Servo action">Servo</th>
    <th>Homed</th><th>Position</th><th>Velocity</th><th>Motor %</th><th>Current</th><th>DC bus V</th><th>Fault words</th><th></th></tr></thead><tbody>
    ${axes.map(({ x, ai, i }) => {
      const st = x.st;
      const detail = mach.axisDetail[`${mach.dash.id}|${x.name}`];
      return `<tr class="${st.faulted ? "bad" : ""}"><td class="l" title="${tip(x)}"><b>${esc(x.label)}</b></td>
        <td><span class="chip ${st.faulted ? "errchip" : st.state === 4 ? "okchip" : ""}">${st.state === null ? "-" : esc(AXIS_STATES[st.state] || `state ${st.state}`)}</span></td>
        <td>${flag(x, "DriveEnableStatus")}</td><td>${flag(x, "ServoActionStatus")}</td><td>${flag(x, "AxisHomedStatus")}</td>
        <td class="mono">${n(x, "ActualPosition")}</td><td class="mono">${n(x, "ActualVelocity")}</td><td class="mono">${n(x, "MotorCapacity")}</td>
        <td class="mono">${n(x, "CurrentFeedback")}</td><td class="mono">${n(x, "DCBusVoltage")}</td>
        <td class="small ${st.faultWords.length ? "errtext" : "muted"}">${st.faultWords.length ? st.faultWords.map((k) => `${esc(k)}=${esc(fmtNum(v[x.members[k]]))}`).join(" ") : known ? "none" : "-"}</td>
        <td class="nowrap"><button class="btn ghost small" data-axis="${esc(x.name)}">${detail ? "Check again" : "Which faults?"}</button>${editBtn(ai, "axes", i)}</td></tr>
        ${detail ? `<tr><td colspan="12" class="small">${detail.error ? `<span class="errtext">${esc(detail.error)}</span>`
          : detail.active.length ? detail.active.map((f) => `<span class="sev ${f.kind === "fault" ? "error" : "warning"}" title="${esc(f.name)}">${esc(f.label)}</span>`).join(" ")
            : `<span class="muted">none of ${detail.checked} fault, alarm and inhibit bits are on</span>`}</td></tr>` : ""}`;
    }).join("")}</tbody></table></div></div>`;
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
      <div class="maxisrow"><button class="btn ghost small" data-axis="${esc(x.name)}">${detail ? "Check again" : "Which faults?"}</button>
        ${detail ? (detail.error ? `<span class="errtext small">${esc(detail.error)}</span>`
          : detail.active.length ? detail.active.map((f) => `<span class="sev ${f.kind === "fault" ? "error" : "warning"}" title="${esc(f.name)}">${esc(f.label)}</span>`).join(" ")
            : `<span class="small muted">none of ${detail.checked} fault, alarm and inhibit bits are on</span>`) : ""}</div>
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
  e.target.disabled = true;
  try {
    mach.axisDetail[`${mach.dash.id}|${name}`] = await api(`api/dashboards/${encodeURIComponent(mach.dash.id)}/axis?name=${encodeURIComponent(name)}`);
  } catch (err) {
    mach.axisDetail[`${mach.dash.id}|${name}`] = { error: err.message };
  }
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

$("#mAreas").addEventListener("change", (e) => {
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
    if (t.editrun) {
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
