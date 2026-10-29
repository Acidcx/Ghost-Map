"use strict";
// The alert list (GET api/alerts, rules in analysis/alerts.py): a count on the Alerts button, errors as a line under
// the tabs on every page, and the whole list in a dialog. Each alert links to the page that shows more.
// Also Maintenance > This PC (GET api/host): disk, memory, CPU, uptime, FactoryTalk services and event-log errors.
// Uses $, esc, api, showTab, fmtUptime and fmtAgo from app.js.

const ALERTS_POLL_MS = 30000, HOST_POLL_MS = 60000;
const alerts = { data: null, timer: null, hostTimer: null };
const GiB = 1024 ** 3;
const fmtGb = (n) => (n == null ? "-" : `${(n / GiB).toFixed(n < 10 * GiB ? 1 : 0)} GB`);
const alertClock = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const sinceText = (t) => (t ? `since ${alertClock(t)} (${fmtAgo(Math.max(0, Math.round(Date.now() / 1000 - t)))})` : "");

const muteText = (m) => m.kind === "ack" ? `acknowledged${m.by ? ` by ${m.by}` : ""} ${fmtAgo(Math.max(0, Math.round(Date.now() / 1000 - m.at)))}, until it clears`
  : `muted${m.target.startsWith("code:") ? ` (every ${m.code})` : ""}${m.by ? ` by ${m.by}` : ""} until ${alertClock(m.until)}`;

// Acknowledge or mute, for admins in the dialog: the alert leaves the banner and the count, not the list.
function muteControls(a) {
  if (a.muted) return `<button type="button" class="btn ghost small admin-only" data-unmute="${esc(a.muted.target)}">Unmute</button>`;
  return `<select class="small admin-only" data-mute="${esc(a.key)}" title="Take it off the banner without fixing it">
    <option value="">Acknowledge or mute...</option>
    <option value="ack">Acknowledge (until it clears or gets worse)</option>
    <option value="8">Mute for 8 hours</option>
    <option value="24">Mute for 24 hours</option>
    <option value="168">Mute for 7 days</option>
    <option value="every:168">Mute every ${esc(a.code)} for 7 days (commissioning)</option>
  </select>`;
}

function alertHtml(a, here = false, controls = false) {  // here: on the page the alert links to
  const meta = [`<span class="mono">${esc(a.code)}</span>`, esc(sinceText(a.since)),
    a.muted ? `<span class="muted">${esc(muteText(a.muted))}</span>` : "",
    a.link?.tab && !here ? `<a href="#" data-alertlink="${esc(JSON.stringify(a.link))}">Show</a>` : "",
    controls ? muteControls(a) : ""].filter(Boolean).join(" &middot; ");
  return `<div class="finding alert${a.muted ? " muted-alert" : ""}">
    <span class="sev ${esc(a.severity)}">${esc(a.severity)}</span>
    <div><span class="t">${esc(a.title)}</span>${a.detail ? `<div class="small">${esc(a.detail)}</div>` : ""}</div>
    ${a.hint ? `<div class="hint">${esc(a.hint)}</div>` : ""}
    <div class="hint small">${meta}</div></div>`;
}

// Go to the page an alert points at: a tab, and for a machine dashboard which one and which view.
async function alertGo(link) {
  $("#alertsDialog").close();
  if (link.dash) {
    try {
      localStorage.setItem("gm.dash", link.dash);
      if (link.view) localStorage.setItem(`gm.dash.view.${link.dash}`, link.view);
    } catch (_) { /* ignore */ }
  }
  showTab(link.tab);
  if (link.dash && window.machineOpen) await window.machineOpen(link.dash);
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-alertlink]");
  if (!el) return;
  e.preventDefault();
  try { alertGo(JSON.parse(el.dataset.alertlink)); } catch (_) { /* ignore */ }
});

async function alertsPoll() {
  clearTimeout(alerts.timer);
  if (!document.hidden) {
    try {
      alerts.data = await api("api/alerts", { timeout: 60000 });
      alerts.error = null;
    } catch (e) { alerts.error = e.message; }
    alertsRender();
  }
  alerts.timer = setTimeout(alertsPoll, ALERTS_POLL_MS);
}

function alertsRender() {
  const d = alerts.data;
  const n = d ? d.alerts.length : 0;
  const errors = d ? d.counts.error : 0;
  const count = $("#alertsCount");
  count.textContent = d ? String(n) : "?";
  count.className = `chip ${!d ? "" : errors ? "errchip" : n ? "warnchip" : "okchip"}`;
  const quiet = d?.counts?.muted ? `, ${d.counts.muted} muted` : "";
  $("#alertsBtn").title = !d ? `Alerts could not be checked${alerts.error ? `: ${alerts.error}` : ""}`
    : (n ? `${errors} error${errors === 1 ? "" : "s"}, ${d.counts.warning} warning${d.counts.warning === 1 ? "" : "s"}` : "Nothing needs attention") + quiet;

  // The line under the tabs: errors only, the worst first.
  const errs = d ? d.alerts.filter((a) => a.severity === "error") : [];
  const bar = $("#alertBar");
  bar.classList.toggle("hidden", !errs.length);
  if (errs.length) {
    const a = errs[0];
    bar.innerHTML = `<span class="sev error">error</span> <b>${esc(a.title)}</b>
      <span class="muted">${esc(sinceText(a.since))}</span>
      ${errs.length > 1 ? `<span class="muted">and ${errs.length - 1} more</span>` : ""}
      ${a.link?.tab ? `<a href="#" data-alertlink="${esc(JSON.stringify(a.link))}">Show</a>` : ""}
      <a href="#" id="alertBarAll">All alerts</a>`;
  }
  if ($("#alertsDialog").open) alertsDialogRender();
}

function alertsDialogRender() {
  const d = alerts.data;
  $("#alertsAt").textContent = d?.at ? `Checked ${new Date(d.at * 1000).toLocaleTimeString()}` : "";
  if (!d) {
    $("#alertsList").innerHTML = `<p class="errtext">Alerts could not be checked${alerts.error ? `: ${esc(alerts.error)}` : ""}.</p>`;
    return;
  }
  const broken = Object.entries(d.errors || {});
  const muted = d.muted || [];
  // Mutes for a whole code show even with no alert right now, so they can be lifted before they run out.
  const idle = (d.mutes || []).filter((m) => !muted.some((a) => a.muted.target === m.target));
  $("#alertsList").innerHTML = (d.alerts.length ? d.alerts.map((a) => alertHtml(a, false, true)).join("")
    : `<p class="oktext">Nothing needs attention${muted.length ? " (apart from what is muted below)" : ""}.</p>`)
    + (muted.length || idle.length ? `<h4 class="small muted">Muted (${muted.length})</h4>` + muted.map((a) => alertHtml(a, false, true)).join("")
      + idle.map((m) => `<p class="small muted">${esc(m.title)}: ${esc(muteText(m))}, nothing to show right now
        <button type="button" class="btn ghost small admin-only" data-unmute="${esc(m.target)}">Unmute</button></p>`).join("") : "")
    + (broken.length ? `<p class="small warntext">Not checked this time (the last alerts from these are kept): ${
      broken.map(([k, v]) => `${esc(k)} (${esc(v)})`).join(", ")}</p>` : "");
  const recent = d.recent || [];
  $("#alertsRecentBox").classList.toggle("hidden", !recent.length);
  $("#alertsRecent").innerHTML = `<table class="small"><tbody>${recent.slice(0, 30).map((r) => `<tr>
    <td class="nowrap">${esc(alertClock(r.at))}</td>
    <td>${r.event === "cleared" ? `<span class="oktext">cleared</span>` : `<span class="${r.severity === "error" ? "errtext" : "warntext"}">${esc(r.severity)}</span>`}</td>
    <td class="wrap">${esc(r.title)}</td></tr>`).join("")}</tbody></table>`;
}

function alertsOpen() {
  alertsDialogRender();
  $("#alertsDialog").showModal();
  alertsPoll();
}
$("#alertsBtn").addEventListener("click", alertsOpen);
$("#alertsList").addEventListener("change", async (e) => {
  const sel = e.target.closest("[data-mute]");
  if (!sel || !sel.value) return;
  const v = sel.value, every = v.startsWith("every:");
  try {
    alerts.data = await api("api/alerts/mute", { method: "POST", body: JSON.stringify({ key: sel.dataset.mute,
      hours: v === "ack" ? null : Number(every ? v.slice(6) : v), every }) });
    alerts.error = null;
  } catch (err) { alerts.error = err.message; }
  alertsRender();
});
$("#alertsList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-unmute]");
  if (!b) return;
  try {
    alerts.data = await api("api/alerts/unmute", { method: "POST", body: JSON.stringify({ target: b.dataset.unmute }) });
  } catch (err) { alerts.error = err.message; }
  alertsRender();
});
$("#alertsClose").addEventListener("click", () => $("#alertsDialog").close());
$("#alertBar").addEventListener("click", (e) => {
  if (e.target.id === "alertBarAll") { e.preventDefault(); alertsOpen(); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) alertsPoll(); });
alertsPoll();

// ------------------------------------------------------------------ Maintenance > This PC
async function hostPoll() {
  clearTimeout(alerts.hostTimer);
  if (document.hidden || $("#tab-hostpc").classList.contains("hidden")) return;
  try {
    hostRender(await api("api/host", { timeout: 120000 }));
  } catch (e) {
    $("#hpTiles").innerHTML = `<p class="errtext">Could not read this PC's health: ${esc(e.message)}</p>`;
  }
  alerts.hostTimer = setTimeout(hostPoll, HOST_POLL_MS);
}

function hostRender(h) {
  $("#hpTitle").textContent = `This PC: ${h.host}`;
  $("#hpAt").textContent = h.at ? `${h.os} · checked ${new Date(h.at * 1000).toLocaleTimeString()}` : h.os;
  const pctCls = (p, warn, err) => (p == null ? "" : p >= err ? "err" : p >= warn ? "warn" : "ok");
  const bar = (p, cls) => `<div class="pbarwrap"><span class="${cls === "err" ? "overdue" : cls === "warn" ? "soon" : ""}" style="width:${Math.min(100, p || 0)}%"></span></div>`;
  const tiles = (h.disks || []).map((d) => {
    const cls = pctCls(d.used_pct, 90, 97);
    return `<div class="tile ${cls}"><div class="n">${d.used_pct == null ? "-" : `${Math.round(d.used_pct)}%`}</div>
      <div class="l">Disk ${esc(d.path)}</div>${bar(d.used_pct, cls)}
      <div class="s small muted" title="${d.data ? "Ghost Map's data is on this drive" : ""}">${fmtGb(d.free)} free of ${fmtGb(d.total)}</div></div>`;
  });
  const m = h.memory;
  if (m) {
    const cls = pctCls(m.used_pct, 90, 95);
    tiles.push(`<div class="tile ${cls}"><div class="n">${Math.round(m.used_pct)}%</div><div class="l">Memory</div>${bar(m.used_pct, cls)}
      <div class="s small muted">${fmtGb(m.available)} free of ${fmtGb(m.total)}</div></div>`);
  }
  const cpu = h.cpu || {};
  const cpuCls = pctCls(cpu.avg_5m, 90, 101);
  tiles.push(`<div class="tile ${cpuCls}"><div class="n">${cpu.now == null ? "-" : `${Math.round(cpu.now)}%`}</div><div class="l">CPU</div>
    ${bar(cpu.now, cpuCls)}<div class="s small muted">${cpu.avg_5m == null ? "Measuring..." : `5 min average ${Math.round(cpu.avg_5m)}%`}</div></div>`);
  if (h.uptime_s != null) {
    tiles.push(`<div class="tile"><div class="n">${esc(fmtUptime(h.uptime_s))}</div><div class="l">Uptime</div>
      <div class="s small muted">${h.boot_at ? `Started ${esc(alertClock(h.boot_at))}` : ""}</div></div>`);
  }
  $("#hpTiles").innerHTML = tiles.join("");
  $("#hpAlerts").innerHTML = (h.alerts || []).map((a) => alertHtml(a, true)).join("");

  const winNote = !h.windows ? "Services and the event log are read on Windows only."
    : h.windows_error ? `Could not read services and the event log: ${h.windows_error}`
    : !h.windows_read_at ? "Reading services and the event log..." : "";
  const svc = h.services || [];
  $("#hpServices").innerHTML = winNote && !svc.length ? `<p class="small muted">${esc(winNote)}</p>`
    : !svc.length ? `<p class="small muted">No FactoryTalk or Rockwell services on this PC.</p>`
    : `<div class="tablewrap"><table><thead><tr><th>Service</th><th>State</th><th>Starts</th></tr></thead><tbody>${svc.map((s) => {
      const bad = (s.start_mode || "").toLowerCase() === "auto" && (s.state || "").toLowerCase() !== "running";
      return `<tr><td title="${esc(s.name)}">${esc(s.display || s.name)}</td>
        <td class="${bad ? "errtext" : (s.state || "").toLowerCase() === "running" ? "oktext" : "muted"}">${esc(s.state)}</td>
        <td class="muted">${esc(s.start_mode)}</td></tr>`;
    }).join("")}</tbody></table></div>`;

  const ev = h.events;
  $("#hpEventsTitle").textContent = `Windows event log, last ${ev?.window_h || 24} h`;
  $("#hpEvents").innerHTML = !ev ? `<p class="small muted">${esc(winNote || "Not read yet.")}</p>`
    : `<p class="small">${ev.critical ? `<span class="errtext">${ev.critical} critical</span>, ` : ""}${ev.errors} error${ev.errors === 1 ? "" : "s"}
      in the System and Application logs${ev.top.length ? ", from these sources most:" : "."}</p>
      ${ev.top.length ? `<div class="tablewrap"><table><thead><tr><th>Source</th><th class="num">Count</th><th>Last</th><th>Latest message</th></tr></thead><tbody>${
        ev.top.map((g) => `<tr><td><span class="${g.level === "critical" ? "errtext" : ""}">${esc(g.source)}</span>
          <span class="small muted">${esc(g.log)} ${esc(g.id)}</span></td><td class="num">${g.count}</td>
          <td class="nowrap small">${g.last ? esc(alertClock(g.last)) : ""}</td><td class="wrap small">${esc(g.message)}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}

document.addEventListener("gm:tab", (e) => { if (e.detail === "hostpc") hostPoll(); else clearTimeout(alerts.hostTimer); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) hostPoll(); });
