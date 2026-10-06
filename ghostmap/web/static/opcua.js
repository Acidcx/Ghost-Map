"use strict";
// OPC UA Tag Browser (read-only), laid out like UaExpert: address space tree, data access (watch) view,
// attributes panel. Uses $, esc and api() from app.js. Only browse/read calls exist on the server.

const ua = { sid: null, url: "", watch: new Map(), sel: null, timer: null, busy: false };
const uaForm = $("#uaForm");
const RECENT_KEY = "gm.ua.recent";

function uaRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch (_) { return []; }
}
function uaRemember(url) {
  const list = [url, ...uaRecent().filter((u) => u !== url)].slice(0, 8);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (_) { /* ignore */ }
  uaFillRecent();
}
function uaFillRecent() {
  $("#uaRecent").innerHTML = uaRecent().map((u) => `<option value="${esc(u)}">`).join("");
}
uaFillRecent();

function uaStatus(html, cls = "muted") {
  const el = $("#uaStatus");
  el.className = `small ${cls}`;
  el.innerHTML = html;
}

const fmtVal = (v) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const fmtTime = (t) => (t ? t.replace("T", " ").replace(/\.\d+/, "").replace("+00:00", "Z") : "");

// ------------------------------------------------------------------ connect
uaForm.security.addEventListener("change", () =>
  uaForm.querySelectorAll(".uasec").forEach((el) => el.classList.toggle("hidden", uaForm.security.value === "None")));

$("#uaEndpoints").addEventListener("click", async () => {
  const box = $("#uaEpList");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="muted small">Asking the server for its endpoints...</p>`;
  try {
    const eps = await api("api/opcua/endpoints", { method: "POST", body: JSON.stringify({ url: uaForm.url.value }) });
    box.innerHTML = `<table class="small"><tr><th>Endpoint</th><th>Security</th><th>Mode</th><th>Logins</th><th></th></tr>` +
      eps.map((e, i) => `<tr><td class="mono">${esc(e.url)}</td><td>${esc(e.security_policy)}</td><td>${esc(e.security_mode)}</td>
        <td>${esc(e.user_tokens.join(", "))}</td><td><button type="button" class="btn ghost small" data-ep="${i}">Use</button></td></tr>`).join("") +
      `</table>`;
    box.querySelectorAll("[data-ep]").forEach((b) => b.addEventListener("click", () => {
      const e = eps[+b.dataset.ep];
      uaForm.url.value = e.url;
      uaForm.security.value = [...uaForm.security.options].some((o) => o.value === e.security_policy) ? e.security_policy : "None";
      if (e.security_mode !== "None") uaForm.mode.value = e.security_mode;
      uaForm.security.dispatchEvent(new Event("change"));
      box.classList.add("hidden");
    }));
  } catch (e) {
    box.innerHTML = `<p class="small" style="color:var(--err)">${esc(e.message)}</p>`;
  }
});

uaForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  await uaDisconnect(true);
  $("#uaConnect").disabled = true;
  uaStatus(`Connecting to ${esc(uaForm.url.value)}...`);
  try {
    const r = await api("api/opcua/connect", { method: "POST", body: JSON.stringify({
      url: uaForm.url.value, security: uaForm.security.value, mode: uaForm.mode.value,
      username: uaForm.username.value, password: uaForm.password.value }) });
    uaForm.password.value = "";
    ua.sid = r.sid;
    ua.url = r.url;
    uaRemember(r.url);
    uaStatus(`<span class="chip okchip">connected</span> <span class="mono">${esc(r.url)}</span> &middot; ${esc(r.server || "OPC UA server")}
      &middot; security ${esc(r.security)}${r.security !== "None" ? ` (${esc(r.mode)})` : ""} &middot; ${esc(r.user)}
      ${r.server_cert_sha1 ? `&middot; <span class="small">server cert SHA-1 <span class="mono">${esc(r.server_cert_sha1)}</span></span>` : ""}`, "");
    $("#uaMain").classList.remove("hidden");
    $("#uaEpList").classList.add("hidden");
    $("#uaConnect").classList.add("hidden");
    $("#uaDisconnect").classList.remove("hidden");
    $("#uaTree").innerHTML = "";
    await uaExpand($("#uaTree"), null);
    uaRestoreWatch();
    uaSchedule();
  } catch (e) {
    uaStatus(`Could not connect: ${esc(e.message)}`, "errtext");
  } finally {
    $("#uaConnect").disabled = false;
  }
});

async function uaDisconnect(quiet) {
  clearTimeout(ua.timer);
  if (ua.sid) {
    try { await api("api/opcua/disconnect", { method: "POST", body: JSON.stringify({ sid: ua.sid }) }); } catch (_) { /* ignore */ }
  }
  ua.sid = null;
  $("#uaConnect").classList.remove("hidden");
  $("#uaDisconnect").classList.add("hidden");
  if (!quiet) {
    $("#uaMain").classList.add("hidden");
    uaStatus("Disconnected.");
  }
}
$("#uaDisconnect").addEventListener("click", () => uaDisconnect(false));

// ------------------------------------------------------------------ tree
async function uaExpand(container, nodeId) {
  container.innerHTML = `<div class="muted small uaload">loading...</div>`;
  let kids;
  try {
    kids = await api("api/opcua/browse", { method: "POST", body: JSON.stringify({ sid: ua.sid, node_id: nodeId }) });
  } catch (e) {
    container.innerHTML = `<div class="small errtext">${esc(e.message)}</div>`;
    return false;
  }
  container.innerHTML = "";
  for (const k of kids) {
    const li = document.createElement("div");
    li.className = "uanode";
    li.innerHTML = `<div class="uarow" title="${esc(k.node_id)}"><span class="uatoggle">&#9656;</span>
      <span class="uaicon ${k.node_class === "Variable" ? "var" : "obj"}">${k.node_class === "Variable" ? "&#9679;" : "&#9632;"}</span>
      <span class="uaname">${esc(k.name)}</span></div><div class="uakids hidden"></div>`;
    const row = li.firstElementChild, kidsEl = li.lastElementChild, tog = row.firstElementChild;
    let loaded = false;
    tog.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!loaded) {
        loaded = true;
        kidsEl.classList.remove("hidden");
        tog.classList.add("open");
        await uaExpand(kidsEl, k.node_id);
        if (!kidsEl.children.length) tog.classList.add("leaf");
        return;
      }
      kidsEl.classList.toggle("hidden");
      tog.classList.toggle("open", !kidsEl.classList.contains("hidden"));
    });
    row.addEventListener("click", () => uaSelect(k, row));
    row.addEventListener("dblclick", () => { if (k.node_class === "Variable") uaAddWatch(k); else tog.click(); });
    container.appendChild(li);
  }
  if (!kids.length) container.innerHTML = "";
  return true;
}

async function uaSelect(node, row) {
  document.querySelectorAll(".uarow.sel").forEach((r) => r.classList.remove("sel"));
  row.classList.add("sel");
  ua.sel = node;
  $("#uaExport").disabled = false;
  $("#uaAddWatch").classList.toggle("hidden", node.node_class !== "Variable");
  const box = $("#uaAttrs");
  box.innerHTML = `<span class="muted">reading...</span>`;
  try {
    const a = await api("api/opcua/attributes", { method: "POST", body: JSON.stringify({ sid: ua.sid, node_id: node.node_id }) });
    const v = a.Value;
    delete a.Value;
    const rows = Object.entries(a).map(([k, val]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(fmtVal(val))}</dd>`);
    if (v) {
      rows.push(`<dt>Value</dt><dd class="mono">${esc(fmtVal(v.value))}</dd>`, `<dt>Status</dt><dd>${esc(v.status)}</dd>`,
        `<dt>Source time</dt><dd class="mono">${esc(fmtTime(v.source_time))}</dd>`);
    }
    box.innerHTML = `<dl>${rows.join("")}</dl>`;
  } catch (e) {
    box.innerHTML = `<span class="errtext">${esc(e.message)}</span>`;
  }
}
$("#uaAddWatch").addEventListener("click", () => ua.sel && uaAddWatch(ua.sel));

// ------------------------------------------------------------------ watch (data access view)
function uaWatchKey() { return `gm.ua.watch.${ua.url}`; }
function uaSaveWatch() {
  try { localStorage.setItem(uaWatchKey(), JSON.stringify([...ua.watch.values()].map((w) => ({ node_id: w.node_id, name: w.name })))); } catch (_) { /* ignore */ }
}
function uaRestoreWatch() {
  ua.watch.clear();
  try { for (const w of JSON.parse(localStorage.getItem(uaWatchKey()) || "[]")) ua.watch.set(w.node_id, { ...w }); } catch (_) { /* ignore */ }
  uaRenderWatch();
  uaPoll();
}
function uaAddWatch(node) {
  if (!ua.watch.has(node.node_id)) ua.watch.set(node.node_id, { node_id: node.node_id, name: node.name });
  uaSaveWatch();
  uaRenderWatch();
  uaPoll();
}
function uaRenderWatch() {
  const body = $("#uaWatch tbody");
  body.innerHTML = [...ua.watch.values()].map((w) => `<tr data-id="${esc(w.node_id)}">
    <td title="${esc(w.node_id)}">${esc(w.name)}</td><td class="mono">${esc(fmtVal(w.value))}</td><td>${esc(w.variant_type || "")}</td>
    <td class="${w.status && w.status !== "Good" ? "errtext" : ""}">${esc(w.status || "")}</td><td class="mono small">${esc(fmtTime(w.source_time))}</td>
    <td><button class="btn ghost small" data-unwatch="${esc(w.node_id)}" title="Remove">&times;</button></td></tr>`).join("");
  body.querySelectorAll("[data-unwatch]").forEach((b) => b.addEventListener("click", () => {
    ua.watch.delete(b.dataset.unwatch);
    uaSaveWatch();
    uaRenderWatch();
  }));
  $("#uaWatchHint").classList.toggle("hidden", ua.watch.size > 0);
}
async function uaPoll() {
  if (!ua.sid || !ua.watch.size || ua.busy) return;
  ua.busy = true;
  try {
    const vals = await api("api/opcua/read", { method: "POST", body: JSON.stringify({ sid: ua.sid, node_ids: [...ua.watch.keys()] }) });
    for (const v of vals) Object.assign(ua.watch.get(v.node_id) || {}, v);
    uaRenderWatch();
  } catch (e) {
    uaStatus(`Read failed: ${esc(e.message)}`, "errtext");
  } finally {
    ua.busy = false;
  }
}
function uaSchedule() {
  clearTimeout(ua.timer);
  const ms = +$("#uaRate").value;
  if (!ua.sid || !ms) return;
  // Pause while the tab is hidden so a forgotten browser tab doesn't keep polling the server.
  ua.timer = setTimeout(async () => {
    if (!document.hidden && !$("#tab-opcua").classList.contains("hidden")) await uaPoll();
    uaSchedule();
  }, ms);
}
$("#uaRate").addEventListener("change", uaSchedule);

// ------------------------------------------------------------------ CSV exports
function csvDownload(name, header, rows) {
  const q = (v) => `"${fmtVal(v).replace(/"/g, '""')}"`;
  const text = [header.map(q).join(","), ...rows.map((r) => r.map(q).join(","))].join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const safeName = (s) => s.replace(/[^A-Za-z0-9_.-]+/g, "_").slice(0, 60);

$("#uaWatchCsv").addEventListener("click", () => csvDownload("ghostmap-watch.csv", ["tag", "node_id", "value", "type", "status", "source_time"],
  [...ua.watch.values()].map((w) => [w.name, w.node_id, w.value, w.variant_type, w.status, w.source_time])));

$("#uaExport").addEventListener("click", async () => {
  if (!ua.sel) return;
  const btn = $("#uaExport");
  btn.disabled = true;
  btn.textContent = "Exporting...";
  try {
    const r = await api("api/opcua/export", { method: "POST", body: JSON.stringify({ sid: ua.sid, node_id: ua.sel.node_id }) });
    csvDownload(`ghostmap-tags-${safeName(ua.sel.name)}.csv`, ["path", "node_id", "type", "value", "status"],
      r.tags.map((t) => [t.path, t.node_id, t.variant_type, t.value, t.status]));
    uaStatus(`Exported ${r.tags.length} tags under ${esc(ua.sel.name)}${r.truncated ? " (stopped at the size limit)" : ""}.`);
  } catch (e) {
    uaStatus(`Export failed: ${esc(e.message)}`, "errtext");
  } finally {
    btn.disabled = false;
    btn.textContent = "Export tags";
  }
});

// Prefill: the demo's simulated server, else the last endpoint used.
(async () => {
  try {
    const info = await api("api/info");
    uaForm.url.value = info.demo_opcua || uaRecent()[0] || "";
    if (info.demo_opcua) uaStatus(`Demo: a simulated FactoryTalk Linx Gateway with two presses is running at <span class="mono">${esc(info.demo_opcua)}</span>. Press Connect.`);
  } catch (_) { /* ignore */ }
})();
