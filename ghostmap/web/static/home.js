"use strict";
// Home: is everything OK? One card per area, each read from the same API as its own page, and a click opens
// that page. Calm by default: color only where something needs attention (ISA-101 style).
// Uses $, esc, api, state, showTab and ring from app.js, machineSummary from machine.js, alertsOpen from alerts.js.

const HOME_MS = 15000;
const home = { timer: null, seq: 0 };
const RANK = { err: 3, warn: 2, ok: 1, "": 0 };
const hNum = (n, d = 0) => (n == null || Number.isNaN(n) ? "-" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const hPct = (p) => `${Math.round(p)}%`;
const hAgo = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`; };

// A small line chart of the last values (0-100 unless max is given).
function spark(values, cls = "", max = 100, w = 120, h = 28) {
  if (!values || values.length < 2) return "";
  const step = w / (values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - (Math.min(v, max) / max) * (h - 4)).toFixed(1)}`).join(" ");
  return `<svg class="spark ${cls}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke-width="1.6"/></svg>`;
}
const bar = (pct, cls = "") => `<span class="hbar ${cls}"><span style="width:${Math.max(0, Math.min(100, pct || 0))}%"></span></span>`;

function card(c) {
  const pill = c.pill ? `<span class="hpill ${c.state}">${esc(c.pill)}</span>` : "";
  return `<article class="hcard ${c.state || ""}${c.wide ? " wide" : ""}" data-go="${esc(c.go || "")}" tabindex="0" role="button" aria-label="${esc(c.title)}: open">
    <header><span class="hico" aria-hidden="true">${c.icon}</span><h3>${esc(c.title)}</h3>${pill}</header>
    ${c.big != null ? `<div class="hbig ${c.bigCls || ""}">${c.big}${c.bigSub ? `<small>${c.bigSub}</small>` : ""}</div>` : ""}
    ${c.body || ""}
    ${c.foot ? `<footer>${c.foot}</footer>` : ""}</article>`;
}
const kv = (rows) => `<dl class="hkv">${rows.filter(Boolean).map(([k, v, cls]) => `<div><dt>${esc(k)}</dt><dd class="${cls || ""}">${v}</dd></div>`).join("")}</dl>`;
const problem = (e) => card({ title: e.title, icon: e.icon, state: "warn", pill: "can't read", go: e.go,
  body: `<p class="small muted">${esc(e.message)}</p>` });

// ------------------------------------------------------------------ cards
async function cardMachine() {
  const icon = "&#9881;&#xFE0E;";
  let m;
  try { m = await machineSummary(); } catch (e) { return problem({ title: "Machine", icon, go: "machine", message: e.message }); }
  if (!m) {
    return card({ title: "Machine", icon, state: "", go: isViewer() ? "machine" : "opcua", big: "No dashboard yet",
      bigCls: "dim", body: `<p class="small muted">${isViewer() ? "An admin builds one from the PLC's tags." : "Build one from the PLC's tags: System &rsaquo; OPC UA tags."}</p>` });
  }
  const run = m.run, commsCls = m.comms.cls || "";
  const st = !m.known ? "warn" : m.faults ? "err" : m.warnings || commsCls === "warn" ? "warn" : run && !run.running ? "warn" : "ok";
  const big = !m.known ? "No data" : !run ? "-" : run.running ? "Running" : "Stopped";
  const list = m.active.length ? `<ul class="hlist">${m.active.map((a) => `<li class="${a.severity === "warning" ? "warn" : "err"}">${esc(a.label)} <span class="muted">${esc(a.area)}</span></li>`).join("")}</ul>` : "";
  return card({ title: m.name, icon, state: st, go: "machine", pill: m.faults ? `${m.faults} fault${m.faults === 1 ? "" : "s"}` : m.known ? "" : "no data",
    big, bigCls: !m.known ? "dim" : run?.running ? "ok" : "warn",
    body: kv([["Faults", m.known ? hNum(m.faults) : "?", m.faults ? "err" : ""], ["Warnings", m.known ? hNum(m.warnings) : "?", m.warnings ? "warn" : ""],
      m.estops ? ["E-stops", hNum(m.estops), "err"] : null, m.axes ? ["Axes OK", `${m.axes - m.faultedAxes}/${m.axes}`, m.faultedAxes ? "err" : ""] : null,
      ["Comms", esc(m.comms.n), commsCls === "ok" ? "" : commsCls]]) + list });
}

async function cardProduction() {
  const icon = "&#9638;";
  let lines;
  try { lines = await api("api/tsc/lines", { timeout: 40000 }); } catch (e) { return problem({ title: "Production", icon, go: "production", message: e.message }); }
  if (!lines.ok) {
    if (lines.configured === false) {
      return card({ title: "Production", icon, go: isViewer() ? "production" : "tscconn", big: "Not connected", bigCls: "dim",
        body: `<p class="small muted">Parts, rates and stops come from TSC.${isViewer() ? "" : " Connect it under System &rsaquo; TSC database."}</p>` });
    }
    return problem({ title: "Production", icon, go: "production", message: lines.error || "TSC could not be read" });
  }
  let id = null;
  try { id = Number(localStorage.getItem("gm.tscline")); } catch (_) { /* ignore */ }
  const line = lines.lines.find((l) => l.id === id) || lines.lines[0];
  if (!line) return card({ title: "Production", icon, go: "production", big: "No lines", bigCls: "dim" });
  let p;
  try { p = await api(`api/tsc/production?line=${encodeURIComponent(line.id)}`, { timeout: 60000 }); } catch (e) { return problem({ title: line.name, icon, go: "production", message: e.message }); }
  if (!p.ok) return problem({ title: line.name, icon, go: "production", message: p.error || "TSC could not be read" });
  const t = p.totals || {}, dt = p.downtime || {}, stations = p.stations || [];
  const stopped = dt.ongoing || stations.some((s) => s.state === "stopped" || s.state === "error");
  const st = stations.some((s) => s.state === "error") ? "err" : stopped ? "warn" : "ok";
  const unit = p.unit === "in" ? "ft" : p.unit;
  const st1 = stations.map((s) => `<div class="hstation"><div><b>${esc(s.name)}</b>${s.state ? ` <span class="hpill ${s.state === "error" ? "err" : s.state === "stopped" ? "warn" : ""}">${esc(s.state)}</span>` : ""}</div>
    ${s.order ? `<div class="small">${esc(s.order)} &middot; ${esc(s.profile || "")}${s.length ? ` &middot; ${esc(fmtLen(s.length, p.unit))}` : ""}</div>
    <div class="hprog">${bar((s.progress || 0) * 100, st)}<span class="small">${hNum(s.qty)} / ${hNum(s.of)}</span></div>` : `<div class="small muted">No order running</div>`}</div>`).join("");
  return card({ title: line.name, icon, state: st, go: "production", wide: true, pill: dt.ongoing ? "stopped" : p.shift?.name ? `${p.shift.name} shift` : "",
    big: hNum(t.good_pieces ?? t.pieces), bigSub: "pieces this shift",
    body: kv([["Per hour", hNum(t.pieces_per_hour, 0)], [unit === "ft" ? "Feet" : "Length", hNum(t.feet)],
      ["Running", dt.running_share != null ? hPct(dt.running_share * 100) : "-", dt.running_share != null && dt.running_share < 0.8 ? "warn" : ""],
      ["Stops", hNum(dt.count), dt.count ? "warn" : ""], t.scrap_pieces ? ["Scrap", hNum(t.scrap_pieces), "warn"] : null,
      p.queue_total ? ["In queue", `${hNum(p.queue_total.pieces)} pcs`] : null]) + `<div class="hstations">${st1}</div>` });
}

async function cardAlerts() {
  const icon = "&#9888;&#xFE0E;";
  let a;
  try { a = await api("api/alerts", { timeout: 60000 }); } catch (e) { return problem({ title: "Alerts", icon, go: "@alerts", message: e.message }); }
  const err = a.counts?.error || 0, warn = a.counts?.warning || 0;
  const st = err ? "err" : warn ? "warn" : "ok";
  const list = (a.alerts || []).slice(0, 5).map((x) => `<li class="${x.severity === "error" ? "err" : "warn"}">${esc(x.title)}</li>`).join("");
  return card({ title: "Alerts", icon, state: st, go: "@alerts", big: err + warn ? hNum(err + warn) : "All clear", bigCls: err ? "err" : warn ? "warn" : "ok",
    bigSub: err + warn ? (err ? `${err} error${err === 1 ? "" : "s"}, ${warn} warning${warn === 1 ? "" : "s"}` : `warning${warn === 1 ? "" : "s"}`) : "",
    body: (list ? `<ul class="hlist">${list}</ul>` : `<p class="small muted">Nothing needs attention.</p>`)
      + (a.counts?.muted ? `<p class="small muted">${hNum(a.counts.muted)} muted</p>` : "") });
}

async function cardService() {
  const icon = "&#10070;";
  let m;
  try { m = await api("api/maintenance", { timeout: 60000 }); } catch (e) { return problem({ title: "Maintenance", icon, go: "service", message: e.message }); }
  const items = m.items || [];
  if (!items.length) return card({ title: "Maintenance", icon, go: "service", big: "No service items", bigCls: "dim", body: `<p class="small muted">Add blades, dies and lubrication on Maintenance &rsaquo; Service.</p>` });
  const over = items.filter((i) => i.state === "overdue").length, soon = items.filter((i) => i.state === "soon").length;
  const top = [...items].sort((x, y) => (y.pct || 0) - (x.pct || 0)).slice(0, 4);
  const cls = (i) => (i.state === "overdue" ? "err" : i.state === "soon" ? "warn" : "");
  return card({ title: "Maintenance", icon, state: over ? "err" : soon ? "warn" : "ok", go: "service",
    big: over ? `${over} overdue` : soon ? `${soon} due soon` : "Up to date", bigCls: over ? "err" : soon ? "warn" : "ok",
    body: `<ul class="hitems">${top.map((i) => `<li><span class="nm">${esc(i.name)}</span>${bar(i.pct, cls(i))}<span class="small ${cls(i)}">${i.pct != null ? hPct(i.pct) : "-"}</span></li>`).join("")}</ul>` });
}

async function cardNetwork() {
  const icon = "&#9673;";
  const [live, nm] = await Promise.all([api("api/live", { timeout: 30000 }).catch(() => null), api("api/netmon/status", { timeout: 30000 }).catch(() => null)]);
  const scan = state.scans?.[0];
  const sws = nm?.configured ? nm.switches || [] : [];
  const swBad = sws.filter((s) => s.error).length, swEvents = sws.reduce((n, s) => n + (s.open_events || 0), 0);
  const lc = live?.counts || {};
  const st = swBad || lc.err ? "err" : swEvents || lc.warn || scan?.errors ? "warn" : live || sws.length || scan ? "ok" : "";
  return card({ title: "Network", icon, state: st, go: "live",
    big: live ? hNum(live.devices?.length) : scan ? hNum(scan.devices) : "-", bigSub: live ? "devices heard" : scan ? "devices in the last scan" : "",
    body: kv([sws.length ? ["Switches", `${sws.length - swBad}/${sws.length} answering`, swBad ? "err" : ""] : ["Switches", "not watched"],
      sws.length ? ["Switch events", hNum(swEvents), swEvents ? "warn" : ""] : null,
      live ? ["Live events", `${hNum(lc.err || 0)} err, ${hNum(lc.warn || 0)} warn`, lc.err ? "err" : lc.warn ? "warn" : ""] : null,
      scan ? ["Last scan", `${hNum(scan.errors)} err, ${hNum(scan.warnings)} warn <small class="muted">${esc(new Date(scan.started).toLocaleDateString([], { month: "short", day: "numeric" }))}</small>`, scan.errors ? "err" : scan.warnings ? "warn" : ""] : null]) });
}

async function cardHost() {
  const icon = "&#9638;&#xFE0E;";
  let h;
  try { h = await api("api/host", { timeout: 120000 }); } catch (e) { return problem({ title: "This PC", icon, go: "hostpc", message: e.message }); }
  const disk = (h.disks || []).find((d) => d.data) || (h.disks || [])[0];
  const lvl = (p, w, e) => (p >= e ? "err" : p >= w ? "warn" : "ok");
  const stopped = (h.services || []).filter((s) => s.start_mode === "Auto" && s.state !== "Running");
  const al = h.alerts || [];
  const st = al.some((a) => a.severity === "error") || stopped.length ? "err" : al.length ? "warn" : "ok";
  const gauges = [disk && [disk.used_pct, `Disk ${disk.path}`, lvl(disk.used_pct, 85, 95)], h.memory && [h.memory.used_pct, "Memory", lvl(h.memory.used_pct, 85, 95)],
    h.cpu && [h.cpu.now, "CPU", lvl(h.cpu.avg_5m ?? h.cpu.now, 80, 95)]].filter(Boolean);
  return card({ title: h.host || "This PC", icon, state: st, go: "hostpc", wide: true, pill: stopped.length ? `${stopped.length} service${stopped.length === 1 ? "" : "s"} stopped` : "",
    body: `<div class="hsplit"><div><div class="hrings">${gauges.map(([p, l, c]) => `<div>${ring(p, hPct(p), c === "ok" ? "" : c, 58, 6)}<span class="small muted">${esc(l)}</span></div>`).join("")}</div>
      ${h.cpu?.series?.length > 2 ? `<div class="hspark"><span class="small muted">CPU</span>${spark(h.cpu.series.map((x) => x.pct), "", 100, 160, 26)}</div>` : ""}</div>` +
      kv([["Up", h.uptime_s != null ? fmtUptime(h.uptime_s) : "-"], h.services ? ["Services", `${h.services.length - stopped.length}/${h.services.length} running`, stopped.length ? "err" : ""] : null,
        h.events ? ["Event log, 24 h", `${hNum(h.events.errors)} error${h.events.errors === 1 ? "" : "s"}`, h.events.errors ? "warn" : ""] : null]) + "</div>" });
}

// ------------------------------------------------------------------ page
async function homePoll() {
  clearTimeout(home.timer);
  if (document.hidden || $("#tab-home").classList.contains("hidden")) return;
  const seq = ++home.seq;
  const loaders = [cardMachine, cardProduction, cardAlerts, cardService, cardNetwork, cardHost];
  if (!$("#hGrid").children.length) $("#hGrid").innerHTML = loaders.map(() => `<article class="hcard loading"><div class="shimmer"></div></article>`).join("");
  const html = await Promise.all(loaders.map((f) => f().catch((e) => problem({ title: f.name.replace(/^card/, ""), icon: "?", go: "", message: e.message }))));
  if (seq !== home.seq) return;
  $("#hGrid").innerHTML = html.join("");
  const states = [...$("#hGrid").querySelectorAll(".hcard")].map((c) => ["err", "warn", "ok"].find((k) => c.classList.contains(k)) || "");
  const worstState = states.reduce((w, s) => (RANK[s] > RANK[w] ? s : w), "");
  const n = states.filter((s) => s === "err" || s === "warn").length;
  $("#hBanner").className = `hbanner ${worstState}`;
  $("#hBanner").innerHTML = worstState === "err" ? `<b>${n} area${n === 1 ? " needs" : "s need"} attention</b>`
    : worstState === "warn" ? `<b>${n} area${n === 1 ? "" : "s"} to keep an eye on</b>` : `<b>All clear</b>`;
  $("#hAt").textContent = `Updated ${new Date().toLocaleTimeString()}, every ${HOME_MS / 1000} s`;
  home.timer = setTimeout(homePoll, HOME_MS);
}

$("#hGrid").addEventListener("click", (e) => {
  const c = e.target.closest("[data-go]");
  if (!c || !c.dataset.go) return;
  if (c.dataset.go === "@alerts") alertsOpen(); else showTab(c.dataset.go);
});
$("#hGrid").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-go]")) { e.preventDefault(); e.target.click(); }
});
document.addEventListener("gm:tab", (e) => { if (e.detail === "home") homePoll(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) homePoll(); });
