"use strict";
// Ghost Map UI - plain JS, no external dependencies (works offline on OT networks).

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SEV_RANK = { error: 0, warning: 1, info: 2 };

const state = { scans: [], scan: null, findingsByTarget: {}, devSort: { key: "ip", dir: 1 }, selDevice: null, selPort: null };

// Paths are relative (no leading "/") so the UI also works under the IXON HTTP proxy's path prefix.
// X-Ghostmap marks requests as coming from this page; the server rejects state changes without it.
// opts.timeout (ms, default 120 s): give up on a request that hangs, so pages show "no data" instead of
// old values when the server or the network stops answering.
async function api(path, opts = {}) {
  const { timeout = 120000, ...rest } = opts;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  let res;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json", "X-Ghostmap": "1" }, signal: ctl.signal, ...rest });
  } catch (e) {
    throw new Error(e.name === "AbortError" ? `no answer from Ghost Map after ${Math.round(timeout / 1000)} s` : `can't reach Ghost Map (${e.message})`);
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401) { location.href = "login"; throw new Error("login required"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* ignore */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

// Uncaught errors on any page go to the server's debug log (Debug log button, or the debug bundle).
function reportError(message, source, line, stack) {
  const page = document.querySelector("#tabs button.active")?.dataset.tab || "";
  fetch("api/clientlog", { method: "POST", headers: { "Content-Type": "application/json", "X-Ghostmap": "1" },
    body: JSON.stringify({ message: String(message).slice(0, 2000), source: String(source || "").slice(0, 300),
      line: Number(line) || 0, stack: String(stack || "").slice(0, 4000), page }) }).catch(() => { /* ignore */ });
}
window.addEventListener("error", (e) => reportError(e.message, e.filename, e.lineno, e.error?.stack));
window.addEventListener("unhandledrejection", (e) => reportError(`unhandled promise rejection: ${e.reason?.message || e.reason}`, "", 0, e.reason?.stack));

// ------------------------------------------------------------------ helpers
const ipKey = (ip) => (ip ? ip.split(".").map((p) => p.padStart(3, "0")).join(".") : "~");
const devLabel = (d) => (d.identity ? `${d.identity.product_name} @ ${d.ip}` : d.ip || d.mac || "?");
const portLabel = (sw, p) => `${sw.sys_name || sw.ip} ${p.name}`;
const worst = (fs) => (fs || []).reduce((w, f) => (w === null || SEV_RANK[f.severity] < SEV_RANK[w] ? f.severity : w), null);
const sevClass = (s) => (s === "error" ? "err" : s === "warning" ? "warn" : "ok");
function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : `${h}h ${m}m`;
}
function findingHtml(f) {
  return `<div class="finding"><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span>
    <div><span class="t">${esc(f.target)}</span> &mdash; ${esc(f.message)}</div>
    ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ""}</div>`;
}

// ------------------------------------------------------------------ tabs
// Pages are grouped by data source (Machine: OPC UA, Production: SQL, Network: TCP/IP); the top row picks the
// group, the second row the page within it. Each group remembers its last page.
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));
document.querySelectorAll("#groups button").forEach((b) =>
  b.addEventListener("click", () => showGroup(b.dataset.group)));
const SCANLESS_TABS = ["opcua", "machine", "production", "tscconn"];  // tabs that work without any scan loaded
const isViewer = () => document.body.classList.contains("viewer");
const tabGroup = (name) => document.querySelector(`#tabs button[data-tab="${name}"]`)?.closest(".tabgroup")?.dataset.group;
function showGroup(group) {
  let name = null;
  try { name = localStorage.getItem(`gm.tab.${group}`); } catch (_) { /* ignore */ }
  const usable = (b) => b && !(b.classList.contains("admin-only") && isViewer());
  if (!usable(document.querySelector(`#tabs button[data-tab="${name}"]`)) || tabGroup(name) !== group) {
    name = [...document.querySelectorAll(`#tabs .tabgroup[data-group="${group}"] button`)].find(usable)?.dataset.tab;
  }
  showTab(name || "machine");
}
function showTab(name) {
  // A tab remembered from an admin login is hidden for a viewer: fall back to the first visible one.
  const btn = document.querySelector(`#tabs button[data-tab="${name}"]`);
  if (!btn || (btn.classList.contains("admin-only") && isViewer())) name = "machine";
  const group = tabGroup(name);
  const free = SCANLESS_TABS.includes(name);
  document.querySelectorAll("#groups button").forEach((b) => b.classList.toggle("active", b.dataset.group === group));
  document.querySelectorAll("#tabs .tabgroup").forEach((g) => g.classList.toggle("hidden", g.dataset.group !== group));
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}` || (!state.scan && !free)));
  $("#empty").classList.toggle("hidden", !!state.scan || free);
  $("#scanpickScan")?.classList.toggle("hidden", group !== "network" || !state.scan);
  $("#newScanBtn")?.classList.toggle("hidden", group !== "network");
  try { localStorage.setItem("gm.tab", name); localStorage.setItem(`gm.tab.${group}`, name); } catch (_) { /* ignore */ }
  document.dispatchEvent(new CustomEvent("gm:tab", { detail: name }));
}

// ------------------------------------------------------------------ scans
async function refreshScans(selectId) {
  state.scans = await api("api/scans");
  const opts = state.scans.map((s) => {
    const label = s.label ? ` - ${s.label}` : "";
    return `<option value="${esc(s.id)}">${esc(s.id)}${esc(label)} (${s.devices} dev, ${s.errors}E/${s.warnings}W)</option>`;
  }).join("");
  $("#scanSelect").innerHTML = opts;
  $("#cmpOld").innerHTML = opts;
  $("#cmpNew").innerHTML = opts;
  if (state.scans.length > 1) $("#cmpOld").value = state.scans[1].id;
  $("#empty").classList.toggle("hidden", state.scans.length > 0);
  if (!state.scans.length) {
    state.scan = null;
    let tab = "overview";
    try { tab = SCANLESS_TABS.includes(localStorage.getItem("gm.tab")) ? localStorage.getItem("gm.tab") : tab; } catch (_) { /* ignore */ }
    showTab(tab);
    return;
  }
  const id = selectId || state.scans[0].id;
  $("#scanSelect").value = id;
  await loadScan(id);
}

async function loadScan(id) {
  state.scan = await api(`api/scans/${encodeURIComponent(id)}`);
  state.selDevice = null;
  state.selPort = null;
  state.findingsByTarget = {};
  for (const f of state.scan.findings) (state.findingsByTarget[f.target] ||= []).push(f);
  $("#csvLink").href = `api/scans/${encodeURIComponent(id)}/inventory.csv`;
  renderOverview();
  renderDevices();
  renderSwitches();
  let tab = "overview";
  try { tab = localStorage.getItem("gm.tab") || tab; } catch (_) { /* ignore */ }
  showTab(tab);
}
$("#scanSelect").addEventListener("change", (e) => loadScan(e.target.value));

// ------------------------------------------------------------------ overview
function renderOverview() {
  const s = state.scan;
  const cip = s.devices.filter((d) => d.identity).length;
  const count = (sev) => s.findings.filter((f) => f.severity === sev).length;
  const ports = s.switches.flatMap((sw) => sw.ports.filter((p) => [6, 117].includes(p.if_type)));
  const tiles = [
    ["Devices", s.devices.length], ["EtherNet/IP", cip], ["Switches", s.switches.length],
    ["Ports up", `${ports.filter((p) => p.oper_status === "up").length}/${ports.length}`],
    ["Errors", count("error"), "err"], ["Warnings", count("warning"), "warn"],
  ];
  $("#tiles").innerHTML = tiles.map(([l, n, c]) => `<div class="tile ${c || ""}"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`).join("");
  $("#findings").innerHTML = s.findings.length ? s.findings.map(findingHtml).join("") : `<p class="muted">No findings. Nice.</p>`;
  $("#scanLog").textContent = JSON.stringify(s.params, null, 2) + "\n\n" + s.log.join("\n");
}

// ------------------------------------------------------------------ devices
const DEV_COLS = [
  ["", null], ["IP", (d) => ipKey(d.ip)], ["MAC", (d) => d.mac || ""], ["Vendor", (d) => d.identity?.vendor_name || d.mac_vendor || ""],
  ["Product", (d) => d.identity?.product_name || ""], ["Type", (d) => d.identity?.device_type_name || ""],
  ["Firmware", (d) => d.identity?.revision || ""], ["Serial", (d) => d.identity?.serial_hex || ""],
  ["State", (d) => d.identity?.state_name || ""], ["Switch port", (d) => `${d.switch_name || d.switch_ip || ""} ${d.switch_port || ""}`],
  ["VLAN", (d) => d.vlan ?? -1], ["Seen via", (d) => d.sources.join("+")],
];

function deviceHealth(d) {
  const w = worst(state.findingsByTarget[devLabel(d)]);
  if (w) return sevClass(w);
  return d.identity ? "ok" : "";
}

function renderDevices() {
  const s = state.scan;
  const q = $("#devFilter").value.trim().toLowerCase();
  const cipOnly = $("#devCipOnly").checked;
  const { key, dir } = state.devSort;
  const col = DEV_COLS.find((c) => c[0] === key) || DEV_COLS[1];
  const rows = s.devices
    .filter((d) => (!cipOnly || d.identity) && (!q || JSON.stringify(d).toLowerCase().includes(q)))
    .sort((a, b) => { const x = col[1](a), y = col[1](b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
  const head = `<thead><tr>${DEV_COLS.map(([n]) => `<th data-k="${esc(n)}">${esc(n)}${n === key ? (dir > 0 ? " &#9650;" : " &#9660;") : ""}</th>`).join("")}</tr></thead>`;
  const body = rows.map((d) => {
    const i = d.identity;
    return `<tr data-key="${esc(d.key)}" class="${state.selDevice === d.key ? "sel" : ""}">
      <td><span class="dot ${deviceHealth(d)}"></span></td>
      <td class="mono">${esc(d.ip || "-")}</td><td class="mono">${esc(d.mac || "")}</td>
      <td>${i ? esc(i.vendor_name) : d.mac_vendor ? `<span class="muted" title="From the MAC address - no EtherNet/IP reply">${esc(d.mac_vendor)}</span>` : ""}</td>
      <td>${esc(i?.product_name || (d.ip ? "(no EtherNet/IP reply)" : "(unknown MAC)"))}${d.is_scanner ? ` <span class="chip" title="The computer running Ghost Map">this computer</span>` : ""}</td>
      <td>${esc(i?.device_type_name || "")}</td><td class="mono">${esc(i?.revision || "")}</td>
      <td class="mono">${esc(i?.serial_hex || "")}</td><td>${esc(i?.state_name || "")}</td>
      <td>${esc(d.switch_name || d.switch_ip || "")} <span class="mono">${esc(d.switch_port || "")}</span></td>
      <td>${esc(d.vlan ?? "")}</td><td>${d.sources.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</td></tr>`;
  }).join("");
  $("#devTable").innerHTML = head + `<tbody>${body}</tbody>`;
  $("#devTable").querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.k; if (!k) return;
    state.devSort = { key: k, dir: state.devSort.key === k ? -state.devSort.dir : 1 };
    renderDevices();
  }));
  $("#devTable").querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => selectDevice(tr.dataset.key)));
  renderDeviceDetail();
}
$("#devFilter").addEventListener("input", renderDevices);
$("#devCipOnly").addEventListener("change", renderDevices);

function selectDevice(key) {
  state.selDevice = state.selDevice === key ? null : key;
  renderDevices();
}

function renderDeviceDetail() {
  const pane = $("#devDetail");
  const d = state.scan.devices.find((x) => x.key === state.selDevice);
  pane.classList.toggle("hidden", !d);
  pane.parentElement.classList.toggle("open", !!d);
  if (!d) return;
  const i = d.identity;
  const rows = [["IP", d.ip], ["MAC", d.mac], ["MAC vendor", d.mac_vendor || "-"], ["Switch", d.switch_name || d.switch_ip], ["Port", d.switch_port], ["VLAN", d.vlan],
    ["Seen via", d.sources.join(", ")]];
  if (i) rows.push(["Vendor", `${i.vendor_name} (${i.vendor_id})`], ["Device type", `${i.device_type_name} (0x${i.device_type.toString(16).toUpperCase().padStart(2, "0")})`],
    ["Product code", i.product_code], ["Firmware", i.revision], ["Serial", i.serial_hex], ["State", `${i.state_name} (${i.state})`],
    ["Status word", `0x${i.status.toString(16).toUpperCase().padStart(4, "0")}`], ["Ext. status", i.extended_status],
    ["Flags", i.status_flags.join(", ") || "-"], ["Reported IP", `${i.socket_ip}:${i.socket_port}`]);
  const fs = state.findingsByTarget[devLabel(d)] || [];
  pane.innerHTML = `<h3>${esc(i ? i.product_name : d.ip || d.mac)}</h3>
    <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(v ?? "-")}</dd>`).join("")}</dl>
    ${fs.length ? `<h3 style="margin-top:14px">Findings</h3>${fs.map(findingHtml).join("")}` : ""}
    ${d.ip ? `<p><button class="btn admin-only" id="detailProbe">Probe now</button> ${i ? `<a class="btn ghost" href="http://${esc(d.ip)}/" target="_blank" rel="noopener">Web page</a>` : ""}</p>` : ""}`;
  const pb = $("#detailProbe");
  if (pb) pb.addEventListener("click", () => { $("#probeIp").value = d.ip; showTab("probe"); runProbe(); });
}

// ------------------------------------------------------------------ switches
function portHealth(sw, p) {
  const w = worst(state.findingsByTarget[portLabel(sw, p)]);
  if (w === "error" || w === "warning") return sevClass(w);
  return p.oper_status === "up" ? "up" : "";
}

function renderSwitches() {
  const s = state.scan;
  if (!s.switches.length) {
    $("#switches").innerHTML = `<div class="panel muted">No switches in this scan. Add switch IPs and SNMP credentials to a scan
      (Stratix switches that answer ListIdentity are added automatically when SNMP credentials are given).</div>`;
    return;
  }
  const devByPort = {};
  for (const d of s.devices) if (d.switch_port) (devByPort[`${d.switch_ip}|${d.switch_port}`] ||= []).push(d);

  $("#switches").innerHTML = s.switches.map((sw, si) => {
    const phys = sw.ports.filter((p) => [6, 117].includes(p.if_type));
    const plate = phys.map((p) => `<div class="port ${portHealth(sw, p)} ${p.is_uplink ? "uplink" : ""}" data-sw="${si}" data-if="${p.if_index}"
        title="${esc(p.name)} ${esc(p.alias)}"><span class="pn">${esc(p.name)}</span>
        <span class="pm">${p.oper_status === "up" ? `${p.speed_mbps}${p.duplex === "half" ? "H" : ""}` : "down"}</span></div>`).join("");
    const rows = phys.map((p) => {
      const devs = devByPort[`${sw.ip}|${p.name}`] || [];
      return `<tr data-sw="${si}" data-if="${p.if_index}"><td><span class="dot ${portHealth(sw, p) === "up" ? "ok" : portHealth(sw, p)}"></span></td>
        <td class="mono">${esc(p.name)}</td><td>${esc(p.alias)}</td><td>${esc(p.oper_status)}</td>
        <td>${p.oper_status === "up" && p.speed_mbps ? esc(p.speed_mbps + "M") : ""}</td><td>${p.oper_status === "up" ? esc(p.duplex) : ""}</td><td>${esc(p.vlan ?? "")}</td>
        <td>${p.macs.length}</td><td>${p.fcs_errors + p.alignment_errors}</td><td>${p.in_errors}</td>
        <td>${p.is_uplink ? "uplink " : ""}${p.neighbors.map((n) => esc(n.remote_name)).join(", ")}</td>
        <td>${devs.map((d) => esc(d.identity ? d.identity.product_name : d.ip || d.mac)).join(", ")}</td></tr>`;
    }).join("");
    const errs = sw.errors.length ? `<p class="small" style="color:var(--warn)">Partial data: ${sw.errors.map(esc).join("; ")}</p>` : "";
    return `<div class="panel">
      <div class="switch-head"><div><h2>${esc(sw.sys_name || sw.ip)} <span class="muted mono">${esc(sw.ip)}</span></h2>
        <div class="switch-meta">${esc(sw.model)} &middot; IOS ${esc(sw.software_version)} &middot; HW ${esc(sw.hardware_revision || "-")}
        &middot; S/N ${esc(sw.serial || "-")} &middot; up ${fmtUptime(sw.uptime_seconds)} &middot; ${esc(sw.location)}</div></div></div>
      ${errs}
      <div class="faceplate">${plate}</div>
      <div id="portDetail-${si}"></div>
      <div class="tablewrap"><table><thead><tr><th></th><th>Port</th><th>Description</th><th>Link</th><th>Speed</th><th>Duplex</th>
        <th>VLAN</th><th>MACs</th><th>CRC/Align</th><th>In err</th><th>Neighbor</th><th>Devices</th></tr></thead>
        <tbody>${rows}</tbody></table></div></div>`;
  }).join("");
  document.querySelectorAll("#switches [data-if]").forEach((el) => el.addEventListener("click", () => selectPort(+el.dataset.sw, +el.dataset.if)));
}

function selectPort(si, ifIndex) {
  const sw = state.scan.switches[si];
  const p = sw.ports.find((x) => x.if_index === ifIndex);
  const box = $(`#portDetail-${si}`);
  document.querySelectorAll(".port.sel").forEach((x) => x.classList.remove("sel"));
  if (state.selPort === `${si}|${ifIndex}`) { state.selPort = null; box.innerHTML = ""; return; }
  state.selPort = `${si}|${ifIndex}`;
  document.querySelector(`.port[data-sw="${si}"][data-if="${ifIndex}"]`)?.classList.add("sel");
  const devByMac = Object.fromEntries(state.scan.devices.filter((d) => d.mac).map((d) => [d.mac, d]));
  const fs = state.findingsByTarget[portLabel(sw, p)] || [];
  box.innerHTML = `<div class="panel detail" style="background:var(--panel-2)">
    <h3>${esc(p.name)} ${esc(p.alias)} <span class="muted small">ifIndex ${p.if_index}</span></h3>
    <dl><dt>Status</dt><dd>${esc(p.admin_status)} / ${esc(p.oper_status)}</dd>
      <dt>Speed/duplex</dt><dd>${esc(p.speed_mbps)} Mbps ${esc(p.duplex)}</dd>
      <dt>Counters</dt><dd class="mono">in_err ${p.in_errors} out_err ${p.out_errors} fcs ${p.fcs_errors} align ${p.alignment_errors}
        late_coll ${p.late_collisions} in_disc ${p.in_discards} out_disc ${p.out_discards}</dd>
      <dt>Neighbors</dt><dd>${p.neighbors.map((n) => `${esc(n.protocol.toUpperCase())}: ${esc(n.remote_name)} ${esc(n.remote_port)} ${esc(n.remote_platform)} ${esc(n.remote_address)}`).join("<br>") || "-"}</dd>
      <dt>MACs (${p.macs.length})</dt><dd class="mono">${p.macs.slice(0, 64).map((m) => {
        const d = devByMac[m];
        return esc(m) + (d ? ` &rarr; ${esc(d.identity ? d.identity.product_name : "")} ${esc(d.ip || "")}` : "");
      }).join("<br>") || "-"}${p.macs.length > 64 ? "<br>..." : ""}</dd></dl>
    ${fs.map(findingHtml).join("")}</div>`;
}

// ------------------------------------------------------------------ compare
$("#cmpRun").addEventListener("click", async () => {
  const out = $("#cmpOut");
  try {
    const d = await api(`api/diff?old=${encodeURIComponent($("#cmpOld").value)}&new=${encodeURIComponent($("#cmpNew").value)}`);
    const brief = (x) => `${esc(x.label)}${x.mac && x.mac !== x.label ? ` <span class="mono muted">${esc(x.mac)}</span>` : ""}`;
    const sec = (title, items, fn) => `<div class="diffsec"><h3>${esc(title)} (${items.length})</h3>${items.length ? `<ul>${items.map((x) => `<li>${fn(x)}</li>`).join("")}</ul>` : `<p class="muted">none</p>`}</div>`;
    out.classList.remove("muted");
    out.innerHTML =
      sec("Added", d.added, brief) +
      sec("Removed", d.removed, brief) +
      sec("Replaced hardware", d.replaced, (x) => `${esc(x.label)}: serial ${esc(x.old_serial)} &rarr; ${esc(x.new_serial)}, firmware ${esc(x.old_firmware)} &rarr; ${esc(x.new_firmware)}`) +
      sec("Changed", d.changed, (x) => `${esc(x.label)}: ${Object.entries(x.changes).map(([k, v]) => `<b>${esc(k)}</b> ${esc(v.old)} &rarr; ${esc(v.new)}`).join("; ")}`) +
      sec("New findings", d.new_findings, (f) => `<span class="sev ${esc(f.severity)}">${esc(f.severity)}</span> ${esc(f.target)}: ${esc(f.message)}`) +
      sec("Resolved findings", d.resolved_findings, (f) => `${esc(f.target)}: ${esc(f.message)}`);
  } catch (e) {
    out.textContent = `Error: ${e.message}`;
  }
});

// ------------------------------------------------------------------ probe
async function runProbe() {
  const ip = $("#probeIp").value.trim();
  const out = $("#probeOut");
  if (!ip) return;
  out.innerHTML = `<p class="muted">Probing ${esc(ip)}...</p>`;
  try {
    const r = await api(`api/probe/${encodeURIComponent(ip)}`);
    const tcp = Object.entries(r.tcp).map(([p, ok]) => `<span class="chip" style="${ok ? "border-color:var(--ok)" : ""}">${esc(p)} ${ok ? "open" : "-"}</span>`).join("");
    const i = r.identity;
    out.innerHTML = `<p>TCP: ${tcp}</p>` + (i ? `<div class="detail"><dl>
      <dt>Vendor</dt><dd>${esc(i.vendor_name)} (${i.vendor_id})</dd><dt>Type</dt><dd>${esc(i.device_type_name)}</dd>
      <dt>Product</dt><dd>${esc(i.product_name)} (code ${i.product_code})</dd><dt>Firmware</dt><dd class="mono">${esc(i.revision)}</dd>
      <dt>Serial</dt><dd class="mono">${esc(i.serial_hex)}</dd><dt>State</dt><dd>${esc(i.state_name)}</dd>
      <dt>Status</dt><dd>${esc(i.extended_status)} &middot; ${esc(i.status_flags.join(", ") || "no flags")}</dd>
      <dt>Reported IP</dt><dd class="mono">${esc(i.socket_ip)}</dd></dl></div>`
      : `<p class="muted">No EtherNet/IP ListIdentity reply - not a CIP device, filtered by a firewall, or wrong subnet.</p>`);
  } catch (e) {
    out.textContent = `Error: ${e.message}`;
  }
}
$("#probeBtn").addEventListener("click", runProbe);
$("#probeIp").addEventListener("keydown", (e) => { if (e.key === "Enter") runProbe(); });

// ------------------------------------------------------------------ new scan
const dlg = $("#scanDialog");
const form = $("#scanForm");
const split = (v) => v.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
function openScan() { $("#jobLog").classList.add("hidden"); $("#scanGo").disabled = false; dlg.showModal(); }
$("#newScanBtn").addEventListener("click", openScan);
$("#emptyScan").addEventListener("click", openScan);
$("#scanCancel").addEventListener("click", () => dlg.close());
form.snmp_version.addEventListener("change", () => {
  const v3 = form.snmp_version.value === "3";
  form.querySelectorAll(".v3").forEach((el) => el.classList.toggle("hidden", !v3));
  form.querySelectorAll(".v2").forEach((el) => el.classList.toggle("hidden", v3));
});
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form;
  const body = {
    label: f.label.value, targets: split(f.targets.value), broadcast: split(f.broadcast.value), switches: split(f.switches.value),
    auto_switches: f.auto_switches.checked, rate: +f.rate.value, timeout: +f.timeout.value,
    snmp: { version: f.snmp_version.value, community: f.community.value, username: f.username.value,
      auth_protocol: f.auth_protocol.value, auth_key: f.auth_key.value, priv_protocol: f.priv_protocol.value, priv_key: f.priv_key.value },
  };
  const log = $("#jobLog");
  log.classList.remove("hidden");
  log.textContent = "Starting...";
  $("#scanGo").disabled = true;
  try {
    const { job } = await api("api/scans", { method: "POST", body: JSON.stringify(body) });
    for (;;) {
      await new Promise((r) => setTimeout(r, 700));
      const j = await api(`api/jobs/${job}`);
      log.textContent = j.log.join("\n");
      log.scrollTop = log.scrollHeight;
      if (j.status === "done") { f.community.value = f.auth_key.value = f.priv_key.value = ""; await refreshScans(j.scan_id); dlg.close(); break; }
      if (j.status === "failed") { log.textContent += `\nFAILED: ${j.error}`; break; }
    }
  } catch (err) {
    log.textContent += `\nError: ${err.message}`;
  } finally {
    $("#scanGo").disabled = false;
  }
});

// ------------------------------------------------------------------ login setup & users
const setupDlg = $("#setupDialog");
$("#setupBtn").addEventListener("click", () => { $("#setupErr").textContent = ""; setupDlg.showModal(); });
$("#setupCancel").addEventListener("click", () => setupDlg.close());
$("#setupForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  if (f.password.value !== f.password2.value) { $("#setupErr").textContent = "Passwords don't match."; return; }
  try {
    await api("api/users/setup", { method: "POST", body: JSON.stringify({ name: f.name.value, password: f.password.value }) });
    location.reload();
  } catch (e) { $("#setupErr").textContent = e.message; }
});

const usersDlg = $("#usersDialog");
async function renderUsers() {
  const list = await api("api/users");
  $("#usersTable").innerHTML = `<tr><th>User</th><th>Role</th><th></th></tr>` + list.map((u) =>
    `<tr><td>${esc(u.name)}</td><td>${esc(u.role)}</td><td><button type="button" class="btn ghost small" data-rm="${esc(u.name)}">Remove</button></td></tr>`).join("");
  $("#usersTable").querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Remove ${b.dataset.rm}?`)) return;
    try { await api(`api/users/${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" }); await renderUsers(); } catch (e) { $("#usersErr").textContent = e.message; }
  }));
}
$("#usersBtn").addEventListener("click", async () => { $("#usersErr").textContent = ""; await renderUsers(); usersDlg.showModal(); });
$("#usersClose").addEventListener("click", () => usersDlg.close());
async function showDebugLog() {
  const box = $("#debugText");
  box.textContent = "Loading...";
  try {
    const res = await fetch("api/debug/log?lines=500", { headers: { "X-Ghostmap": "1" } });
    box.textContent = res.ok ? (await res.text()) || "The log is empty." : `Could not load the log (${res.status}).`;
  } catch (e) { box.textContent = e.message; }
  box.scrollTop = box.scrollHeight;
}
$("#debugBtn").addEventListener("click", () => { $("#debugDialog").showModal(); showDebugLog(); });
$("#debugRefresh").addEventListener("click", showDebugLog);
$("#debugClose").addEventListener("click", () => $("#debugDialog").close());
$("#usersForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try {
    await api("api/users", { method: "POST", body: JSON.stringify({ name: f.name.value, role: f.role.value, password: f.password.value }) });
    f.name.value = f.password.value = "";
    $("#usersErr").textContent = "";
    await renderUsers();
  } catch (e) { $("#usersErr").textContent = e.message; }
});

$("#logoutBtn").addEventListener("click", async () => { await api("api/logout", { method: "POST" }); location.href = "login"; });
$("#loadDemo").addEventListener("click", async () => { await api("api/demo", { method: "POST" }); await refreshScans("demo-today"); });

// ------------------------------------------------------------------ boot
(async () => {
  try {
    const info = await api("api/info");
    const b = info.build || {};
    $("#ver").textContent = "v" + info.version + (b.commit ? ` (${b.commit.slice(0, 7)})` : "");
    $("#ver").title = b.commit ? `Build ${b.run ? "#" + b.run + " " : ""}${b.commit} ${b.branch || ""}, ${b.built || ""}` : "Run from source";
    state.info = info;
    document.body.classList.toggle("viewer", info.role !== "admin");
    if (info.user) {
      $("#whoName").textContent = `${info.user} (${info.role})`;
      $("#who").classList.remove("hidden");
    } else if (!info.auth) {
      $("#unsecured").classList.remove("hidden");
      $("#setupBtn").classList.toggle("hidden", !info.local);
    }
  } catch (_) { /* ignore */ }
  await refreshScans();
})();
