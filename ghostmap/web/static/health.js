"use strict";
// Machine > Device health: each drive's thermal model and thermal bits, I/O modules, the controller (from the PLC
// through FT Linx, read-only) and every EtherNet/IP device from the last scan, worst first. Uses $, esc, api from app.js.

const dh = { dash: "", data: null, devices: null, timer: null, onlyIssues: false };
const DH_POLL_MS = 10000;  // the server shares one read per machine for 5 s, whoever is looking
const DH_RANK = { error: 0, warning: 1, info: 2, ok: 3, unknown: 4 };
const DH_CAPS = [["MotorCapacity", "Motor"], ["InverterCapacity", "Inverter"], ["ConverterCapacity", "Converter"], ["BusRegulatorCapacity", "Bus reg."]];
const dhVisible = () => !document.hidden && !$("#tab-health").classList.contains("hidden");

async function dhLoadList() {
  const list = await api("api/dashboards");
  let id = dh.dash;
  try { id = id || localStorage.getItem("gm.dash"); } catch (_) { /* ignore */ }
  if (!list.some((d) => d.id === id)) id = list[0]?.id || "";
  dh.dash = id;
  $("#dhDash").innerHTML = list.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
  $("#dhDash").value = id;
  $("#dhDashWrap").classList.toggle("hidden", list.length < 2);
  return list;
}

async function dhPoll() {
  clearTimeout(dh.timer);
  if (!dhVisible()) return;
  try {
    if (!dh.listed) { await dhLoadList(); dh.listed = true; }
    const [data, devices] = await Promise.all([
      dh.dash ? api(`api/dashboards/${encodeURIComponent(dh.dash)}/devhealth`, { timeout: 30000 })
        .catch((e) => ({ ok: false, error: e.message, drives: [], io: [], controllers: [], summary: {} })) : null,
      api("api/devhealth/devices")]);
    dh.data = data;
    dh.devices = devices;
  } catch (e) {
    $("#dhNote").innerHTML = `<p class="errtext small">${esc(e.message)}</p>`;
  }
  dhRender();
  dh.timer = setTimeout(dhPoll, DH_POLL_MS);
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "health") { dh.listed = false; dhPoll(); } });
document.addEventListener("visibilitychange", () => { if (dhVisible()) dhPoll(); });
$("#dhDash").addEventListener("change", () => {
  dh.dash = $("#dhDash").value;
  dh.data = null;
  try { localStorage.setItem("gm.dash", dh.dash); } catch (_) { /* ignore */ }
  dhPoll();
});
$("#dhOnly").addEventListener("change", () => { dh.onlyIssues = $("#dhOnly").checked; dhRender(); });

// ------------------------------------------------------------------ pieces
const dhChip = (level) => `<span class="sev ${level === "ok" ? "ok" : level === "unknown" ? "unk" : esc(level)}">${level === "unknown" ? "no data" : esc(level)}</span>`;
const dhNum = (v, unit = "", digits = 1) => (v === null || v === undefined ? `<span class="muted">-</span>` : `${Number(v).toFixed(digits)}${unit}`);
const dhSort = (rows) => [...rows].sort((a, b) => DH_RANK[a.level] - DH_RANK[b.level]);
const dhShow = (rows) => dhSort(rows).filter((r) => !dh.onlyIssues || r.level === "error" || r.level === "warning");

function dhCap(pct) {
  if (pct === null || pct === undefined) return `<span class="muted">-</span>`;
  const cls = pct >= 100 ? "err" : pct >= 80 ? "warn" : "ok";
  return `<span class="capbar ${cls}" title="${Number(pct).toFixed(1)} % of rated (thermal model)"><span style="width:${Math.max(0, Math.min(100, pct))}%"></span><b>${Math.round(pct)}%</b></span>`;
}

function dhIssues(issues) {
  if (!issues.length) return "";
  return issues.map((i) => `<div class="dhissue"><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span> ${esc(i.text)}
    <span class="small muted mono">${esc(i.code)}</span><div class="hint small muted">${esc(i.hint)}</div></div>`).join("");
}

// A row plus, when it has issues, a second row underneath with each issue and what to do about it.
function dhRow(level, cells, issues, span) {
  return `<tr class="${level === "error" ? "bad" : level === "warning" ? "warnrow" : ""}">${cells}</tr>
    ${issues.length ? `<tr class="dhsub"><td colspan="${span}"><div class="dhwrap">${dhIssues(issues)}</div></td></tr>` : ""}`;
}

function dhTile(n, label, sub, cls) {
  return `<div class="tile ${cls}"><div class="n">${n}</div><div class="l">${esc(label)}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
}

// ------------------------------------------------------------------ render
function dhRender() {
  const D = dh.data, V = dh.devices;
  const S = D?.summary || {};
  const at = D?.at ? new Date(D.at * 1000).toLocaleTimeString() : "";
  $("#dhLive").textContent = D ? (D.ok ? `Read ${D.tags} tags at ${at}` : "") : dh.dash ? "Reading..." : "";
  $("#dhNote").innerHTML = D && !D.ok ? `<p class="errtext small">Couldn't read the PLC through the gateway: ${esc(D.error || "")}</p>` : "";

  const drives = D?.drives || [], io = D?.io || [], ctls = D?.controllers || [], devs = V?.devices || [];
  const devBad = devs.filter((d) => d.level === "error" || d.level === "warning").length;
  const peak = S.peak_capacity;
  const worstDrive = dhSort(drives)[0]?.level;
  $("#dhTiles").innerHTML = [
    dh.dash && dhTile(drives.length ? `${S.drive_issues || 0}/${drives.length}` : "0", "Drives with issues",
      drives.length ? "Kinetix / CIP Motion axes" : "no motion axes in the tag export", S.drive_issues ? (worstDrive === "error" ? "err" : "warn") : drives.length ? "ok" : ""),
    dh.dash && dhTile(peak === null || peak === undefined ? "-" : `${Math.round(peak)}%`, "Highest drive load", "of rated, thermal model",
      peak >= 100 ? "err" : peak >= 80 ? "warn" : peak !== null && peak !== undefined ? "ok" : ""),
    dh.dash && dhTile(S.thermal_on || 0, "Thermal alarms on", "over-temperature / overload bits", S.thermal_on ? "warn" : drives.length ? "ok" : ""),
    dh.dash && dhTile(io.length ? `${S.io_faulted || 0}/${io.length}` : "0", "I/O faulted", "modules and produced/consumed tags", S.io_faulted ? "err" : io.length ? "ok" : ""),
    dhTile(V?.scan ? `${devBad}/${devs.length}` : "-", "Network devices with issues", V?.scan ? `scan ${esc(V.scan.id)}` : "no scan yet",
      devs.some((d) => d.level === "error") ? "err" : devBad ? "warn" : devs.length ? "ok" : ""),
  ].filter(Boolean).join("");

  const parts = [];
  if (!dh.dash) {
    parts.push(`<div class="panel"><h3>Drives, I/O and controller</h3><p class="small muted">No machine dashboard yet. Build one from the PLC's tags
      (System > OPC UA tags, or Machine > Dashboard > From CSV); this page then reads its drives, I/O modules and controller.</p></div>`);
  } else {
    const dRows = dhShow(drives).map((d) => dhRow(d.level, `<td>${dhChip(d.level)}</td>
        <td class="l" title="${esc(d.base)}"><b>${esc(d.label)}</b>${d.program ? ` <span class="small muted">${esc(d.program)}</span>` : ""}</td>
        <td>${esc(d.state || "-")}</td>
        ${DH_CAPS.map(([k]) => `<td>${k in d.capacity ? dhCap(d.capacity[k]) : `<span class="muted small">n/a</span>`}</td>`).join("")}
        <td class="mono">${dhNum(d.values.DCBusVoltage, " V", 0)}</td><td class="mono">${dhNum(d.values.OutputCurrent, " A")}</td>
        <td class="mono">${dhNum(d.values.OutputPower, " kW", 2)}</td>
        <td class="small">${d.thermal_on.length ? `<span class="warntext">${d.thermal_on.length} on</span>` : d.thermal_checked ? `<span class="muted">${d.thermal_checked} clear</span>` : `<span class="muted">-</span>`}</td>`,
      d.issues, 12)).join("");
    parts.push(`<div class="panel"><div class="uahead"><h3>Drives</h3><span class="small muted">${drives.length} ${drives.length === 1 ? "axis" : "axes"}</span></div>
      <p class="small muted">Kinetix drives don't send a temperature over the network. <b>Capacity</b> is the drive's own thermal model for the
        motor, the inverter (output stage), the converter (input) and the bus regulator, in % of rated: over 80 % runs hot, 100 % is where it folds back or trips.
        <b>Thermal</b> counts the axis's over-temperature and overload alarm and fault bits that are on.</p>
      ${drives.length ? `<div class="tablewrap"><table class="mdrives dhtable"><thead><tr><th></th><th>Axis</th><th>State</th>
        ${DH_CAPS.map(([, l]) => `<th>${l}</th>`).join("")}<th>DC bus</th><th>Current</th><th>Power</th><th>Thermal</th></tr></thead>
        <tbody>${dRows || `<tr><td colspan="12" class="muted small">No drive has a problem.</td></tr>`}</tbody></table></div>`
        : `<p class="small muted">The dashboard's tag export has no motion axes (AXIS_CIP_DRIVE tags).</p>`}</div>`);

    const ctl = ctls.map((c) => {
      const it = c.items;
      const kv = [["Controller", c.plc], ["Mode", it.Mode], ["Firmware", String(it.Revision || "").replace(/^Revision:\s*/, "")], ["Linx status", it.Status],
        ["Linx connection drops", it.ConnectionsDropped], ["Last drop", it.LastConnectionDropTime],
        ["Linx response", it.AveragePacketResponse !== undefined ? `${it.AveragePacketResponse} ms` : undefined],
        ["Linx poll cycle", it.CLXPresentScanTime !== undefined ? `${it.CLXPresentScanTime} ms (max ${it.CLXMaxScanTime ?? "-"})` : undefined]]
        .filter(([, v]) => v !== undefined && v !== null && v !== "");
      return `<div class="dhctl">${dhChip(c.level)} <dl class="kv">${kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>${dhIssues(c.issues)}</div>`;
    }).join("");
    const ioRows = dhShow(io).map((r) => dhRow(r.level, `<td>${dhChip(r.level)}</td><td class="l"><b>${esc(r.name)}</b></td><td class="small muted">${esc(r.kind)}</td>
        <td class="mono">${r.value === null || r.value === undefined ? "-" : r.kind === "I/O module" ? `0x${(r.value >>> 0).toString(16).toUpperCase().padStart(8, "0")}` : esc(r.value)}</td>`, r.issues, 4)).join("");
    parts.push(`<div class="mgrid dhgrid">
      <div class="panel"><h3>Controller</h3>${ctl || `<p class="small muted">The tag export has no FT Linx Diagnostic Items. Export the whole controller (its shortcut) on System > OPC UA tags to get them.</p>`}</div>
      <div class="panel"><div class="uahead"><h3>I/O modules and connections</h3><span class="small muted">${io.length}</span></div>
        <p class="small muted">Each I/O module's <b>Fault</b> word from the controller (0 = fine, all ones = no connection) and produced/consumed tags' connection status.</p>
        ${io.length ? `<div class="tablewrap"><table class="mdrives"><thead><tr><th></th><th>Name</th><th>Kind</th><th>Fault</th></tr></thead>
          <tbody>${ioRows || `<tr><td colspan="4" class="muted small">No module or connection has a problem.</td></tr>`}</tbody></table></div>`
          : `<p class="small muted">No I/O module fault words in the tag export.</p>`}</div></div>`);
  }

  const devRows = dhShow(devs).map((d) => dhRow(d.level, `<td>${dhChip(d.level)}</td>
      <td class="l" title="${esc(d.vendor)}"><b>${esc(d.product)}</b></td>
      <td class="mono">${esc(d.ip || "-")}</td><td class="small">${esc(d.type)}</td><td class="mono">${esc(d.revision)}</td>
      <td class="mono">${esc(d.serial)}</td><td class="small" title="${esc(d.flags.join(", "))}">${esc(d.status || d.state)}</td>
      <td class="small muted">${esc(d.port || "")}</td>
      <td>${d.ip ? `<button class="btn ghost small admin-only" data-dhprobe="${esc(d.ip)}">Probe</button>` : ""}</td>`, d.issues, 9)).join("");
  parts.push(`<div class="panel"><div class="uahead"><h3>Network devices</h3><span class="small muted">${V?.scan ? `from scan ${esc(V.scan.id)}` : ""}</span></div>
    <p class="small muted">What each EtherNet/IP device said about itself in the last scan (ListIdentity): fault and I/O connection status, firmware
      compared with the other devices of the same product, and serials that answer from more than one address.${V?.other ? ` ${V.other} devices without EtherNet/IP are on Network > Devices.` : ""}</p>
    ${!V?.scan ? `<p class="small muted">No scan yet: run one from Network > Scan.</p>` : devs.length ? `<div class="tablewrap"><table class="mdrives dhtable"><thead><tr><th></th><th>Device</th><th>IP</th><th>Type</th>
      <th>Firmware</th><th>Serial</th><th>Status</th><th>Switch port</th><th></th></tr></thead>
      <tbody>${devRows || `<tr><td colspan="9" class="muted small">No device has a problem.</td></tr>`}</tbody></table></div>` : `<p class="small muted">The scan found no EtherNet/IP devices.</p>`}</div>`);
  $("#dhBody").innerHTML = parts.join("");
}

$("#dhBody").addEventListener("click", (e) => {
  const ip = e.target.dataset?.dhprobe;
  if (!ip) return;
  $("#probeIp").value = ip;
  showTab("probe");
  runProbe();  // probe.js
});
