"use strict";
// Network > Live: what the passive listeners hear (ARP table, BOOTP/DHCP, switch syslog, SNMP traps), refreshed
// every 2 s while the Live or Overview tab is open. Uses $, esc, api and showTab from app.js, fmtHm from production.js
// and runProbe from probe.js.

const live = { seq: 0, events: [], data: null, timer: null, fresh: new Set() };
const LIVE_POLL_MS = 2000, LIVE_KEPT = 300;
const KIND = { arp: "ARP", bootp: "BOOTP", syslog: "Syslog", traps: "Trap", trap: "Trap" };
const LVL_ICON = { err: "&#10006;", warn: "&#9888;", info: "&#8505;", ok: "&#10004;" };

function ago(t) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`;
}
const fmtHms = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const liveVisible = () => !$("#tab-live").classList.contains("hidden") || !$("#tab-overview").classList.contains("hidden");

async function livePoll() {
  clearTimeout(live.timer);
  if (document.hidden || !liveVisible()) return;
  try {
    const d = await api(`api/live?since=${live.seq}`, { timeout: 10000 });
    if (d.seq < live.seq) live.events = [];  // the server restarted
    live.fresh = new Set(d.events.filter(() => live.seq > 0).map((e) => e.seq));
    live.events = [...live.events, ...d.events].slice(-LIVE_KEPT);
    live.seq = d.seq;
    live.data = d;
    liveRender();
  } catch (e) {
    $("#liveMeta").innerHTML = `<span class="errtext">${esc(e.message)}</span>`;
  }
  live.timer = setTimeout(livePoll, LIVE_POLL_MS);
}
document.addEventListener("gm:tab", (e) => { if (e.detail === "live" || e.detail === "overview") livePoll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) livePoll(); });
$("#liveProblems").addEventListener("change", () => liveRender());

function liveRender() {
  const d = live.data;
  if (!d) return;
  liveStrip(d);
  if ($("#tab-live").classList.contains("hidden")) return;
  const admin = !isViewer();
  $("#liveMeta").innerHTML = !d.listen ? `<span class="warntext">Listeners are off (Ghost Map was started with --no-listen).</span>`
    : `${d.simulated ? `<span class="chip">simulated demo traffic</span> ` : ""}Listening since ${esc(fmtHm(d.started))} &middot; ${d.devices.length} devices heard`;
  $("#liveBeat").classList.toggle("off", !d.running);
  $("#liveListeners").innerHTML = d.listeners.map((l) => {
    const st = !l.enabled ? "off" : l.state;
    const chip = st === "listening" ? `<span class="chip okchip">listening</span>` : st === "error" ? `<span class="chip errchip">not listening</span>`
      : `<span class="chip">off</span>`;
    return `<div class="listener ${esc(st)}"><div class="lhead"><b>${esc(l.label)}</b>${chip}</div>
      <div class="small muted">${l.port ? `UDP ${l.port} &middot; ` : ""}${esc(l.what)}</div>
      ${l.error ? `<div class="small errtext">${esc(l.error)}</div>` : ""}
      <div class="lfoot"><span class="small"><b>${Number(l.packets).toLocaleString()}</b> ${l.name === "arp" ? "reads" : "packets"}${l.last ? ` &middot; last ${esc(ago(l.last))}` : ""}</span>
        ${admin && d.listen ? `<label class="tgl" title="${l.enabled ? "Stop" : "Start"} this listener"><input type="checkbox" data-listener="${esc(l.name)}" ${l.enabled ? "checked" : ""}><span></span></label>` : ""}</div></div>`;
  }).join("");
  const hour = live.events.filter((e) => e.at > Date.now() / 1000 - 3600);
  const n = (lvl) => hour.filter((e) => e.level === lvl).length;
  const fresh = d.devices.filter((x) => Date.now() / 1000 - x.last < 60).length;
  const unknown = d.devices.filter((x) => x.mac && !x.label).length;
  $("#liveTiles").innerHTML = [
    ["Problems, last hour", n("err"), n("err") ? "err" : "ok"], ["Warnings, last hour", n("warn"), n("warn") ? "warn" : ""],
    ["Heard in the last minute", fresh, fresh ? "ok" : ""], ["Not in the last scan", unknown, unknown ? "warn" : ""],
  ].map(([l, v, c]) => `<div class="tile ${c}"><div class="n">${v}</div><div class="l">${esc(l)}</div></div>`).join("");
  const only = $("#liveProblems").checked;
  const evs = live.events.filter((e) => !only || e.level === "err" || e.level === "warn").slice().reverse();
  $("#liveFeed").innerHTML = evs.map((e) => `<div class="fev ${esc(e.level)}${live.fresh.has(e.seq) ? " new" : ""}">
      <span class="ficon">${LVL_ICON[e.level] || ""}</span>
      <div class="fbody"><div>${esc(e.text)}</div>${e.hint ? `<div class="small muted">${esc(e.hint)}</div>` : ""}
        <div class="small muted"><span class="chip">${esc(KIND[e.kind] || e.kind)}</span> ${esc(fmtHms(e.at))} &middot; ${esc(ago(e.at))}
        ${admin && e.ip ? ` &middot; <a href="#" data-live-probe="${esc(e.ip)}">inspect ${esc(e.ip)}</a>` : ""}</div></div></div>`).join("")
    || `<p class="small muted">${only ? "No problems heard." : "Nothing heard yet. Events appear here as they happen."}</p>`;
  $("#liveDevCount").textContent = `${d.devices.length} total`;
  $("#liveDevices").innerHTML = d.devices.map((x) => {
    const age = Date.now() / 1000 - x.last, cls = age < 60 ? "on" : age < 600 ? "recent" : "";
    return `<tr><td><span class="hdot ${cls}" title="last heard ${esc(ago(x.last))}"></span></td>
      <td class="mono">${admin && x.ip ? `<a href="#" data-live-probe="${esc(x.ip)}">${esc(x.ip)}</a>` : esc(x.ip || "-")}</td>
      <td class="ldev">${x.label ? esc(x.label.replace(/ \([\d.]+\)$/, "")) : `${esc(x.vendor || "unknown")} <span class="chip warnchip">not in last scan</span>`}
        ${x.mac ? `<div class="mono small muted">${esc(x.mac)}</div>` : ""}</td><td>${x.sources.map((s) => `<span class="chip">${esc(KIND[s] || s)}</span>`).join(" ")}</td>
      <td class="small">${esc(ago(x.last))}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">No devices heard yet.</td></tr>`;
}

// A one-line summary on the scan Overview, so the live side is visible from there too.
function liveStrip(d) {
  const el = $("#liveStrip");
  if (!el) return;
  const hour = live.events.filter((e) => e.at > Date.now() / 1000 - 3600 && (e.level === "err" || e.level === "warn"));
  const last = live.events[live.events.length - 1];
  el.innerHTML = !d.listen ? "" : `<div class="panel livestrip ${hour.some((e) => e.level === "err") ? "err" : hour.length ? "warn" : "ok"}">
    <span class="livedot"></span><b>Live</b><span class="small">${hour.length ? `${hour.length} ${hour.length === 1 ? "warning or problem" : "warnings and problems"}` : "nothing wrong"} heard in the last hour
      &middot; ${d.devices.length} devices heard${last ? ` &middot; last: ${esc(last.text)} (${esc(ago(last.at))})` : ""}</span>
    <span class="grow"></span><button class="btn ghost small" type="button" data-live-open>Open Live</button></div>`;
}

document.addEventListener("click", async (e) => {
  const open = e.target.closest("[data-live-open]");
  if (open) { showTab("live"); return; }
  const p = e.target.closest("[data-live-probe]");
  if (p) {
    e.preventDefault();
    $("#probeIp").value = p.dataset.liveProbe;
    showTab("probe");
    runProbe();
    return;
  }
  const t = e.target.closest("[data-listener]");
  if (t) {
    try {
      await api(`api/live/listeners/${encodeURIComponent(t.dataset.listener)}`, { method: "POST", body: JSON.stringify({ enabled: t.checked }) });
    } catch (err) { alert(err.message); }
    livePoll();
  }
});
