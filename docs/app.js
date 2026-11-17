"use strict";

const state = { engines: [], engine: null, index: [], history: {}, docs: {}, selected: new Set(), cache: {}, kind: "variables" };
const vendors = () => state.engine.vendors;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const vkey = (v) => v.split(/[.-]/).map(Number);
function cmpVer(a, b) {
  const x = vkey(a), y = vkey(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0);
  }
  return 0;
}
// order: release series, then vendor (oracle first), then version
const cmpEntry = (a, b) => cmpVer(a.series, b.series) || b.vendor.localeCompare(a.vendor) || cmpVer(a.version, b.version);
const vname = (vendor) => vendors()[vendor]?.short || vendor;
const label = (e) => `${vname(e.vendor)} ${e.version}`;
const entryOf = (file) => state.index.find((e) => e.file === file);

async function load(file) {
  if (!state.cache[file]) state.cache[file] = await (await fetch(`data/${state.engine.id}/${file}`)).json();
  return state.cache[file];
}

function updateCounts() {
  document.querySelectorAll("#versions details").forEach((d) => {
    const n = d.querySelectorAll("input:checked").length;
    const c = d.querySelector(":scope > summary .count");
    c.textContent = n || "";
    c.title = n ? `${n} selected` : "";
  });
}

function renderVersions() {
  let html = "";
  for (const v of Object.keys(vendors())) {
    const items = state.index.filter((e) => e.vendor === v);
    if (!items.length) continue;
    html += `<details class="vendor" open><summary>${esc(vendors()[v].name)} <span class="count"></span></summary>`;
    const series = [...new Set(items.map((e) => e.series))].sort(cmpVer);
    for (const s of series) {
      html += `<details class="series"><summary>${esc(s)} <span class="count"></span></summary>`;
      for (const e of items.filter((i) => i.series === s).sort((a, b) => cmpVer(b.version, a.version))) {
        html += `<label><input type="checkbox" value="${esc(e.file)}"> ${esc(e.version)}</label>`;
      }
      html += `</details>`;
    }
    html += `</details>`;
  }
  $("#versions").innerHTML = html;
}

const valueOf = (rec) => (rec ? String(rec.default ?? "") : null);
const range = (ev) => (ev.from_version ? `between ${ev.from_version} and ${ev.to_version}` : `as of ${ev.to_version} or earlier`);

// Human-readable notes for one row, limited to the span of versions selected per vendor.
function notesFor(name, cols, many) {
  const notes = [];
  const kindKey = state.kind === "status" ? "status" : "variables";
  const vendors = [...new Set(cols.map((c) => c.vendor))];
  let message = null, replacement = null;
  for (const vendor of vendors) {
    const vc = cols.filter((c) => c.vendor === vendor);
    const lo = vc[0].version, hi = vc[vc.length - 1].version;
    const h = state.history[vendor]?.[kindKey]?.[name];
    if (!h) continue;
    if (h.message) { message = h.message; replacement = h.replacement || replacement; }
    const pre = many ? `${vname(vendor)}: ` : "";
    for (const ev of h.events) {
      const inSpan = cmpVer(ev.to_version, lo) > 0 && cmpVer(ev.to_version, hi) <= 0;
      if (ev.type === "deprecated" && !ev.from_version) {
        if (cmpVer(ev.to_version, hi) <= 0) notes.push(["deprecated", `${pre}Deprecated ${range(ev)}`]);
      } else if (!inSpan) continue;
      else if (ev.type === "added") notes.push(["added", `${pre}Added ${range(ev)}`]);
      else if (ev.type === "removed") notes.push(["removed", `${pre}Removed ${range(ev)}`]);
      else if (ev.type === "deprecated") notes.push(["deprecated", `${pre}Deprecated ${range(ev)}`]);
      else if (ev.type === "default") {
        notes.push(["changed", `${pre}Default changed ${range(ev)}: <code>${esc(ev.old === "" ? "(empty)" : ev.old)}</code> → <code>${esc(ev.new === "" ? "(empty)" : ev.new)}</code>`]);
      }
    }
  }
  if (replacement) notes.push(["info", `Replacement: <code>${esc(replacement)}</code>`]);
  else if (message) notes.push(["info", esc(message)]);
  return notes;
}

function docLinks(name, cols) {
  const kind = state.kind === "status" ? "status" : "variables";
  const links = [];
  for (const [v, cfg] of Object.entries(vendors())) {
    for (const c of [...cols].reverse().filter((c) => c.vendor === v)) {
      const docs = state.docs[v] || {};
      const entry = docs[c.series] || docs["*"];
      const hit = entry && entry[kind] && entry[kind][name];
      if (!hit) continue;
      const [rel, label] = Array.isArray(hit) ? hit : [hit, cfg.short];
      const url = /^https?:/.test(rel) ? rel : entry.base + rel;
      if (!links.some(([u]) => u === url)) links.push([url, label]);
      break;
    }
  }
  return links.map(([u, l]) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(l)} docs</a>`).join(" · ");
}

async function compute() {
  const cols = [...state.selected].map(entryOf).sort(cmpEntry);
  const datasets = await Promise.all(cols.map((c) => load(c.file)));
  const hideEnv = $("#novolatile").checked;
  const cats = new Set([...document.querySelectorAll("[data-cat]")].filter((c) => c.checked).map((c) => c.dataset.cat));
  const showOpts = $("#showopts").checked;
  const q = $("#search").value.trim().toLowerCase();
  const many = new Set(cols.map((c) => c.vendor)).size > 1;

  const names = new Set();
  const recs = datasets.map((d) => {
    if (state.kind === "status") {
      const m = {};
      d.status.forEach((n) => (m[n.toLowerCase()] = { default: "", orig: n }));
      return m;
    }
    return d.variables;
  });
  recs.forEach((r) => Object.keys(r).forEach((n) => names.add(n)));

  const rows = [];
  for (const name of [...names].sort()) {
    if (q && !name.includes(q)) continue;
    const base = recs[0][name];
    const cells = recs.map((r) => r[name]);
    if (hideEnv && cells.some((c) => c && (c.default_source === "runtime" || c.default_source === "derived"))) continue;
    if (state.kind === "variables" && !showOpts && cells.every((c) => !c || c.option_only)) continue;
    const present = cells.map(Boolean);
    const tags = [];
    if (!base && present.some(Boolean)) tags.push("added");
    else if (base && present.some((p) => !p)) tags.push("removed");
    else if (state.kind === "variables" && cells.some((c) => valueOf(c) !== valueOf(base))) tags.push("changed");
    if (state.kind === "variables" && cells.some((c) => c && c.deprecated) && !(base && base.deprecated)) tags.push("deprecated");
    if (!tags.some((t) => cats.has(t))) continue;
    const desc = (cells.find((c) => c && c.desc) || {}).desc || "";
    const orig = (cells.find((c) => c && c.orig) || {}).orig;
    rows.push({ name, orig, tags, cells, base, desc, notes: notesFor(name, cols, many) });
  }
  return { cols, rows };
}

const stripTags = (h) => h.replace(/<[^>]+>/g, "");

async function render() {
  const n = state.selected.size;
  $("#toolbar").hidden = n < 2;
  if (n < 2) {
    $("#summary").textContent = "";
    $("#results").innerHTML = '<p class="empty">Select two or more versions to compare.</p>';
    return;
  }
  const { cols, rows } = await compute();
  state.last = { cols, rows };
  const count = (c) => rows.filter((r) => r.tags.includes(c)).length;
  const stat = (k, label) => `<div class="stat ${k}"><span class="n">${count(k)}</span><span class="l">${label}</span></div>`;
  $("#summary").innerHTML = `<div class="meta">Baseline <b>${esc(label(cols[0]))}</b> · ${rows.length} rows</div><div class="stats">` +
    stat("added", "added") + stat("removed", "removed") +
    (state.kind === "variables" ? (state.engine.deprecations === false ? "" : stat("deprecated", "deprecated")) + stat("changed", "default changed") : "") + "</div>";

  const head = cols.map((c, i) => `<th>${esc(label(c))}${i === 0 ? "<small>baseline</small>" : ""}</th>`).join("");
  const body = rows.map((r) => {
    const tds = r.cells.map((c, i) => {
      if (!c) return `<td class="val missing">absent</td>`;
      const v = state.kind === "status" ? "present" : valueOf(c);
      let cls = "val";
      if (i > 0) cls += !r.base ? " new" : v !== valueOf(r.base) ? " diff" : "";
      const dep = c.deprecated ? ` <span class="tag deprecated" title="${esc(c.deprecated)}">deprecated</span>` : "";
      return `<td class="${cls}">${esc(v === "" ? "(empty)" : v)}${dep}</td>`;
    }).join("");
    const optOnly = state.kind === "variables" && r.cells.every((c) => !c || c.option_only);
    const tags = (optOnly ? '<span class="tag option" title="Not a system variable: only available as a mysqld startup/plugin option">startup option</span> ' : "") +
      r.tags.map((t) => `<span class="tag ${t}">${t === "changed" ? "default changed" : t}</span>`).join(" ");
    const notes = r.notes.map(([k, t]) => `<li class="${k}">${t}</li>`).join("");
    return `<tr><td class="name">${esc(r.orig || r.name)}<br>${tags}` +
      (r.desc ? `<div class="desc">${esc(r.desc.slice(0, 200))}</div>` : "") + `</td>${tds}` +
      `<td class="notes"><ul>${notes}</ul><div class="links">${docLinks(r.name, cols)}</div></td></tr>`;
  }).join("");
  $("#results").innerHTML = rows.length
    ? `<div class="scroll"><table><thead><tr><th>Name</th>${head}<th>Notes</th></tr></thead><tbody>${body}</tbody></table></div>`
    : '<p class="empty">No differences match the current filters.</p>';
}

function exportCsv() {
  if (!state.last) return;
  const { cols, rows } = state.last;
  const q = (s) => `"${String(s ?? "").replace(/"/g, '""')}"`;
  const lines = [["name", "tags", ...cols.map(label), "notes"].map(q).join(",")];
  for (const r of rows) {
    const vals = r.cells.map((c) => (c ? (state.kind === "status" ? "present" : valueOf(c)) : "absent"));
    lines.push([r.orig || r.name, r.tags.join("|"), ...vals, r.notes.map(([, t]) => stripTags(t)).join(" | ")].map(q).join(","));
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  a.download = `configdelta-${state.engine.id}-${state.kind}.csv`;
  a.click();
}

async function loadEngine(id) {
  state.engine = state.engines.find((e) => e.id === id) || state.engines[0];
  try { localStorage.setItem("engine", state.engine.id); } catch (e) {}
  const base = `data/${state.engine.id}/`;
  [state.index, state.history, state.docs] = await Promise.all([
    fetch(base + "index.json", { cache: "no-store" }).then((r) => r.json()),
    fetch(base + "history.json", { cache: "no-store" }).then((r) => r.json()),
    fetch(base + "doclinks.json").then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
  ]);
  state.cache = {};
  state.selected.clear();
  state.kind = state.engine.kinds[0].id;
  const hasDep = state.engine.deprecations !== false;
  document.querySelector('[data-cat="deprecated"]').closest("label").hidden = !hasDep;
  $(".kinds").hidden = state.engine.kinds.length < 2;
  $("#showopts-label").hidden = !state.engine.optionOnly;
  $(".kinds").innerHTML = state.engine.kinds.map((k) => `<button data-kind="${esc(k.id)}" type="button"${k.id === state.kind ? ' class="active"' : ""}>${esc(k.label)}</button>`).join("");
  document.querySelectorAll(".kinds button").forEach((b) => (b.onclick = () => {
    state.kind = b.dataset.kind;
    document.querySelectorAll(".kinds button").forEach((x) => x.classList.toggle("active", x === b));
    render();
  }));
  renderVersions();
  render();
}

async function init() {
  state.engines = await (await fetch("data/engines.json", { cache: "no-store" })).json();
  const sel = $("#engine");
  sel.innerHTML = state.engines.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("");
  let saved = "";
  try { saved = localStorage.getItem("engine") || ""; } catch (e) {}
  if (state.engines.some((e) => e.id === saved)) sel.value = saved;
  sel.onchange = () => loadEngine(sel.value);
  await loadEngine(sel.value);
  $("#versions").addEventListener("change", (e) => {
    e.target.checked ? state.selected.add(e.target.value) : state.selected.delete(e.target.value);
    updateCounts();
    render();
  });
  $("#clear").onclick = () => {
    state.selected.clear();
    document.querySelectorAll("#versions input").forEach((i) => (i.checked = false));
    updateCounts();
    render();
  };
  const setAll = (open) => document.querySelectorAll("#versions details").forEach((d) => (d.open = open));
  $("#expand").onclick = () => setAll(true);
  $("#collapse").onclick = () => setAll(false);
  $("#toolbar").addEventListener("change", render);
  $("#search").addEventListener("input", render);
  $("#csv").onclick = exportCsv;
  // The update API only exists behind `python3 server.py`; on a static host (GitHub Pages) there is nothing to call.
  fetch("api/status", { cache: "no-store" })
    .then((r) => r.ok && r.headers.get("content-type").includes("json"))
    .catch(() => false)
    .then((ok) => { if (!ok) $("#update").hidden = true; });
}
let toastTimer;
function toast(msg, { ms = 4000, error = false } = {}) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", error);
  t.classList.add("show");
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

async function reloadVersions() {
  const base = `data/${state.engine.id}/`;
  state.index = await (await fetch(base + "index.json", { cache: "no-store" })).json();
  state.history = await (await fetch(base + "history.json", { cache: "no-store" })).json();
  state.docs = await fetch(base + "doclinks.json").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
  state.cache = {};
  const open = new Set([...document.querySelectorAll("#versions details[open] > summary")].map((s) => s.parentElement.className + s.firstChild.textContent.trim()));
  renderVersions();
  document.querySelectorAll("#versions details").forEach((d) => {
    d.open = open.has(d.className + d.querySelector("summary").firstChild.textContent.trim());
  });
  document.querySelectorAll("#versions input").forEach((i) => (i.checked = state.selected.has(i.value)));
  updateCounts();
  render();
}

let reportTimer;
// Bottom-right card for the Update button: live progress while running, then the list of added releases (or "up to date").
function showReport({ cls = "", title, note = "", items = [], autoHide = 0 }) {
  const el = $("#update-report");
  clearTimeout(reportTimer);
  el.className = cls;
  el.innerHTML = `<header><span>${esc(title)}</span><button type="button" aria-label="Dismiss">×</button></header>` +
    (note ? `<p>${esc(note)}</p>` : "") +
    (items.length ? `<ul>${items.map((i) => `<li class="${i.cls || ""}">${esc(i.text)}${i.tag ? `<small>${esc(i.tag)}</small>` : ""}</li>`).join("")}</ul>` : "");
  el.querySelector("button").onclick = () => (el.hidden = true);
  el.hidden = false;
  if (autoHide) reportTimer = setTimeout(() => (el.hidden = true), autoHide);
}
const releaseLabel = (s) => { const [v, ...rest] = s.split(" "); return `${vname(v)} ${rest.join(" ")}`; };

async function runUpdate() {
  const btn = $("#update");
  btn.disabled = true;
  btn.querySelector("span").textContent = "Updating…";
  const engineName = state.engine.name;
  showReport({ title: `Updating ${engineName}`, note: "Checking Docker Hub for new releases…" });
  try {
    const post = await fetch(`api/update?engine=${encodeURIComponent(state.engine.id)}`, { method: "POST" });
    if (!post.ok) throw new Error("update API not available — start the app with: python3 server.py");
    let s;
    do {
      await new Promise((r) => setTimeout(r, 1500));
      s = await (await fetch("api/status", { cache: "no-store" })).json();
      if (s.state === "extracting") {
        const items = s.found.map((f) => s.done.includes(f) ? { cls: "ok", text: releaseLabel(f) } : s.failed.includes(f) ? { cls: "fail", text: releaseLabel(f) } : { text: releaseLabel(f), tag: "pending" });
        showReport({ title: `Updating ${engineName}`, note: s.message, items });
      } else if (s.state === "checking") showReport({ title: `Updating ${engineName}`, note: s.message });
    } while (["checking", "extracting"].includes(s.state));
    if (s.state === "uptodate") showReport({ cls: "done", title: "Up to date", note: `No new ${engineName} releases found.`, autoHide: 10000 });
    else if (s.state === "error") showReport({ cls: "error", title: "Update failed", note: s.message });
    else {
      await reloadVersions();
      const items = [...s.done.map((f) => ({ cls: "ok", text: releaseLabel(f) })), ...s.failed.map((f) => ({ cls: "fail", text: releaseLabel(f), tag: "failed" }))];
      showReport({ cls: s.done.length ? "done" : "error", title: s.done.length ? `${s.done.length} new release${s.done.length > 1 ? "s" : ""} added` : "No releases added", note: s.failed.length ? `${s.failed.length} failed — see extractor/failures/` : "", items });
    }
  } catch (e) {
    showReport({ cls: "error", title: "Update failed", note: e.message });
  } finally {
    btn.disabled = false;
    btn.querySelector("span").textContent = "Update";
  }
}

function initTheme() {
  const root = document.documentElement;
  const apply = (choice) => {
    if (choice === "system") delete root.dataset.theme; else root.dataset.theme = choice;
    document.querySelectorAll("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.themeChoice === choice));
  };
  let saved = "system";
  try { saved = localStorage.getItem("theme") || "system"; } catch (e) {}
  apply(saved);
  document.querySelectorAll("[data-theme-choice]").forEach((b) => (b.onclick = () => {
    apply(b.dataset.themeChoice);
    try { b.dataset.themeChoice === "system" ? localStorage.removeItem("theme") : localStorage.setItem("theme", b.dataset.themeChoice); } catch (e) {}
  }));
}
function initResizer() {
  const MIN = 140, MAX = 480, root = document.documentElement, handle = $("#resizer");
  const set = (w, save) => {
    w = Math.max(MIN, Math.min(MAX, Math.round(w)));
    root.style.setProperty("--side", w + "px");
    handle.setAttribute("aria-valuenow", w);
    if (save) try { localStorage.setItem("sidebarWidth", w); } catch (e) {}
    return w;
  };
  let w = 220;
  try { w = Number(localStorage.getItem("sidebarWidth")) || w; } catch (e) {}
  w = set(w);
  handle.setAttribute("aria-valuemin", MIN);
  handle.setAttribute("aria-valuemax", MAX);
  handle.onpointerdown = (e) => {
    const startX = e.clientX, startW = w;
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    document.body.classList.add("resizing");
    handle.onpointermove = (m) => { w = set(startW + m.clientX - startX); };
    handle.onpointerup = () => {
      handle.onpointermove = handle.onpointerup = null;
      handle.classList.remove("dragging");
      document.body.classList.remove("resizing");
      set(w, true);
    };
  };
  handle.ondblclick = () => { w = set(220, true); };
  handle.onkeydown = (e) => {
    if (e.key === "ArrowLeft") w = set(w - 20, true);
    else if (e.key === "ArrowRight") w = set(w + 20, true);
    else return;
    e.preventDefault();
  };
}
initTheme();
initResizer();
$("#update").onclick = runUpdate;
init().catch((e) => ($("#versions").textContent = "Failed to load data: " + e.message));
