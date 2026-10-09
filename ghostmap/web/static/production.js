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
  $("#pNote").innerHTML = d.source === "demo" ? `<p class="small muted">Simulated part schedule (server set to <b>demo</b>). The parts and orders are made up.</p>` : "";
  $("#pLive").textContent = `Read ${fmtHm(d.at)}${d.source === "demo" ? " (demo)" : ""}`;
  $("#pShift").textContent = `This shift since ${fmtHm(d.shift_start)}`;
  const T = d.totals, Q = d.queue_total;
  const tiles = [
    ["Parts done", pNum(T.parts)], ["Pieces", pNum(T.pieces)], ["Feet", pNum(T.feet)],
    ["Feet / hour", pNum(T.feet_per_hour)], ["Scrap / remakes", `${pNum(T.scrap)} / ${pNum(T.remakes)}`, T.scrap ? "warn" : ""],
    ["In queue", `${pNum(Q.parts)} parts`, "", `${pNum(Q.pieces)} pieces, ${pNum(Q.feet)} ft`],
    ["On hold", pNum(d.held.length), d.held.length ? "warn" : ""],
  ];
  const tilesHtml = `<div class="tiles">${tiles.map(([l, n, c, t]) => `<div class="tile ${c || ""}" ${t ? `title="${esc(t)}"` : ""}><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`).join("")}</div>`;
  $("#pBody").innerHTML = tilesHtml + runningHtml(d) + heldHtml(d)
    + `<div class="pgrid"><div class="panel"><h3>Feet per hour</h3>${hourlySvg(d.hourly)}</div>${ordersHtml(d)}</div>`
    + queueHtml(d) + recentHtml(d);
}

function partSpec(p, unit) {
  return [p.profile, p.color, p.gauge != null ? `${p.gauge} ga` : "", fmtLen(p.length, unit)].filter(Boolean).map(esc).join(" &middot; ");
}

function runningHtml(d) {
  if (!d.running.length) return `<div class="panel prun idle"><h3>Running now</h3><p class="muted">Nothing running on this line.</p></div>`;
  return d.running.map((p) => {
    const pct = Math.round((p.progress || 0) * 100);
    return `<div class="panel prun"><div class="uahead"><h3>Running now: order ${esc(p.order)}</h3><span class="small muted">started ${esc(fmtHm(p.start))}</span></div>
      <div class="pspec">${partSpec(p, d.unit)}</div>
      <div class="pbig"><div class="pbarwrap"><span style="width:${pct}%"></span></div>
        <div class="pcount"><b>${pNum(p.actual)}</b> of ${pNum(p.requested)} pieces (${pct}%)</div></div>
      <div class="mhealth">
        <div><div class="k">Remaining</div><div class="v">${pNum(p.remaining)} pieces</div></div>
        <div><div class="k">Running for</div><div class="v">${esc(fmtMins(p.elapsed_s))}</div></div>
        <div><div class="k">Done in about</div><div class="v">${p.eta_s != null ? esc(fmtMins(p.eta_s)) : "-"}</div></div>
        <div><div class="k">Bundle / piece mark</div><div class="v">${esc(p.bundle || "-")} / ${esc(p.piece_mark || "-")}</div></div>
        <div><div class="k">Part</div><div class="v">${esc(pNum(p.feet))} ft total</div></div>
      </div></div>`;
  }).join("");
}

function heldHtml(d) {
  if (!d.held.length) return "";
  return `<div class="panel mbanner warn"><h3>On hold</h3>${d.held.map((p) =>
    `<div class="small">Order <b>${esc(p.order)}</b>: ${partSpec(p, d.unit)}, ${pNum(p.requested)} pieces <span class="chip warnchip">${esc(p.status)}</span></div>`).join("")}</div>`;
}

function hourlySvg(hours) {
  const W = 600, H = 190, top = 18, bottom = 22, n = hours.length, slot = W / n;
  const max = Math.max(1, ...hours.map((h) => h.feet));
  const bars = hours.map((h, i) => {
    const bh = Math.round((h.feet / max) * (H - top - bottom));
    const x = i * slot + slot * 0.15, y = H - bottom - bh, w = slot * 0.7;
    const label = new Date(h.start * 1000).toLocaleTimeString([], { hour: "2-digit" });
    return `<g><title>${esc(label)}: ${esc(pNum(h.feet))} ft, ${esc(pNum(h.pieces))} pieces, ${esc(h.parts)} parts</title>
      <rect class="pbar${i === n - 1 ? " now" : ""}" x="${x}" y="${y}" width="${w}" height="${Math.max(bh, h.feet ? 1 : 0)}" rx="2"></rect>
      ${h.feet ? `<text class="pval" x="${x + w / 2}" y="${y - 4}" text-anchor="middle">${esc(pNum(h.feet))}</text>` : ""}
      <text class="plab" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${esc(label)}</text></g>`;
  }).join("");
  return `<svg class="pchart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Feet produced per hour">
    <line class="paxis" x1="0" x2="${W}" y1="${H - bottom}" y2="${H - bottom}"></line>${bars}</svg>
    <p class="small muted">Last 12 hours by clock hour; the last bar is the hour in progress.</p>`;
}

function ordersHtml(d) {
  if (!d.orders.length) return `<div class="panel"><h3>Orders on the schedule</h3><p class="muted small">No open orders.</p></div>`;
  const rows = d.orders.map((o) => {
    const total = o.open_parts + o.done_parts, pct = total ? Math.round((o.done_parts / total) * 100) : 0;
    return `<tr><td><b>${esc(o.order)}</b> ${o.running ? `<span class="chip okchip">running</span>` : ""}${o.held ? `<span class="chip warnchip">${o.held} held</span>` : ""}</td>
      <td><div class="pbarwrap sm"><span style="width:${pct}%"></span></div></td>
      <td>${o.done_parts} / ${total}</td><td>${pNum(o.open_pieces)}</td><td>${pNum(o.open_feet)}</td></tr>`;
  }).join("");
  return `<div class="panel"><h3>Orders on the schedule</h3><div class="tablewrap"><table class="ptable"><thead><tr>
    <th>Order</th><th>Progress</th><th>Parts done</th><th>Pieces left</th><th>Feet left</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="small muted">Parts done counts parts finished in the last 12 hours or this shift.</p></div>`;
}

function queueHtml(d) {
  const more = d.queue_total.parts - d.queue.length;
  const rows = d.queue.map((p, i) => `<tr><td>${i + 1}</td><td>${esc(p.order)}</td><td>${esc(p.profile)}</td><td>${esc(p.color)}</td>
    <td>${p.gauge != null ? esc(p.gauge) : "-"}</td><td>${esc(fmtLen(p.length, d.unit))}</td><td>${pNum(p.requested)}</td>
    <td>${pNum(p.feet)}</td><td>${esc(p.bundle)}</td></tr>`).join("");
  return `<div class="panel"><h3>Up next</h3>${d.queue.length ? `<div class="tablewrap"><table class="ptable"><thead><tr><th>#</th><th>Order</th><th>Profile</th>
    <th>Color</th><th>Gauge</th><th>Length</th><th>Pieces</th><th>Feet</th><th>Bundle</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${more > 0 ? `<p class="small muted">and ${more} more part(s) after these.</p>` : ""}` : `<p class="muted small">The queue is empty.</p>`}</div>`;
}

function recentHtml(d) {
  const rows = d.recent.map((p) => `<tr><td>${esc(fmtHm(p.end))}</td><td>${esc(p.order)}</td><td>${esc(p.profile)}</td>
    <td>${esc(fmtLen(p.length, d.unit))}</td><td>${pNum(p.actual)}</td><td>${pNum(p.feet)}</td>
    <td>${p.start && p.end ? esc(fmtMins(p.end - p.start)) : "-"}</td>
    <td>${p.scrap ? `<span class="chip errchip">scrap</span>` : ""}${p.remake ? `<span class="chip warnchip">remake</span>` : ""}${p.error ? esc(p.error) : ""}</td></tr>`).join("");
  return `<div class="panel"><h3>Recently completed</h3>${d.recent.length ? `<div class="tablewrap"><table class="ptable"><thead><tr><th>Done</th><th>Order</th>
    <th>Profile</th><th>Length</th><th>Pieces</th><th>Feet</th><th>Took</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<p class="muted small">Nothing finished in the last 12 hours.</p>`}</div>`;
}

// ------------------------------------------------------------------ connection page
function tscForm() {
  const f = $("#tForm");
  return { server: f.server.value.trim(), database: f.database.value.trim(), username: f.username.value.trim(),
    password: f.password.value, encrypt: f.encrypt.checked, trust_cert: f.trust_cert.checked, cafile: f.cafile.value.trim(),
    view: f.view.value.trim(), length_unit: f.length_unit.value, shifts: f.shifts.value.trim(), cache_s: Number(f.cache_s.value) || 30 };
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
    for (const k of ["server", "database", "username", "cafile", "view", "length_unit", "shifts", "cache_s"]) f[k].value = c[k] ?? "";
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
      <div><div class="k">View</div><div class="v mono">${esc(s.view)}</div></div>
      <div><div class="k">Last good read</div><div class="v ${ok ? "oktext" : ""}">${s.last_ok ? esc(fmtHm(s.last_ok)) : "none yet"}</div></div>
      <div><div class="k">Query time</div><div class="v">${s.last_ms != null ? `${s.last_ms} ms` : "-"}</div></div>
      <div><div class="k">Reads since start</div><div class="v">${pNum(s.queries)}</div></div></div>
      ${s.last_error ? `<p class="${ok ? "muted" : "errtext"} small">Last error (${esc(fmtHm(s.last_error_at))}): ${esc(s.last_error)}</p>` : ""}`;
  } catch (e) { $("#tStatus").textContent = e.message; }
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
