"use strict";
// Probe: the device inspector. One button runs POST api/inspect (ports, latency, CIP identity and services, where
// the device is plugged in, what changed since earlier scans, a verdict); Live trace times one TCP connect a second.
// Uses $, esc, api, ring and state from app.js.

const probe = { data: null, live: null, trace: [], tracePort: null, traceIp: null };
const LIVE_MS = 1000, TRACE_POINTS = 90;
const LEVEL = { err: ["errchip", "&#10006;"], warn: ["warnchip", "&#9888;"], info: ["", "&#8505;"], ok: ["okchip", "&#10004;"] };
const pms = (v) => (v == null ? "-" : v < 10 ? `${v.toFixed(2)} ms` : `${Math.round(v)} ms`);

function probeIps() {
  const ips = (state.scan?.devices || []).filter((d) => d.ip).map((d) => [d.ip, d.identity ? d.identity.product_name : d.mac_vendor || ""]);
  $("#probeIps").innerHTML = ips.map(([ip, n]) => `<option value="${esc(ip)}">${esc(n)}</option>`).join("");
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "probe") probeIps(); else probeStopLive(); });

async function runProbe() {
  const ip = $("#probeIp").value.trim();
  if (!ip) return;
  probeStopLive();
  $("#probeOut").innerHTML = `<div class="panel probescan"><div class="scanline"></div><p class="muted">Inspecting ${esc(ip)}: ports, ten timed connects,
    ListIdentity and ListServices...</p></div>`;
  try {
    probe.data = await api("api/inspect", { method: "POST", body: JSON.stringify({ ip, community: $("#probeCommunity").value }), timeout: 60000 });
    probeRender();
  } catch (e) {
    $("#probeOut").innerHTML = `<div class="panel"><p class="errtext">${esc(e.message)}</p></div>`;
  }
}
$("#probeForm").addEventListener("submit", (e) => { e.preventDefault(); runProbe(); });

function score(checks) {
  let s = 100;
  for (const c of checks) s -= c.level === "err" ? 35 : c.level === "warn" ? 10 : 0;
  return Math.max(0, s);
}

function probeRender() {
  const r = probe.data, ctx = r.context, i = r.identity, dev = ctx.device;
  const sc = score(r.checks), cls = sc >= 85 ? "ok" : sc >= 55 ? "warn" : "err";
  const name = i ? i.product_name : dev?.mac_vendor || (r.latency.port ? "Network device" : "No answer");
  const sub = [i ? i.vendor_name : "", i ? `firmware ${i.revision}` : "", dev?.mac ? `MAC ${dev.mac}` : ""].filter(Boolean).map(esc).join(" &middot; ");
  const hero = `<div class="panel phero"><div class="phead">
      ${ring(sc, sc, cls + " big", 92, 8, "health")}
      <div class="grow"><div class="small muted">${r.simulated ? `<span class="chip">simulated demo device</span>` : ""}Inspected ${esc(new Date(r.at * 1000).toLocaleTimeString())}</div>
        <h2 class="pname">${esc(name)}</h2><div class="mono">${esc(r.ip)}</div><div class="small muted">${sub}</div></div>
      <div class="pstats">${stat("Average", pms(r.latency.avg))}${stat("Jitter", pms(r.latency.jitter))}
        ${stat("Lost", `${r.latency.lost}/${r.latency.sent}`, r.latency.lost ? "err" : "")}${stat("Open ports", r.ports.filter((p) => p.open).length)}</div>
    </div></div>`;
  const checks = `<div class="panel"><h3>Verdict</h3>${r.checks.map((c) => {
    const [chip, icon] = LEVEL[c.level];
    return `<div class="pcheck ${esc(c.level)}"><span class="chip ${chip}">${icon}</span><div><div>${esc(c.text)}</div>
      ${c.hint ? `<div class="small muted">${esc(c.hint)}</div>` : ""}</div></div>`;
  }).join("")}</div>`;
  const lat = `<div class="panel"><div class="uahead"><h3>Response time</h3><span class="small muted">${r.latency.port ? `TCP ${r.latency.port}, ${r.latency.sent} connects` : "no open port to time"}</span></div>
    ${latencyBars(r.latency.samples)}
    <div class="mhealth">${stat("Fastest", pms(r.latency.min))}${stat("Average", pms(r.latency.avg))}${stat("Slowest", pms(r.latency.max))}${stat("Jitter", pms(r.latency.jitter))}</div>
    <div id="probeTrace">${r.latency.port ? `<p class="small muted">Press <b>Live trace</b> to watch it for a while (one connect a second).</p>` : ""}</div></div>`;
  const ports = `<div class="panel"><h3>Ports</h3><div class="pports">${r.ports.map((p) => `<div class="pport ${p.open ? (p.note ? "warn" : "open") : ""}" title="${esc(p.note || p.service)}">
      <b class="mono">${p.port}</b><span>${esc(p.service)}</span><small>${p.open ? "open" : "closed"}</small></div>`).join("")}</div></div>`;
  const cip = i ? `<div class="panel detail"><h3>EtherNet/IP identity</h3><dl>
      <dt>Vendor</dt><dd>${esc(i.vendor_name)} (${i.vendor_id})</dd><dt>Type</dt><dd>${esc(i.device_type_name)}</dd>
      <dt>Product</dt><dd>${esc(i.product_name)} (code ${i.product_code})</dd><dt>Firmware</dt><dd class="mono">${esc(i.revision)}</dd>
      <dt>Serial</dt><dd class="mono">${esc(i.serial_hex)}</dd><dt>State</dt><dd>${esc(i.state_name)}</dd>
      <dt>Status</dt><dd>${esc(i.extended_status)}${(i.status_flags || []).map((f) => ` <span class="chip">${esc(f)}</span>`).join("")}</dd>
      <dt>Says its IP is</dt><dd class="mono">${esc(i.socket_ip)}</dd>
      <dt>Services</dt><dd>${(r.services || []).map((s) => (s.capabilities.length ? s.capabilities : [s.name]).map((t) => `<span class="chip okchip">${esc(t)}</span>`).join("")).join("") || "<span class=\"muted\">no ListServices answer</span>"}</dd>
      </dl></div>` : `<div class="panel"><h3>EtherNet/IP identity</h3><p class="small muted">No ListIdentity answer: not a CIP device, or UDP 44818 is blocked on the path.</p></div>`;
  const snmp = r.snmp ? `<div class="panel detail"><h3>SNMP</h3>${r.snmp.ok ? `<dl><dt>Name</dt><dd>${esc(r.snmp.name || "-")}</dd>
      <dt>Description</dt><dd>${esc(r.snmp.descr || "-")}</dd><dt>Up for</dt><dd>${r.snmp.uptime_s != null ? esc(fmtUp(r.snmp.uptime_s)) : "-"}</dd>
      <dt>Location</dt><dd>${esc(r.snmp.location || "-")}</dd><dt>Contact</dt><dd>${esc(r.snmp.contact || "-")}</dd></dl>`
      : `<p class="small muted">No SNMP answer with that community (${esc(r.snmp.error)}).</p>`}</div>` : "";
  $("#probeOut").innerHTML = hero + `<div class="pgrid">${checks}${lat}</div>` + pathHtml(r, ctx)
    + `<div class="pgrid">${cip}${ports}</div>` + snmp + historyHtml(ctx);
  $("#probeOut").querySelectorAll("[data-probe-switch]").forEach((b) => b.addEventListener("click", () => {
    $("#probeIp").value = b.dataset.probeSwitch;
    runProbe();
  }));
}

function stat(k, v, cls = "") {
  return `<div><div class="k">${esc(k)}</div><div class="v ${cls === "err" ? "errtext" : ""}">${esc(v)}</div></div>`;
}

function fmtUp(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

function latencyBars(samples) {
  if (!samples.length) return "";
  const W = 400, H = 70, n = samples.length, slot = W / n, max = Math.max(1, ...samples.filter((s) => s != null));
  return `<svg class="pchart plat" viewBox="0 0 ${W} ${H}" role="img" aria-label="Connect times"><defs><linearGradient id="gmLatGrad" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" style="stop-color:var(--accent)"></stop><stop offset="1" style="stop-color:var(--accent-2)"></stop></linearGradient></defs>${samples.map((s, k) => {
    const x = k * slot + slot * 0.2, w = slot * 0.6;
    if (s == null) return `<g><title>attempt ${k + 1}: no answer</title><rect class="lost" x="${x}" y="4" width="${w}" height="${H - 8}" rx="2"></rect></g>`;
    const h = Math.max(2, (s / max) * (H - 10));
    return `<g><title>attempt ${k + 1}: ${pms(s)}</title><rect class="pbar" x="${x}" y="${H - 2 - h}" width="${w}" height="${h}" rx="2"></rect></g>`;
  }).join("")}</svg>`;
}

// This PC -> switch (port, speed, duplex, errors) -> the device, from the newest scan that saw it.
function pathHtml(r, ctx) {
  if (!ctx.scan) return `<div class="panel"><h3>Where it is plugged in</h3><p class="small muted">No saved scan has seen ${esc(r.ip)}. Run a scan that includes it
    (with the switch's SNMP) to see its switch port here.</p></div>`;
  const p = ctx.port, sw = ctx.switch;
  const portCls = !p ? "" : p.duplex === "half" || p.fcs_errors || p.in_errors ? "err" : p.oper_status === "up" ? "ok" : "warn";
  const errs = p ? (p.in_errors || 0) + (p.fcs_errors || 0) + (p.late_collisions || 0) : 0;
  const node = (title, body, cls = "") => `<div class="pnode ${cls}"><div class="small muted">${title}</div>${body}</div>`;
  const link = (txt, cls = "") => `<div class="plink ${cls}"><span>${txt}</span></div>`;
  return `<div class="panel"><div class="uahead"><h3>Where it is plugged in</h3><span class="small muted">from scan ${esc(ctx.scan.id)}</span></div>
    <div class="ppath">${node("Ghost Map", "<b>This PC</b>")}${link("network")}
      ${sw ? node("Switch", `<b>${esc(sw.name || sw.ip)}</b><div class="small mono">${esc(sw.ip)}</div>
        <button class="btn ghost small" data-probe-switch="${esc(sw.ip)}" type="button">Inspect switch</button>`) : node("Switch", `<span class="muted">not known</span>`)}
      ${link(p ? `${esc(p.name)}${p.alias ? ` &middot; ${esc(p.alias)}` : ""}<br>${p.speed_mbps ? `${p.speed_mbps} Mb` : "?"} ${esc(p.duplex)} duplex${errs ? `<br><b>${errs.toLocaleString()} errors</b>` : ""}` : "port not known", portCls)}
      ${node("Device", `<b>${esc(r.identity ? r.identity.product_name : ctx.device?.mac_vendor || "Device")}</b><div class="small mono">${esc(r.ip)}</div>`, "dev")}</div>
    ${ctx.findings.length ? `<div class="pfind">${ctx.findings.map((f) => findingHtml(f)).join("")}</div>` : ""}</div>`;
}

function historyHtml(ctx) {
  if (ctx.history.length < 2) return "";
  const rows = ctx.history.map((h, k) => {
    const prev = ctx.history[k + 1];
    const mark = (a, b) => (prev && a !== b ? ` class="changed"` : "");
    return `<tr><td>${esc(h.scan)}</td><td>${esc(h.started ? new Date(h.started).toLocaleString() : "")}</td><td>${esc(h.product)}</td>
      <td${mark(h.revision, prev?.revision)} class="mono">${esc(h.revision)}</td><td${mark(h.serial, prev?.serial)} class="mono">${esc(h.serial)}</td>
      <td class="mono">${esc(h.mac)}</td><td${mark(h.port, prev?.port)}>${esc(h.port)}</td></tr>`;
  }).join("");
  return `<div class="panel"><h3>At this address in earlier scans</h3><div class="tablewrap"><table class="ptable phist"><thead><tr><th>Scan</th><th>When</th>
    <th>Product</th><th>Firmware</th><th>Serial</th><th>MAC</th><th>Switch port</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="small muted">Highlighted: changed since the scan before (a replaced module, a firmware update, a moved cable).</p></div>`;
}

// ------------------------------------------------------------------ live trace
function probeStopLive() {
  clearTimeout(probe.live);
  probe.live = null;
  $("#probeLive").classList.remove("on");
  $("#probeLive").textContent = "Live trace";
}

async function probeTick() {
  try {
    const r = await api(`api/probe/${encodeURIComponent(probe.traceIp)}/ping?port=${probe.tracePort}`, { timeout: 5000 });
    probe.trace.push(r.ms);
  } catch (e) {
    if (!/too fast/.test(e.message)) probe.trace.push(null);
  }
  probe.trace = probe.trace.slice(-TRACE_POINTS);
  drawTrace();
  if (probe.live !== null) probe.live = setTimeout(probeTick, LIVE_MS);
}

$("#probeLive").addEventListener("click", () => {
  if (probe.live !== null) { probeStopLive(); return; }
  const ip = $("#probeIp").value.trim();
  if (!ip) return;
  if (!probe.data || probe.data.ip !== ip) { runProbe().then(() => startLive(ip)); return; }
  startLive(ip);
});

function startLive(ip) {
  const port = probe.data?.latency?.port || 44818;
  if (probe.traceIp !== ip || probe.tracePort !== port) probe.trace = [];
  probe.traceIp = ip;
  probe.tracePort = port;
  $("#probeLive").classList.add("on");
  $("#probeLive").textContent = "Stop trace";
  probe.live = 0;
  probeTick();
}

function drawTrace() {
  const el = $("#probeTrace");
  if (!el) return;
  const t = probe.trace, ok = t.filter((v) => v != null);
  // The window grows from 30 s to TRACE_POINTS s, so a fresh trace isn't a sliver at the right edge.
  const N = Math.min(TRACE_POINTS, Math.max(30, t.length));
  const W = 600, H = 120, max = Math.max(2, ...ok) * 1.15, step = W / (N - 1);
  let path = "", area = "";
  const pts = [];
  t.forEach((v, k) => {
    const x = (N - t.length + k) * step;
    if (v == null) { if (pts.length) { path += pathOf(pts); area += areaOf(pts, H); } pts.length = 0; return; }
    pts.push([x, H - (v / max) * (H - 8)]);
  });
  if (pts.length) { path += pathOf(pts); area += areaOf(pts, H); }
  const lost = t.length - ok.length;
  const losses = t.map((v, k) => (v == null ? `<line class="tlost" x1="${(N - t.length + k) * step}" x2="${(N - t.length + k) * step}" y1="0" y2="${H}"></line>` : "")).join("");
  const last = t[t.length - 1];
  el.innerHTML = `<div class="uahead"><b>Live trace</b><span class="small">${last == null ? `<span class="errtext">no answer</span>` : `<b>${pms(last)}</b>`}
      &middot; avg ${pms(ok.length ? ok.reduce((a, b) => a + b, 0) / ok.length : null)} &middot; lost ${lost} of ${t.length}</span></div>
    <svg class="ptrace" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Live connect times">
      <defs><linearGradient id="gmTraceGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--accent);stop-opacity:.35"></stop>
        <stop offset="1" style="stop-color:var(--accent);stop-opacity:0"></stop></linearGradient></defs>
      ${losses}<path class="tarea" d="${area}"></path><path class="tline" d="${path}"></path></svg>
    <div class="small muted">TCP ${probe.tracePort}, one connect a second, last ${N} s. Red lines: no answer.</div>`;
}
const pathOf = (pts) => pts.map(([x, y], k) => `${k ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
const areaOf = (pts, H) => `M${pts[0][0].toFixed(1)},${H}` + pts.map(([x, y]) => `L${x.toFixed(1)},${y.toFixed(1)}`).join("") + `L${pts[pts.length - 1][0].toFixed(1)},${H}Z`;
