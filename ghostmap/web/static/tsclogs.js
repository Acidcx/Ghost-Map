"use strict";
// Production > Event logs: TSC's own log files on this PC (read-only). Uses $, esc and api from app.js.

const lg = { files: [], seq: 0 };
const lgSize = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const lgWhen = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

async function lgList() {
  const box = $("#lgText");
  try {
    const r = await api("api/tsclogs");
    lg.files = r.files;
    const keep = $("#lgFile").value;
    const groups = {};
    r.files.forEach((f) => (groups[f.folder] = groups[f.folder] || []).push(f));
    $("#lgFile").innerHTML = Object.entries(groups).map(([folder, fs]) => `<optgroup label="${esc(folder)}">${fs.map((f) =>
      `<option value="${esc(f.id)}">${esc(f.name)} (${lgWhen(f.modified)}, ${lgSize(f.size)})</option>`).join("")}</optgroup>`).join("");
    if (!r.files.length) {
      $("#lgDownload").classList.add("hidden");
      box.textContent = r.folders.length ? "The log folders are empty."
        : `No TSC log folder on this PC. Looked in:\n${r.searched.join("\n")}`;
      return;
    }
    if (r.files.some((f) => f.id === keep)) $("#lgFile").value = keep;
    await lgShow();
  } catch (e) { box.textContent = e.message; }
}

async function lgShow() {
  const id = $("#lgFile").value, box = $("#lgText"), seq = ++lg.seq;
  if (!id) return;
  const q = $("#lgQuery").value.trim();
  const qs = `id=${encodeURIComponent(id)}&lines=${$("#lgLines").value}&q=${encodeURIComponent(q)}&problems=${$("#lgProblems").checked}`;
  $("#lgDownload").href = `api/tsclogs/download?id=${encodeURIComponent(id)}`;
  $("#lgDownload").classList.remove("hidden");
  try {
    const r = await api(`api/tsclogs/file?${qs}`);
    if (seq !== lg.seq) return;  // a newer pick is loading
    const shown = r.lines.length < r.matched ? `last ${r.lines.length} of ${r.matched} lines` : `${r.matched} line${r.matched === 1 ? "" : "s"}`;
    $("#lgStat").textContent = `${r.name}: ${shown}${q || $("#lgProblems").checked ? " that match" : ""}` +
      `${r.truncated ? " (from the last 8 MB of the file; download it for the rest)" : ""}. Lines with errors are in red.`;
    box.innerHTML = r.lines.length ? r.lines.map((l) => (l.problem ? `<span class="bad">${esc(l.text)}</span>` : esc(l.text))).join("\n")
      : "Nothing matches.";
    box.scrollTop = box.scrollHeight;
  } catch (e) { if (seq === lg.seq) box.textContent = e.message; }
}

let lgTimer = null;
$("#lgFile").addEventListener("change", lgShow);
$("#lgLines").addEventListener("change", lgShow);
$("#lgProblems").addEventListener("change", lgShow);
$("#lgQuery").addEventListener("input", () => { clearTimeout(lgTimer); lgTimer = setTimeout(lgShow, 300); });
$("#lgRefresh").addEventListener("click", lgList);
document.addEventListener("gm:tab", (e) => { if (e.detail === "tsclogs") lgList(); });
