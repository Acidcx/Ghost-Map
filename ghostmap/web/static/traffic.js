"use strict";
// Network > Traffic: the background traffic monitor (switch port counters over read-only SNMP).
// Bandwidth, broadcast and multicast rates per port now, peaks, charts over time, and storm / congestion events.
// Uses $, esc and api from app.js.

const NET_POLL_MS = 10000;
const net = { status: null, sw: null, port: null, timer: null, now: null };

const nMbps = (bps) => (bps == null ? "-" : bps < 1e5 ? `${(bps / 1e6).toFixed(2)}` : bps < 1e8 ? (bps / 1e6).toFixed(1) : (bps / 1e6).toFixed(0));
const nRate = (v) => (v == null ? "-" : v >= 100 ? Math.round(v).toLocaleString() : v >= 1 ? v.toFixed(1) : v > 0 ? v.toFixed(2) : "0");
const nTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "-");
const nDate = (t) => (t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "-");
const nDur = (s) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
const N_TITLES = {
  "port.broadcast_storm": "Broadcast storm", "port.utilization": "Link busy", "port.multicast_high": "High multicast",
  "port.multicast_flood": "Multicast flooding", "port.errors_rising": "Errors climbing", "port.drops": "Output drops",
  "switch.cpu_high": "Switch CPU high",
};

// ------------------------------------------------------------------ polling
async function netPoll() {
  clearTimeout(net.timer);
  if (document.hidden || $("#tab-traffic").classList.contains("hidden")) return;
  try {
    net.status = await api("api/netmon/status", { timeout: 20000 });
    netSwitchList();
    if (net.sw) {
      const hours = Number($("#nHours").value);
      const [now, ev] = await Promise.all([
        api(`api/netmon/now?switch=${encodeURIComponent(net.sw)}&hours=24`, { timeout: 20000 }),
        api(`api/netmon/events?hours=${Math.max(hours, 24)}`, { timeout: 20000 }),
      ]);
      net.now = now;
      net.events = ev.events;
    }
    netRender();
  } catch (e) {
    $("#nNote").innerHTML = `<p class="errtext small">${esc(e.message)}</p>`;
  }
  net.timer = setTimeout(netPoll, NET_POLL_MS);
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "traffic") netPoll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) netPoll(); });
$("#nHours").addEventListener("change", () => { net.chartKey = null; netPoll(); });
$("#nSwitch").addEventListener("change", (e) => { net.sw = e.target.value; net.port = null; net.chartKey = null; netPoll(); });

function netSwitchList() {
  const sws = net.status?.switches || [];
  const opts = sws.map((s) => `<option value="${esc(s.switch)}">${esc(s.name ? `${s.name} (${s.switch})` : s.switch)}</option>`).join("");
  if ($("#nSwitch").innerHTML !== opts) $("#nSwitch").innerHTML = opts;
  if (!sws.some((s) => s.switch === net.sw)) net.sw = sws[0]?.switch || null;
  if (net.sw) $("#nSwitch").value = net.sw;
  // With several switches the wiring-order list on the page picks one; the drop-down is only for a long list.
  $("#nSwitch").closest("label").classList.toggle("hidden", sws.length < 2 || sws.length <= 8);
}

// Description cell: a link to another switch says which switch and port it goes to (a button when that switch is
// monitored); a port with several devices behind it (a daisy chain of drives, a ring, an unmanaged switch) says how
// many and names them.
function netDescHtml(p) {
  let h = esc(p.alias);
  if (p.uplink) {
    const ip = netLinkTarget(p.link_to);
    h += ` <span class="chip">switch link</span>`;
    if (p.link_to) h += ip && ip !== net.sw
      ? ` <a href="#" class="small" data-goto="${esc(ip)}">to ${esc(p.link_to)}</a>`
      : ` <span class="small muted">to ${esc(p.link_to)}</span>`;
  } else if (p.chain) {
    // Named from the newest scan when it found them; otherwise by maker, and the same names counted once.
    const devs = (p.devices || []).map((d) => ({ ...d, label: d.name || d.vendor || "unknown device" }));
    const counts = new Map();
    for (const d of devs) counts.set(d.label, (counts.get(d.label) || 0) + 1);
    const shown = [...counts].map(([l, n]) => (n > 1 ? `${l} \u00d7${n}` : l));
    const rest = p.macs - devs.length;
    h += ` <span class="chip" title="${esc(devs.map((d) => `${d.label}  ${d.mac}`).join("\n"))}">${esc(p.macs)} devices</span>
      <div class="small muted nchainlist">${esc(shown.slice(0, 4).join(", "))}${shown.length > 4 || rest > 0 ? ", and more" : ""}</div>`;
  }
  return h;
}

// ------------------------------------------------------------------ page
function netRender() {
  const s = net.status;
  if (!s.configured) {
    $("#nLive").textContent = "";
    $("#nNote").innerHTML = s.load_error ? `<p class="errtext small">The saved settings could not be loaded: ${esc(s.load_error)}</p>` : "";
    $("#nBody").innerHTML = `<div class="panel"><h3>Not set up yet</h3><p class="small muted">${isViewer()
      ? "An admin can set up the traffic monitor with the machine's switch IPs and a read-only SNMP login."
      : "Click <b>Settings</b> and enter the machine's switch IPs and a read-only SNMP community (or v3 user). Type <b>demo, demo2</b> to try two simulated, daisy-chained switches."}</p></div>`;
    if (!isViewer()) netShowSettings(true);
    return;
  }
  const sw = s.switches.find((x) => x.switch === net.sw) || {};
  const fresh = sw.last_ok && Date.now() / 1000 - sw.last_ok < (s.interval_s || 30) * 3;
  $("#nLive").innerHTML = `${fresh ? `<span class="chip okchip">live</span>` : `<span class="chip warnchip">no recent read</span>`}
    every ${esc(s.interval_s)} s &middot; last read ${esc(nTime(sw.last_ok))}${sw.cpu ? ` &middot; switch CPU ${esc(sw.cpu.cpu_5s)}% (1 min ${esc(sw.cpu.cpu_1m)}%)` : ""}`;
  $("#nNote").innerHTML = sw.error ? `<p class="errtext small">Last poll failed (${esc(nTime(sw.error_at))}): ${esc(sw.error)}</p>` : "";
  const ports = net.now?.ports || [];
  if (!ports.length) {
    $("#nBody").innerHTML = `${netChainHtml()}<div class="panel"><p class="muted small">${sw.error ? "No readings from this switch yet." :
      "Waiting for the second reading: rates need two readings one poll interval apart."}</p></div>${netEventsHtml()}`;
    netChainWire();
    return;
  }
  const keep = $("#nChart");
  $("#nBody").innerHTML = `${netChainHtml()}${netPortsHtml(ports)}<div id="nChartSlot"></div>${netEventsHtml()}`;
  if (keep && net.port) $("#nChartSlot").replaceWith(keep);
  else $("#nChartSlot").outerHTML = `<div id="nChart"></div>`;
  document.querySelectorAll("#nPorts tbody tr").forEach((tr) => tr.addEventListener("click", () => {
    net.port = net.port === tr.dataset.port ? null : tr.dataset.port;
    net.chartKey = null;
    netRender();
  }));
  document.querySelectorAll("#nPorts [data-goto]").forEach((a) => a.addEventListener("click", (ev) => {
    ev.stopPropagation();
    netPick(a.dataset.goto);
  }));
  netChainWire();
  netChart();
}

function netPick(ip) {
  net.sw = ip; net.port = null; net.chartKey = null;
  netPoll();
}

// Switch name (as LLDP/CDP and the links report it) -> its IP in the settings, for switches being monitored.
function netByName() {
  const m = {};
  for (const s of net.status?.switches || []) if (s.name) m[s.name.split(".")[0].toLowerCase()] = s.switch;
  return m;
}
const netLinkTarget = (to) => netByName()[(to || "").split(" ")[0].split(".")[0].toLowerCase()];

// The monitored switches in wiring order: each one indented under the switch port it hangs off, so a machine with
// daisy-chained switches reads top to bottom. Links to switches that aren't monitored (the plant network) are named.
function netChainHtml() {
  const sws = net.status.switches;
  if (sws.length < 2) return "";
  const t = Date.now() / 1000, every = net.status.interval_s || 30;
  const rows = sws.map((s) => {
    const fresh = s.last_ok && t - s.last_ok < every * 3;
    const dot = s.error || !fresh ? "err" : s.open_events ? "warn" : "ok";
    const other = (s.links || []).filter((l) => !netLinkTarget(l.to));
    return `<button type="button" class="nsw ${s.switch === net.sw ? "sel" : ""}" data-sw="${esc(s.switch)}" style="margin-left:${Math.min(s.depth, 6) * 26}px">
      ${s.depth ? `<span class="nbranch">&#x2514;</span>` : ""}<span class="dot ${dot}"></span>
      <b>${esc(s.name || s.switch)}</b> <span class="small muted mono">${esc(s.switch)}</span>
      ${s.via ? `<span class="small muted">on ${esc(s.via)}</span>` : ""}
      ${other.map((l) => `<span class="small muted">&middot; ${esc(l.port)} to ${esc(l.to)}</span>`).join(" ")}
      ${s.open_events ? `<span class="chip warnchip">${esc(s.open_events)} open</span>` : ""}
      ${s.cpu ? `<span class="small muted">CPU ${esc(s.cpu.cpu_5s)}%</span>` : ""}</button>`;
  }).join("");
  return `<div class="panel"><h3>Switches in wiring order <span class="small muted">(click one for its ports)</span></h3>
    <div class="nchain">${rows}</div></div>`;
}
function netChainWire() {
  document.querySelectorAll(".nchain [data-sw]").forEach((b) => b.addEventListener("click", () => netPick(b.dataset.sw)));
}

function netPortsHtml(ports) {
  const t = net.status.thresholds, peaks = net.now.peaks || {};
  const open = {};
  for (const o of net.now.open || []) (open[o.port] ||= []).push(o.code);
  const cls = (v, warn, err) => (v == null ? "" : err != null && v >= err ? "errtext" : v >= warn ? "warntext" : "");
  const rows = ports.map((p) => {
    const busy = Math.max(p.in_util ?? 0, p.out_util ?? 0), pk = peaks[p.port] || {};
    const codes = open[p.port] || [];
    const dot = codes.length ? (codes.includes("port.broadcast_storm") && (p.in_bcast ?? 0) >= t.bcast_storm_pps ? "err" : "warn")
      : p.oper === "up" ? "ok" : "";
    return `<tr data-port="${esc(p.port)}" class="${net.port === p.port ? "sel" : ""}"><td><span class="dot ${dot}"></span></td>
      <td class="mono">${esc(p.port)}</td><td>${netDescHtml(p)}</td>
      <td>${p.oper === "up" && p.speed_mbps ? esc(p.speed_mbps + "M") : esc(p.oper || "")}</td>
      <td class="num">${nMbps(p.in_bps)}</td><td class="num">${nMbps(p.out_bps)}</td>
      <td><div class="ubar ${busy >= t.util_percent ? "warn" : ""}"><span style="width:${Math.min(100, busy)}%"></span></div>
        <span class="small ${cls(busy, t.util_percent)}">${busy ? busy.toFixed(busy < 10 ? 1 : 0) + "%" : "0%"}</span></td>
      <td class="num ${cls(p.in_bcast, t.bcast_pps, t.bcast_storm_pps)}">${nRate(p.in_bcast)}</td>
      <td class="num ${cls(p.in_mcast, t.mcast_pps)}">${nRate(p.in_mcast)}</td>
      <td class="num ${cls(p.in_err, t.errors_per_s)}">${nRate(p.in_err)}</td>
      <td class="num ${cls(p.out_drop, t.drops_per_s)}">${nRate(p.out_drop)}</td>
      <td class="num ${cls(pk.util, t.util_percent)}">${pk.util != null ? `${pk.util.toFixed(0)}%` : "-"}</td>
      <td class="num ${cls(pk.in_bcast, t.bcast_pps, t.bcast_storm_pps)}">${nRate(pk.in_bcast)}</td></tr>`;
  }).join("");
  return `<div class="panel"><h3>Ports now <span class="small muted">(averaged over the last ${esc(ports[0]?.seconds ?? "")} s; click a port for its charts)</span></h3>
    <div class="tablewrap"><table id="nPorts"><thead><tr><th></th><th>Port</th><th>Description</th><th>Link</th>
    <th class="num">In Mbps</th><th class="num">Out Mbps</th><th>Busy</th><th class="num" title="Broadcast packets per second received from the device on this port">Bcast in /s</th>
    <th class="num" title="Multicast packets per second received from the device on this port">Mcast in /s</th><th class="num">Errors /s</th>
    <th class="num" title="Packets the switch dropped instead of sending on this port">Drops /s</th>
    <th class="num">Peak busy 24 h</th><th class="num">Peak bcast 24 h</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="small muted">"In" is what the port received from the device plugged into it; "out" is what the switch sent to it.
      A broadcast storm shows as high <b>Bcast in</b> on the port it comes from.</p></div>`;
}

// Events from every monitored switch: a storm crosses the links between switches, so the machine is one story.
// A storm seen on a link at the same time as on an ordinary port is shown greyed, pointing at where it comes from.
function netEventsHtml() {
  const many = (net.status?.switches || []).length > 1;
  const all = (net.events || []).filter((e) => many || !net.sw || e.switch === net.sw);
  const evs = all.slice(0, 25);
  const now = Date.now() / 1000;
  const rows = evs.map((e) => {
    const end = e.end ?? null, lasted = (end ?? now) - e.start, passing = e.passing_from?.length;
    const msg = passing ? `Passing through this link: the same storm started at ${e.passing_from.join(", ")}.` : e.message;
    return `<tr class="${passing ? "passing" : end ? "" : "sel"}"><td>${esc(nDate(e.start))}</td><td>${esc(nDur(lasted))}${end ? "" : ", ongoing"}</td>
      ${many ? `<td>${esc(e.name || e.switch)}</td>` : ""}
      <td class="mono">${esc(e.port || "switch")}</td><td><span class="sev ${esc(passing ? "info" : e.severity)}">${esc(N_TITLES[e.code] || e.code)}</span></td>
      <td class="num">${e.peak != null ? `${esc(nRate(e.peak))} ${esc(e.unit)}` : ""}</td><td class="wrap" title="${esc(passing ? e.message : e.hint)}">${esc(msg)}</td></tr>`;
  }).join("");
  return `<div class="panel"><div class="toolbar"><h3 style="margin:0">Storms and congestion${many ? ` <span class="small muted">(all switches)</span>` : ""}</h3><span class="grow"></span>
    <a class="btn ghost" href="api/netmon/events.csv?hours=168" download>Download CSV (7 days)</a></div>
    ${evs.length ? `<div class="tablewrap"><table class="ptable"><thead><tr><th>Start</th><th>Lasted</th>${many ? "<th>Switch</th>" : ""}<th>Port</th><th>What</th>
      <th class="num">Peak</th><th>Details</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${all.length > evs.length ? `<p class="small muted">The newest ${evs.length} of ${all.length}; the CSV has them all.</p>` : ""}`
    : `<p class="muted small">Nothing in this period: no broadcast storms, busy links, climbing errors or drops.</p>`}</div>`;
}

// ------------------------------------------------------------------ charts
async function netChart() {
  const box = $("#nChart");
  if (!box) return;
  if (!net.port) { box.innerHTML = ""; return; }
  const hours = Number($("#nHours").value);
  const key = `${net.sw}|${net.port}|${hours}|${Math.floor(Date.now() / 30000)}`;
  if (net.chartKey === key) return;
  net.chartKey = key;
  try {
    const [r, c] = await Promise.all([
      api(`api/netmon/series?switch=${encodeURIComponent(net.sw)}&port=${encodeURIComponent(net.port)}&hours=${hours}`),
      api(`api/netmon/cpu?switch=${encodeURIComponent(net.sw)}&hours=${hours}`),
    ]);
    const t = net.status.thresholds, p = (net.now.ports || []).find((x) => x.port === net.port) || {};
    const bw = r.rates.map((x) => ({ t: x.t, a: x.in_bps / 1e6, b: x.out_bps / 1e6 }));
    const pk = r.rates.map((x) => ({ t: x.t, a: x.in_bcast, b: x.in_mcast }));
    const cpu = c.cpu.map((x) => ({ t: x.t, a: x.cpu_5s, b: x.cpu_1m }));
    $("#nChart").innerHTML = `<div class="panel"><h3>${esc(net.port)} ${esc(p.alias || "")} <span class="small muted">${esc($("#nHours").selectedOptions[0].text)}</span></h3>
      <div class="ncharts">
        <div><div class="small muted">Bandwidth (Mbps) <span class="lg a">in</span> <span class="lg b">out</span></div>
          ${lineChart(bw, r.since, r.until, { limit: p.speed_mbps ? p.speed_mbps * t.util_percent / 100 : null })}</div>
        <div><div class="small muted">Packets/s received, highest in each step <span class="lg a">broadcast</span> <span class="lg b">multicast</span></div>
          ${lineChart(pk, r.since, r.until, { limit: t.bcast_pps })}</div>
        <div><div class="small muted">Switch CPU % <span class="lg a">5 s</span> <span class="lg b">1 min</span></div>
          ${lineChart(cpu, c.since, c.until, { max: 100 })}</div>
      </div>
      <p class="small muted">A dashed line marks the warning level (${esc(t.util_percent)}% of the link, ${esc(t.bcast_pps)} broadcasts/s) when the readings come near it.</p></div>`;
  } catch (e) { $("#nChart").innerHTML = `<div class="panel errtext small">${esc(e.message)}</div>`; }
}

function lineChart(pts, since, until, { limit = null, max = null } = {}) {
  const W = 520, H = 150, L = 44, B = 18, T = 6;
  if (!pts.length) return `<p class="muted small">No readings in this period.</p>`;
  const peak = Math.max(1e-6, ...pts.flatMap((p) => [p.a ?? 0, p.b ?? 0]));
  // Scale to the data; include the warning line only when the data comes near it.
  const top = max ?? Math.max(peak, limit != null && limit < peak * 2 ? limit : 0) * 1.1;
  const x = (t) => L + ((t - since) / Math.max(1, until - since)) * (W - L - 4);
  const y = (v) => T + (1 - Math.min(v, top) / top) * (H - T - B);
  const step = pts.length > 1 ? Math.min(...pts.slice(1).map((p, i) => p.t - pts[i].t)) * 1.5 : Infinity;
  const path = (k) => {
    let d = "", prev = null;
    for (const p of pts) {
      if (p[k] == null) { prev = null; continue; }
      d += `${prev == null || p.t - prev > step ? "M" : "L"}${x(p.t).toFixed(1)},${y(p[k]).toFixed(1)}`;
      prev = p.t;
    }
    return d;
  };
  const fmt = (v) => (v >= 100 ? Math.round(v).toLocaleString() : v >= 1 ? v.toFixed(1) : v.toFixed(2));
  const ticks = [0, top / 2, top].map((v) => `<text x="${L - 4}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>
    <line class="grid" x1="${L}" x2="${W - 4}" y1="${y(v)}" y2="${y(v)}"></line>`).join("");
  const tl = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const xt = [since, (since + until) / 2, until].map((t, i) => `<text x="${x(t)}" y="${H - 4}" text-anchor="${["start", "middle", "end"][i]}">${tl(t)}</text>`).join("");
  const lim = limit != null && limit < top ? `<line class="limit" x1="${L}" x2="${W - 4}" y1="${y(limit)}" y2="${y(limit)}"></line>` : "";
  // A few readings (just started, or a long range) are drawn as dots so a single point still shows.
  const dots = (k) => (pts.length > 40 ? "" : pts.filter((p) => p[k] != null)
    .map((p) => `<circle class="${k}" cx="${x(p.t).toFixed(1)}" cy="${y(p[k]).toFixed(1)}" r="2.2"></circle>`).join(""));
  return `<svg class="nchart" viewBox="0 0 ${W} ${H}" role="img">${ticks}${xt}${lim}
    <path class="b" d="${path("b")}"></path><path class="a" d="${path("a")}"></path>${dots("b")}${dots("a")}</svg>`;
}

// ------------------------------------------------------------------ settings
function netShowSettings(show) {
  $("#nSettings").classList.toggle("hidden", !show);
  if (show) netLoadSettings();
}
$("#nSetBtn").addEventListener("click", () => netShowSettings($("#nSettings").classList.contains("hidden")));

function netForm() {
  const f = $("#nForm");
  return { switches: f.switches.value.split(/[\s,;]+/).filter(Boolean), interval_s: Number(f.interval_s.value) || 30,
    version: f.version.value, community: f.community.value, username: f.username.value.trim(),
    auth_protocol: f.auth_protocol.value, auth_key: f.auth_key.value, priv_protocol: f.priv_protocol.value, priv_key: f.priv_key.value };
}
function netVersionToggle() {
  const v3 = $("#nForm").version.value === "3";
  document.querySelectorAll("#nForm .v3").forEach((el) => el.classList.toggle("hidden", !v3));
  document.querySelectorAll("#nForm .v2").forEach((el) => el.classList.toggle("hidden", v3));
}
$("#nForm").version.addEventListener("change", netVersionToggle);

async function netLoadSettings() {
  try {
    const c = await api("api/netmon/config");
    const f = $("#nForm");
    f.switches.value = (c.switches || []).join(", ");
    f.interval_s.value = c.interval_s || 30;
    for (const k of ["version", "username", "auth_protocol", "priv_protocol"]) f[k].value = c[k] ?? f[k].value;
    for (const k of ["community", "auth_key", "priv_key"]) {
      f[k].value = "";
      f[k].placeholder = c[`has_${k}`] ? "saved (leave blank to keep it)" : "";
    }
    $("#nClear").classList.toggle("hidden", !c.configured);
    $("#nErr").textContent = c.load_error ? `The saved settings could not be loaded: ${c.load_error}` : "";
    netVersionToggle();
  } catch (e) { $("#nErr").textContent = e.message; }
}

$("#nTest").addEventListener("click", async () => {
  $("#nErr").textContent = "";
  $("#nTestOut").innerHTML = `<span class="muted">Reading the switches...</span>`;
  try {
    const r = await api("api/netmon/test", { method: "POST", body: JSON.stringify(netForm()), timeout: 90000 });
    $("#nTestOut").innerHTML = r.error ? `<span class="errtext">${esc(r.error)}</span>` : r.switches.map((s) => s.ok
      ? `<div><span class="oktext">${esc(s.switch)}:</span> ${esc(s.name)} ${esc(s.model)}, ${esc(s.ports)} ports</div>`
      : `<div><span class="errtext">${esc(s.switch)}: ${esc(s.error)}</span></div>`).join("");
  } catch (e) { $("#nTestOut").innerHTML = `<span class="errtext">${esc(e.message)}</span>`; }
});

$("#nForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#nErr").textContent = "";
  try {
    await api("api/netmon/config", { method: "POST", body: JSON.stringify(netForm()) });
    $("#nTestOut").innerHTML = `<span class="oktext">Saved. The first rates appear after two polls.</span>`;
    netLoadSettings();
    netPoll();
  } catch (e) { $("#nErr").textContent = e.message; }
});

$("#nClear").addEventListener("click", async () => {
  if (!confirm("Stop the traffic monitor and remove its settings? Recorded rates and events are kept.")) return;
  try {
    await api("api/netmon/config", { method: "DELETE" });
    $("#nTestOut").innerHTML = "";
    netLoadSettings();
    netPoll();
  } catch (e) { $("#nErr").textContent = e.message; }
});
