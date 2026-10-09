"use strict";
// Production pages: the TSC part schedule (read-only SQL) for one line, and its connection settings.
// Uses $, esc and api from app.js.

const PROD_POLL_MS = 15000;  // the server reuses a read for its cache time (30 s by default) anyway
const prod = { lines: null, line: null, data: null, timer: null, error: null };

const pNum = (n, d = 0) => (n == null ? "-" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const fmtHm = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "-");
const fmtMins = (s) => (s == null ? "-" : s < 60 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
function fmtLen(v, unit) {
  if (v == null || v === "") return "-";
  if (unit !== "in") return `${pNum(v, 1)} ${unit}`;
  const ft = Math.floor(v / 12), inch = Math.round((v - ft * 12) * 10) / 10;
  return `${ft}' ${inch}"`;
}

// ------------------------------------------------------------------ line page
async function prodLoadLines() {
  const r = await api("api/tsc/lines", { timeout: 40000 });
  if (!r.ok) {
    prod.lines = null;
    prod.error = r;
    $("#pLine").innerHTML = "";
    return;
  }
  prod.error = null;
  prod.lines = r.lines;
  $("#pLine").innerHTML = r.lines.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
  let id = prod.line;
  try { id = id ?? Number(localStorage.getItem("gm.tscline")); } catch (_) { /* ignore */ }
  if (!r.lines.some((l) => l.id === id)) id = r.lines[0]?.id ?? null;
  prod.line = id;
  if (id != null) $("#pLine").value = id;
}

$("#pLine").addEventListener("change", (e) => {
  prod.line = Number(e.target.value);
  prod.data = null;
  try { localStorage.setItem("gm.tscline", prod.line); } catch (_) { /* ignore */ }
  prodRender();
  prodPoll();
});

async function prodPoll() {
  clearTimeout(prod.timer);
  if (document.hidden || $("#tab-production").classList.contains("hidden")) return;
  try {
    if (!prod.lines) await prodLoadLines();
    if (prod.lines && prod.line != null) {
      const line = prod.line;
      const d = await api(`api/tsc/production?line=${encodeURIComponent(line)}`, { timeout: 60000 });
      if (line === prod.line) prod.data = d;
    }
  } catch (e) {
    prod.data = { ok: false, error: e.message };
  }
  prodRender();
  prod.timer = setTimeout(prodPoll, PROD_POLL_MS);
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "production") prodPoll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) prodPoll(); });

function prodRender() {
  const admin = !document.body.classList.contains("viewer");
  const setup = admin ? `Set it up under <b>Production &gt; Connection</b>.` : "Ask an admin to set it up.";
  if (prod.error) {
    $("#pNote").innerHTML = prod.error.configured === false
      ? `<p class="muted">No TSC database connection yet. ${setup}</p>`
      : `<p class="errtext small">Can't read the TSC database: ${esc(prod.error.error)}</p>`;
    $("#pBody").innerHTML = "";
    $("#pLive").textContent = $("#pShift").textContent = "";
    return;
  }
  const d = prod.data;
  if (!d) { $("#pNote").innerHTML = `<p class="muted small">Reading the part schedule...</p>`; $("#pBody").innerHTML = ""; return; }
  if (!d.ok) {
    $("#pNote").innerHTML = `<p class="errtext small">Can't read the TSC database: ${esc(d.error)}. Showing nothing rather than old numbers.</p>`;
    $("#pBody").innerHTML = "";
    return;
  }
  $("#pNote").innerHTML = d.source === "demo" ? `<p class="small muted">Simulated part schedule (server set to <b>demo</b>). The orders and parts are made up.</p>` : "";
  $("#pLive").textContent = `Read ${fmtHm(d.at)}${d.source === "demo" ? " (demo)" : ""}`;
  const sh = d.shift;
  $("#pShift").textContent = `${sh.name ? `${sh.name} shift` : "This shift"} ${fmtHm(sh.start)} to ${fmtHm(sh.end)}`
    + (sh.source === "settings" ? " (from the settings)" : "");
  const T = d.totals, Q = d.queue_total, D = d.downtime;
  const tiles = [
    ["Orders worked", pNum(T.orders), "", "Orders with a part finished or running this shift"],
    ["Pieces made", pNum(T.pieces), "", `${pNum(T.parts)} part lines finished, ${pNum(T.pieces_per_hour, 1)} pieces an hour`],
    ["Feet made", pNum(T.feet)], ["Feet / hour", pNum(T.feet_per_hour)],
  ];
  if (D) tiles.push(["Uptime", `${Math.round(D.uptime * 100)}%`, D.ongoing ? "err" : D.uptime < 0.8 ? "warn" : "ok",
    `${D.count} stop(s), ${fmtMins(D.seconds)} down this shift (TSC stop reasons)`]);
  tiles.push(["Scrap / remakes", `${pNum(T.scrap_pieces)} / ${pNum(T.remakes)}`, T.scrap_pieces ? "warn" : "", "Scrap pieces / remade part lines"],
    ["In queue", `${pNum(Q.orders)} orders`, "", `${pNum(Q.bundles)} bundles, ${pNum(Q.parts)} part lines, ${pNum(Q.pieces)} pieces, ${pNum(Q.feet)} ft`],
    ["On hold", `${pNum(d.held.length)} ${d.held.length === 1 ? "part" : "parts"}`, d.held.length ? "warn" : ""]);
  const tilesHtml = `<div class="tiles">${tiles.map(([l, n, c, t]) => `<div class="tile ${c || ""}" ${t ? `title="${esc(t)}"` : ""}><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div>${t ? `<div class="s">${esc(t)}</div>` : ""}</div>`).join("")}</div>`;
  $("#pBody").innerHTML = tilesHtml + stationsHtml(d) + runningHtml(d) + heldHtml(d)
    + `<div class="pgrid"><div class="panel"><h3>Feet per hour</h3>${hourlySvg(d.hourly)}</div>${downtimeHtml(d)}</div>`
    + ordersHtml(d) + queueHtml(d) + recentHtml(d);
}

function partSpec(p, unit) {
  return [p.profile, p.color, p.gauge != null ? `${p.gauge} ga` : "", fmtLen(p.length, unit)].filter(Boolean).map(esc).join(" &middot; ");
}

const STATION_STATE = { running: ["okchip", "running"], done: ["", "done with this part"], idle: ["", "idle"] };

// One card per station. A station that waits on others (e.g. the sandwich press waits on the pan and the back
// skin) is shown after them with what it waits for, so you can see which side is behind.
function stationsHtml(d) {
  if (!d.stations.length || (d.stations.length === 1 && !d.stations[0].waits_for.length)) return "";
  const cards = d.stations.map((s) => {
    const pct = Math.round((s.progress || 0) * 100);
    const [chip, word] = STATION_STATE[s.state] || ["", s.state];
    return `<div class="pstation ${esc(s.state)} ${s.waits_for.length ? "joins" : ""}">
      <div class="uahead"><b>${esc(s.name)}</b><span class="chip ${chip}">${esc(word)}</span></div>
      ${s.waits_for.length ? `<div class="small muted">waits for ${esc(s.waits_for.join(" + "))}</div>` : ""}
      ${s.order ? `<div class="small">Order ${esc(s.order)} &middot; ${esc(s.profile || "")} ${esc(fmtLen(s.length, d.unit))}</div>
        <div class="pbarwrap"><span style="width:${pct}%"></span></div>
        <div class="small"><b>${pNum(s.qty)}</b> of ${pNum(s.of)} pieces</div>` : `<div class="small muted">No part at this station.</div>`}</div>`;
  }).join("");
  return `<div class="panel"><h3>Stations</h3><div class="pstations">${cards}</div></div>`;
}

function runningHtml(d) {
  if (!d.running.length) {
    const why = d.downtime && d.downtime.ongoing ? `Stopped: ${esc(d.downtime.stops.find((s) => s.ongoing)?.reason || "")}` : "Nothing running on this line.";
    return `<div class="panel prun idle"><h3>Running now</h3><p class="muted">${why}</p></div>`;
  }
  return d.running.map((p) => {
    const pct = Math.round((p.progress || 0) * 100);
    const st = (p.stations || []).length > 1 ? `<div class="pststrip">${p.stations.map((s) => {
      const sp = p.quantity ? Math.round(Math.min(s.qty / p.quantity, 1) * 100) : 0;
      return `<div><span class="small">${esc(s.name)}</span><div class="pbarwrap sm"><span style="width:${sp}%"></span></div><span class="small muted">${pNum(s.qty)}</span></div>`;
    }).join("")}</div>` : "";
    return `<div class="panel prun"><div class="uahead"><h3>Running now: order ${esc(p.order)}</h3><span class="small muted">started ${esc(fmtHm(p.start))}</span></div>
      <div class="pspec">${partSpec(p, d.unit)}</div>
      <div class="pbig"><div class="pbarwrap"><span style="width:${pct}%"></span></div>
        <div class="pcount"><b>${pNum(p.actual)}</b> of ${pNum(p.quantity)} pieces (${pct}%)</div></div>
      ${st}
      <div class="mhealth">
        <div><div class="k">Pieces left</div><div class="v">${pNum(p.remaining)}</div></div>
        <div><div class="k">Running for</div><div class="v">${esc(fmtMins(p.elapsed_s))}</div></div>
        <div><div class="k">Done in about</div><div class="v">${p.eta_s != null ? esc(fmtMins(p.eta_s)) : "-"}</div></div>
        <div><div class="k">Bundle / piece mark</div><div class="v">${esc(p.bundle || "-")} / ${esc(p.piece_mark || "-")}</div></div>
        <div><div class="k">Coil</div><div class="v">${esc(p.coil || "-")}</div></div>
        <div><div class="k">Part line</div><div class="v">${esc(pNum(p.feet))} ft</div></div>
      </div></div>`;
  }).join("");
}

function heldHtml(d) {
  if (!d.held.length) return "";
  return `<div class="panel mbanner warn"><h3>On hold</h3>${d.held.map((p) =>
    `<div class="small">Order <b>${esc(p.order)}</b>, bundle ${esc(p.bundle || "-")}: ${partSpec(p, d.unit)}, ${pNum(p.quantity)} pieces <span class="chip warnchip">${esc(p.status || "hold")}</span></div>`).join("")}</div>`;
}

function hourlySvg(hours) {
  const W = 600, H = 190, top = 18, bottom = 22, n = hours.length, slot = W / n;
  const max = Math.max(1, ...hours.map((h) => h.feet));
  const bars = hours.map((h, i) => {
    const bh = Math.round((h.feet / max) * (H - top - bottom));
    const x = i * slot + slot * 0.15, y = H - bottom - bh, w = slot * 0.7;
    const label = new Date(h.start * 1000).toLocaleTimeString([], { hour: "2-digit" });
    return `<g><title>${esc(label)}: ${esc(pNum(h.feet))} ft, ${esc(pNum(h.pieces))} pieces, ${esc(h.parts)} part lines</title>
      <rect class="pbar${i === n - 1 ? " now" : ""}" x="${x}" y="${y}" width="${w}" height="${Math.max(bh, h.feet ? 1 : 0)}" rx="2"></rect>
      ${h.feet ? `<text class="pval" x="${x + w / 2}" y="${y - 4}" text-anchor="middle">${esc(pNum(h.feet))}</text>` : ""}
      <text class="plab" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${esc(label)}</text></g>`;
  }).join("");
  return `<svg class="pchart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Feet produced per hour">
    <line class="paxis" x1="0" x2="${W}" y1="${H - bottom}" y2="${H - bottom}"></line>${bars}</svg>
    <p class="small muted">Last 12 hours by clock hour; the last bar is the hour in progress.</p>`;
}

function downtimeHtml(d) {
  const D = d.downtime;
  if (!D) return `<div class="panel"><h3>Stops this shift</h3><p class="small muted">TSC's stop reasons aren't readable with this SQL login.
    See <b>What Ghost Map can read</b> on the Connection page.</p></div>`;
  if (!D.count) return `<div class="panel"><h3>Stops this shift</h3><p class="small muted">No stops recorded this shift.</p></div>`;
  const max = Math.max(1, ...D.reasons.map((r) => r.seconds));
  const reasons = D.reasons.map((r) => `<div class="preason"><span class="l" title="${esc(r.reason)}">${esc(r.reason)}</span>
    <div class="pbarwrap"><span class="down" style="width:${Math.round((r.seconds / max) * 100)}%"></span></div>
    <span class="small">${esc(fmtMins(r.seconds))} &middot; ${r.stops}x</span></div>`).join("");
  const stops = D.stops.slice(0, 6).map((s) => `<tr><td>${esc(fmtHm(s.stop))}</td><td>${esc(s.reason)}</td>
    <td>${s.ongoing ? `<span class="chip errchip">still stopped</span>` : esc(fmtMins(s.seconds))}</td></tr>`).join("");
  return `<div class="panel"><h3>Stops this shift: ${D.count}, ${esc(fmtMins(D.seconds))} down</h3>${reasons}
    <div class="tablewrap"><table class="ptable"><thead><tr><th>Stopped</th><th>Reason</th><th>For</th></tr></thead><tbody>${stops}</tbody></table></div></div>`;
}

function ordersHtml(d) {
  if (!d.orders.length) return `<div class="panel"><h3>Orders on the schedule</h3><p class="muted small">No open orders.</p></div>`;
  const rows = d.orders.map((o) => {
    const known = o.pieces != null, pct = known && o.pieces ? Math.round((o.pieces_done / o.pieces) * 100) : 0;
    return `<tr><td><b>${esc(o.order)}</b> ${o.running ? `<span class="chip okchip">running</span>` : ""}${o.held ? `<span class="chip warnchip">${o.held} on hold</span>` : ""}</td>
      <td>${known ? pNum(o.bundles) : "-"}</td>
      <td>${known ? `<div class="pbarwrap sm"><span style="width:${pct}%"></span></div>` : ""}</td>
      <td>${known ? `${pNum(o.pieces_done)} / ${pNum(o.pieces)}` : "-"}</td>
      <td>${known ? `${pNum(o.parts_done)} / ${pNum(o.parts)}` : "-"}</td><td>${known ? pNum(o.feet_left) : "-"}</td></tr>`;
  }).join("");
  return `<div class="panel"><h3>Orders on the schedule</h3><div class="tablewrap"><table class="ptable"><thead><tr>
    <th>Order</th><th>Bundles</th><th>Progress</th><th>Pieces done</th><th>Part lines done</th><th>Feet left</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="small muted">Whole orders, in the order they come up on the line. A part line is one row of the cut list (a length and a quantity).</p></div>`;
}

function stationPills(p) {
  return (p.stations || []).map((s) => `<span class="chip" title="${esc(s.name)}">${esc(s.id)}</span>`).join("");
}

function queueHtml(d) {
  const more = d.queue_total.parts - d.queue.length;
  const multi = d.stations.length > 1;
  const rows = d.queue.map((p, i) => `<tr class="${p.state === "held" ? "held" : ""}"><td>${i + 1}</td><td>${esc(p.order)}</td><td>${esc(p.bundle)}</td>
    <td>${esc(p.profile)}</td><td>${esc(p.color)}</td><td>${p.gauge != null ? esc(p.gauge) : "-"}</td><td>${esc(fmtLen(p.length, d.unit))}</td>
    <td>${pNum(p.quantity)}</td><td>${pNum(p.feet)}</td>${multi ? `<td>${stationPills(p)}</td>` : ""}
    <td>${p.state === "held" ? `<span class="chip warnchip">on hold</span>` : ""}</td></tr>`).join("");
  return `<div class="panel"><h3>Up next: ${pNum(d.queue_total.orders)} orders, ${pNum(d.queue_total.bundles)} bundles, ${pNum(d.queue_total.pieces)} pieces</h3>
    ${d.queue.length ? `<div class="tablewrap"><table class="ptable"><thead><tr><th>#</th><th>Order</th><th>Bundle</th><th>Profile</th>
    <th>Color</th><th>Gauge</th><th>Length</th><th>Pieces</th><th>Feet</th>${multi ? "<th>Stations</th>" : ""}<th></th></tr></thead><tbody>${rows}</tbody></table></div>
    ${more > 0 ? `<p class="small muted">and ${more} more part line(s) after these.</p>` : ""}
    <p class="small muted">In TSC's run order. Parts on hold stay in the queue but the line skips them.</p>` : `<p class="muted small">The queue is empty.</p>`}</div>`;
}

function recentHtml(d) {
  const rows = d.recent.map((p) => `<tr><td>${esc(fmtHm(p.end))}</td><td>${esc(p.order)}</td><td>${esc(p.bundle)}</td><td>${esc(p.profile)}</td>
    <td>${esc(fmtLen(p.length, d.unit))}</td><td>${pNum(p.actual)}</td><td>${pNum(p.feet)}</td>
    <td>${p.start && p.end ? esc(fmtMins(p.end - p.start)) : "-"}</td>
    <td>${p.scrap ? `<span class="chip errchip">scrap</span>` : ""}${p.remake ? `<span class="chip warnchip">remake</span>` : ""}${p.error ? esc(p.error) : ""}</td></tr>`).join("");
  return `<div class="panel"><h3>Recently completed</h3>${d.recent.length ? `<div class="tablewrap"><table class="ptable"><thead><tr><th>Done</th><th>Order</th>
    <th>Bundle</th><th>Profile</th><th>Length</th><th>Pieces</th><th>Feet</th><th>Took</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<p class="muted small">Nothing finished in the last 12 hours.</p>`}</div>`;
}

// ------------------------------------------------------------------ connection page
function tscForm() {
  const f = $("#tForm");
  return { server: f.server.value.trim(), database: f.database.value.trim(), username: f.username.value.trim(),
    password: f.password.value, encrypt: f.encrypt.checked, trust_cert: f.trust_cert.checked, cafile: f.cafile.value.trim(),
    view: f.view.value.trim(), queue_view: f.queue_view.value.trim(), done_view: f.done_view.value.trim(), length_unit: f.length_unit.value, shifts: f.shifts.value.trim(), cache_s: Number(f.cache_s.value) || 30 };
}
function tscCaToggle() {
  const f = $("#tForm");
  $("#tForm .tca").classList.toggle("hidden", !f.encrypt.checked || f.trust_cert.checked);
  f.trust_cert.closest("label").classList.toggle("hidden", !f.encrypt.checked);
}
$("#tForm").encrypt.addEventListener("change", tscCaToggle);
$("#tForm").trust_cert.addEventListener("change", tscCaToggle);

async function tscLoad() {
  if (document.body.classList.contains("viewer")) return;
  try {
    const c = await api("api/tsc/config");
    const f = $("#tForm");
    for (const k of ["server", "database", "username", "cafile", "view", "queue_view", "done_view", "length_unit", "shifts", "cache_s"]) f[k].value = c[k] ?? "";
    f.encrypt.checked = !!c.encrypt;
    f.trust_cert.checked = c.trust_cert !== false;
    f.password.value = "";
    f.password.placeholder = c.has_password ? "saved (leave blank to keep it)" : "";
    $("#tClear").classList.toggle("hidden", !c.configured);
    $("#tErr").textContent = c.load_error ? `The saved settings could not be loaded: ${c.load_error}` : "";
    tscCaToggle();
  } catch (e) { $("#tErr").textContent = e.message; }
  tscStatus();
}

async function tscStatus() {
  try {
    const s = await api("api/tsc/status");
    if (!s.configured) { $("#tStatus").innerHTML = `<span class="muted">Not set up yet.</span>`; return; }
    const ok = s.last_ok && (!s.last_error_at || s.last_ok > s.last_error_at);
    $("#tStatus").innerHTML = `<div class="mhealth">
      <div><div class="k">Source</div><div class="v">${s.source === "demo" ? "simulated (demo)" : `${esc(s.server)} / ${esc(s.database)}`}</div></div>
      <div><div class="k">Queue view</div><div class="v mono">${esc(s.queue_view)}</div></div>
      <div><div class="k">Last good read</div><div class="v ${ok ? "oktext" : ""}">${s.last_ok ? esc(fmtHm(s.last_ok)) : "none yet"}</div></div>
      <div><div class="k">Query time</div><div class="v">${s.last_ms != null ? `${s.last_ms} ms` : "-"}</div></div>
      <div><div class="k">Reads since start</div><div class="v">${pNum(s.queries)}</div></div></div>
      ${s.last_error ? `<p class="${ok ? "muted" : "errtext"} small">Last error (${esc(fmtHm(s.last_error_at))}): ${esc(s.last_error)}</p>` : ""}`;
    tscFeatures(s);
  } catch (e) { $("#tStatus").textContent = e.message; }
}
// The optional reads (stations, shifts, stops, strokes) and the GRANT each one needs, from what the last reads found.
function tscFeatures(s) {
  const f = Object.entries(s.features || {});
  const seen = f.some(([, x]) => x.ok != null);
  if (!s.configured || !seen) { $("#tFeatures").innerHTML = `<span class="muted">Shown after the Line or Counters page has read from TSC once.</span>`; return; }
  const missing = f.filter(([, x]) => x.ok === false);
  $("#tFeatures").innerHTML = `<table class="ptable tfeat"><tbody>${f.map(([, x]) => `<tr><td>${x.ok ? `<span class="chip okchip">readable</span>`
      : x.ok === false ? `<span class="chip warnchip">no access</span>` : `<span class="chip">not tried</span>`}</td>
      <td>${esc(x.description)}${x.ok === false ? `<div class="muted">${esc(x.error)}</div>` : ""}</td></tr>`).join("")}</tbody></table>
    ${missing.length ? `<p>To turn these on, a SQL Server admin runs (on the TSC database):</p>
      <pre class="mono tgrant">${esc(missing.map(([, x]) => x.grant.split("; ").join(";\n")).join("\n"))}</pre>` : ""}`;
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "tscconn") tscLoad(); });

$("#tTest").addEventListener("click", async () => {
  $("#tErr").textContent = "";
  $("#tTestOut").innerHTML = `<span class="muted">Connecting...</span>`;
  try {
    const r = await api("api/tsc/test", { method: "POST", body: JSON.stringify(tscForm()), timeout: 60000 });
    $("#tTestOut").innerHTML = r.ok
      ? `<span class="oktext">Connected and read the view in ${esc(r.ms)} ms.</span> Lines: ${r.lines.map((l) => esc(`${l.name} (${l.id})`)).join(", ") || "none found"}.`
      : `<span class="errtext">${esc(r.error)}</span>`;
  } catch (e) { $("#tTestOut").innerHTML = `<span class="errtext">${esc(e.message)}</span>`; }
});

$("#tForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#tErr").textContent = "";
  try {
    await api("api/tsc/config", { method: "POST", body: JSON.stringify(tscForm()) });
    $("#tTestOut").innerHTML = `<span class="oktext">Saved.</span>`;
    prod.lines = null;
    prod.data = null;
    await tscLoad();
  } catch (e) { $("#tErr").textContent = e.message; }
});

$("#tClear").addEventListener("click", async () => {
  if (!confirm("Remove the TSC connection settings (and the saved password)?")) return;
  try {
    await api("api/tsc/config", { method: "DELETE" });
    prod.lines = null;
    prod.data = null;
    $("#tForm").reset();
    await tscLoad();
  } catch (e) { $("#tErr").textContent = e.message; }
});

$("#tImport").addEventListener("click", () => window.openImport());
document.addEventListener("gm:imported", () => { prod.lines = null; prod.data = null; tscLoad(); });
