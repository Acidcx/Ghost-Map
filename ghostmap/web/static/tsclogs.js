"use strict";
// Production > Event logs: TSC's own log files on this PC (read-only), one file per day.
// Entries are "time|source|ip|pc|level|method|message" plus any lines under them (a stack trace), which fold
// away under the message. Uses $, esc and api from app.js.

const lg = { files: [], file: null, data: null, off: new Set(), seq: 0, timer: null };
const lgSize = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const lgWhen = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

function lgClass(level) {
  const l = (level || "").toLowerCase();
  if (/^(error|fatal|critical|exception)/.test(l)) return "err";
  if (/^warn/.test(l)) return "warn";
  if (/^(debug|trace|verbose)/.test(l)) return "dbg";
  return l ? "info" : "";
}

// "Bradbury.TSC.DataView" reads better as "DataView" when every source shares the prefix.
function lgPrefix(sources) {
  if (sources.length < 2) return sources.length ? sources[0].replace(/[^.]*$/, "") : "";
  let p = sources[0];
  for (const s of sources) while (!s.startsWith(p)) p = p.slice(0, -1);
  return p.slice(0, p.lastIndexOf(".") + 1);
}

// Files of the folder the open file is in, oldest to newest, for Older / Newer.
function lgDays() {
  const f = lg.files.find((x) => x.id === lg.file);
  return lg.files.filter((x) => f && x.folder === f.folder).sort((a, b) => (a.date || "").localeCompare(b.date || "") || a.modified - b.modified);
}

async function lgList() {
  const box = $("#lgText");
  try {
    const r = await api("api/tsclogs");
    lg.files = r.files;
    const groups = {};
    r.files.forEach((f) => (groups[f.folder] = groups[f.folder] || []).push(f));
    $("#lgFile").innerHTML = Object.entries(groups).map(([folder, fs]) => `<optgroup label="${esc(folder)}">${fs.map((f) =>
      `<option value="${esc(f.id)}">${esc(f.name)} (${f.date ? "" : `${lgWhen(f.modified)}, `}${lgSize(f.size)})</option>`).join("")}</optgroup>`).join("");
    if (!r.files.length) {
      $("#lgDownload").classList.add("hidden");
      $("#lgTop").innerHTML = "";
      box.textContent = r.folders.length ? "The log folders are empty." : `No TSC log folder on this PC. Looked in:\n${r.searched.join("\n")}`;
      return;
    }
    if (!r.files.some((f) => f.id === lg.file)) lg.file = r.files[0].id;
    await lgOpen(lg.file);
  } catch (e) { box.textContent = e.message; }
}

async function lgOpen(id) {
  lg.file = id;
  $("#lgFile").value = id;
  const f = lg.files.find((x) => x.id === id);
  $("#lgDate").value = f?.date || "";
  const days = lgDays(), i = days.findIndex((x) => x.id === id);
  $("#lgOlder").disabled = i <= 0;
  $("#lgNewer").disabled = i < 0 || i >= days.length - 1;
  $("#lgDownload").href = `api/tsclogs/download?id=${encodeURIComponent(id)}`;
  $("#lgDownload").classList.remove("hidden");
  await lgShow();
}

async function lgShow() {
  const id = lg.file, seq = ++lg.seq;
  if (!id) return;
  const q = $("#lgQuery").value.trim(), src = $("#lgSource").value;
  const qs = new URLSearchParams({ id, lines: $("#lgLines").value, q, source: src, hide: [...lg.off].join(",") });
  try {
    const r = await api(`api/tsclogs/file?${qs}`);
    if (seq !== lg.seq) return;  // a newer pick is loading
    lg.data = r;
    lg.filtered = Boolean(q || src || lg.off.size);
    lgRender();
  } catch (e) { if (seq === lg.seq) $("#lgText").textContent = e.message; }
}

function lgRender() {
  const filtered = lg.filtered;
  const r = lg.data, pre = lgPrefix(r.sources), short = (s) => (s && s.startsWith(pre) ? s.slice(pre.length) : s || "");
  // Level buttons with counts for the whole day; click one to hide or show it.
  const order = (l) => ["err", "warn", "info", "dbg", ""].indexOf(lgClass(l));
  $("#lgLevels").innerHTML = Object.entries(r.levels).filter(([l]) => l).sort((a, b) => order(a[0]) - order(b[0])).map(([l, n]) =>
    `<button type="button" class="lv-${lgClass(l)}${lg.off.has(l) ? " off" : ""}" data-lv="${esc(l)}"><span class="lv ${lgClass(l)}">${esc(l)}</span> ${n.toLocaleString()}</button>`).join("");
  const keep = $("#lgSource").value;
  $("#lgSource").innerHTML = `<option value="">All</option>` + r.sources.map((s) => `<option value="${esc(s)}">${esc(short(s))}</option>`).join("");
  $("#lgSource").value = r.sources.includes(keep) ? keep : "";
  $("#lgSource").closest("label").classList.toggle("hidden", !r.sources.length);

  // The messages that repeat most: usually one fault flooding the log.
  $("#lgTop").innerHTML = r.top.length ? `<details class="lgtop" ${r.top[0].count >= 10 ? "open" : ""}><summary>Most repeated (${r.top.length})</summary>
    <table>${r.top.map((t, i) => `<tr data-top="${i}" title="Show only these"><td class="n">${t.count.toLocaleString()}&times;</td>
      <td><span class="lv ${lgClass(t.level)}">${esc(t.level)}</span></td><td class="mono">${esc(short(t.source))}.${esc(t.method)}</td>
      <td>${esc(t.message)}</td><td class="muted nowrap">${esc((t.first || "").slice(11, 19))}&ndash;${esc((t.last || "").slice(11, 19))}</td></tr>`).join("")}</table></details>` : "";

  const shown = r.entries.length < r.matched ? `last ${r.entries.length.toLocaleString()} of ${r.matched.toLocaleString()}` : r.matched.toLocaleString();
  $("#lgStat").textContent = `${r.name}: ${shown} ${r.format === "tsc" ? "entries" : "lines"}${filtered ? ` that match (of ${r.total.toLocaleString()})` : ""}` +
    `${r.truncated ? ", from the last 8 MB of the file (download it for the rest)" : ""}.`;
  const box = $("#lgText");
  if (!r.entries.length) { box.innerHTML = `<p class="small muted" style="padding:8px">Nothing matches.</p>`; return; }
  const open = $("#lgExpand").checked ? " open" : "";
  const msg = (e) => (e.more.length ? `<details${open}><summary>${esc(e.message)} <span class="muted">(+${e.more.length} line${e.more.length === 1 ? "" : "s"})</span></summary><pre>${esc(e.more.join("\n"))}</pre></details>` : esc(e.message));
  if (r.format !== "tsc") {
    box.innerHTML = `<table class="lgt"><tbody>${r.entries.map((e) => `<tr class="${e.problem ? "lv-err" : ""}"><td class="msg">${msg(e)}</td></tr>`).join("")}</tbody></table>`;
  } else {
    let day = null;
    const rows = r.entries.map((e) => {
      const d = (e.time || "").slice(0, 10), cls = lgClass(e.level);
      const brk = d && d !== day && (day !== null || d !== r.date) ? `<tr class="daybreak"><td colspan="5">${esc(d)}</td></tr>` : "";
      day = d || day;
      return `${brk}<tr class="lv-${cls}" title="${esc([e.host, e.ip].filter(Boolean).join("  "))}"><td class="t">${esc((e.time || "").slice(11) || e.time || "")}</td>` +
        `<td>${e.level ? `<span class="lv">${esc(e.level)}</span>` : ""}</td><td class="s">${esc(short(e.source))}</td><td class="m">${esc(e.method || "")}</td><td class="msg">${msg(e)}</td></tr>`;
    }).join("");
    box.innerHTML = `<table class="lgt"><thead><tr><th>Time</th><th>Level</th><th>Source</th><th>Method</th><th>Message</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  box.scrollTop = box.scrollHeight;
}

$("#lgLevels").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-lv]");
  if (!b) return;
  const l = b.dataset.lv;
  lg.off.has(l) ? lg.off.delete(l) : lg.off.add(l);
  lgShow();
});
$("#lgTop").addEventListener("click", (ev) => {
  const tr = ev.target.closest("[data-top]");
  if (!tr) return;
  const t = lg.data.top[Number(tr.dataset.top)];
  $("#lgSource").value = t.source;
  $("#lgQuery").value = t.message.slice(0, 200);
  lg.off.clear();
  lgShow();
});
$("#lgFile").addEventListener("change", (e) => lgOpen(e.target.value));
$("#lgOlder").addEventListener("click", () => { const d = lgDays(), i = d.findIndex((x) => x.id === lg.file); if (i > 0) lgOpen(d[i - 1].id); });
$("#lgNewer").addEventListener("click", () => { const d = lgDays(), i = d.findIndex((x) => x.id === lg.file); if (i >= 0 && i < d.length - 1) lgOpen(d[i + 1].id); });
$("#lgDate").addEventListener("change", (e) => {
  const want = e.target.value, f = lg.files.find((x) => x.date === want);
  if (f) lgOpen(f.id);
  else { $("#lgStat").textContent = `No log file for ${want}.`; $("#lgDate").value = lg.files.find((x) => x.id === lg.file)?.date || ""; }
});
$("#lgLines").addEventListener("change", lgShow);
$("#lgSource").addEventListener("change", lgShow);
$("#lgExpand").addEventListener("change", () => lg.data && lgRender());
$("#lgQuery").addEventListener("input", () => { clearTimeout(lg.timer); lg.timer = setTimeout(lgShow, 300); });
$("#lgRefresh").addEventListener("click", lgList);
document.addEventListener("gm:tab", (e) => { if (e.detail === "tsclogs") lgList(); });
