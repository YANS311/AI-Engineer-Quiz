/* Keepstack single-page client. Vanilla JS, no build step. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const PAGE_SIZE = 12;
const state = {
  token: localStorage.getItem("keepstack_token"),
  user: null,
  q: "",
  mode: "keyword",
  media: null,
  includeTags: [],
  excludeTags: [],
  sort: "newest",
  collection: null,
  page: 1,
  pageSize: PAGE_SIZE,
  total: 0,
  collectionsMap: {},
  allTags: [],
};

// ---------- API helper ----------
async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  if (opts.json) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { logout(); throw new Error("unauthorised"); }
  if (!res.ok) { const t = await res.text(); throw new Error(t || res.statusText); }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
}

function toast(msg, kind = "ok") {
  const t = $("#toast"); t.textContent = msg; t.className = "toast " + kind;
  setTimeout(() => t.classList.add("hidden"), 2600);
}
const fmtBytes = (b) => { if (!b) return "0 B"; const u = ["B","KB","MB","GB","TB"]; const i = Math.floor(Math.log(b)/Math.log(1024)); return (b/Math.pow(1024,i)).toFixed(i?1:0)+" "+u[i]; };
const esc = (s) => (s ?? "").toString().replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// ---------- auth ----------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/auth/login", { json: { username: $("#login-user").value, password: $("#login-pass").value } });
    state.token = r.token; localStorage.setItem("keepstack_token", r.token);
    state.user = r.user; enterApp();
  } catch { $("#login-error").textContent = "Invalid credentials"; $("#login-error").classList.remove("hidden"); }
});
$("#btn-logout").addEventListener("click", logout);
function logout() { localStorage.removeItem("keepstack_token"); state.token = null; state.user = null;
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }

async function enterApp() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  $("#user-label").textContent = `${state.user.username} (${state.user.role})`;
  const isAdmin = state.user.role === "admin";
  $$(".admin-only").forEach(el => el.classList.toggle("hidden", !isAdmin));
  await loadFilterOptions();
  loadLibrary();
}

// ---------- navigation ----------
$$(".nav-item").forEach(btn => btn.addEventListener("click", () => {
  $$(".nav-item").forEach(b => b.classList.remove("active")); btn.classList.add("active");
  const view = btn.dataset.view;
  $$(".view").forEach(v => v.classList.add("hidden"));
  $("#view-" + (view === "library" ? "library" : view)).classList.remove("hidden");
  if (view === "library") { loadFilterOptions(); loadLibrary(); }
  if (view === "collections") loadCollections();
  if (view === "dashboard") loadDashboard();
  if (view === "users") loadUsers();
  if (view === "audit") loadAudit();
}));

// ---------- library / search / filters ----------
let searchTimer;
let latestSearchSeq = 0;
let currentSearchController = null;

function parseTagInput(raw) {
  return (raw || "")
    .split(",")
    .map(s => s.trim().toLowerCase())
    .filter(Boolean);
}

function addIncludeTags(raw) {
  const tags = Array.isArray(raw) ? raw : parseTagInput(raw);
  let changed = false;
  for (const t of tags) {
    if (!state.includeTags.includes(t)) {
      state.includeTags.push(t);
      changed = true;
    }
  }
  if (changed) resetPageAndLoad();
}

function removeIncludeTag(tag) {
  const t = (tag || "").trim().toLowerCase();
  const next = state.includeTags.filter(x => x !== t);
  if (next.length !== state.includeTags.length) {
    state.includeTags = next;
    resetPageAndLoad();
  }
}

function addExcludeTags(raw) {
  const tags = Array.isArray(raw) ? raw : parseTagInput(raw);
  let changed = false;
  for (const t of tags) {
    if (!state.excludeTags.includes(t)) {
      state.excludeTags.push(t);
      changed = true;
    }
  }
  if (changed) resetPageAndLoad();
}

function removeExcludeTag(tag) {
  const t = (tag || "").trim().toLowerCase();
  const next = state.excludeTags.filter(x => x !== t);
  if (next.length !== state.excludeTags.length) {
    state.excludeTags = next;
    resetPageAndLoad();
  }
}

function clearAllFilters() {
  state.q = "";
  state.media = null;
  state.collection = null;
  state.includeTags = [];
  state.excludeTags = [];
  $("#search").value = "";
  resetPageAndLoad();
}

function resetPageAndLoad() {
  state.page = 1;
  loadLibrary();
}

function hasActiveFilters() {
  return Boolean(
    (state.q && state.q.trim()) ||
    state.collection ||
    state.media ||
    state.includeTags.length > 0 ||
    state.excludeTags.length > 0
  );
}

function buildFilterParams() {
  const p = new URLSearchParams();
  if (state.q && state.q.trim()) p.set("q", state.q);
  p.set("sort", state.sort);
  if (state.media) p.set("media_type", state.media);
  if (state.collection) p.set("collection_id", String(state.collection));
  state.includeTags.forEach(t => p.append("include_tags", t));
  state.excludeTags.forEach(t => p.append("exclude_tags", t));
  return p;
}

async function loadFilterOptions() {
  try {
    const [colData, tagData] = await Promise.all([
      api("/api/collections"),
      api("/api/tags"),
    ]);
    const cols = colData.collections || [];
    state.collectionsMap = {};
    cols.forEach(c => { state.collectionsMap[String(c.id)] = c.name; });

    const colSelect = $("#filter-collection");
    if (colSelect) {
      colSelect.innerHTML = `<option value="">All collections</option>` +
        cols.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
      colSelect.value = state.collection ? String(state.collection) : "";
    }

    const tags = (tagData.tags || []).map(t => t.name);
    state.allTags = tags;
    const incSelect = $("#include-tag-select");
    if (incSelect) {
      incSelect.innerHTML = `<option value="">+ Pick tag...</option>` +
        tags.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
    }
    const excSelect = $("#exclude-tag-select");
    if (excSelect) {
      excSelect.innerHTML = `<option value="">- Pick tag...</option>` +
        tags.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
    }
    const dl = $("#all-tags-datalist");
    if (dl) {
      dl.innerHTML = tags.map(t => `<option value="${esc(t)}"></option>`).join("");
    }
  } catch {
    // Non-fatal if filter option fetch fails; main search handles errors.
  }
}

function syncFilterControls() {
  const colSelect = $("#filter-collection");
  if (colSelect) colSelect.value = state.collection ? String(state.collection) : "";
  const mediaSelect = $("#filter-media");
  if (mediaSelect) mediaSelect.value = state.media || "";
  const sortSelect = $("#sort");
  if (sortSelect) sortSelect.value = state.sort;
  if ($("#search") && $("#search").value !== state.q) {
    $("#search").value = state.q;
  }
}

function renderActiveFilters() {
  const wrap = $("#active-filters");
  const list = $("#active-filter-chips");
  if (!wrap || !list) return;

  if (!hasActiveFilters()) {
    wrap.classList.add("hidden");
    list.innerHTML = "";
    return;
  }
  wrap.classList.remove("hidden");

  const chips = [];
  if (state.q && state.q.trim()) {
    chips.push(`<span class="chip" data-kind="q" title="Remove keyword filter">Keyword: "${esc(state.q.trim())}"</span>`);
  }
  if (state.collection) {
    const cname = state.collectionsMap[String(state.collection)] || `#${state.collection}`;
    chips.push(`<span class="chip" data-kind="collection" title="Remove collection filter">Collection: ${esc(cname)}</span>`);
  }
  if (state.media) {
    chips.push(`<span class="chip" data-kind="media" title="Remove media type filter">Type: ${esc(state.media)}</span>`);
  }
  for (const t of state.includeTags) {
    chips.push(`<span class="chip" data-kind="inc" data-tag="${esc(t)}" title="Remove include tag">Include: #${esc(t)}</span>`);
  }
  for (const t of state.excludeTags) {
    chips.push(`<span class="chip chip-exclude" data-kind="exc" data-tag="${esc(t)}" title="Remove exclude tag">Exclude: #${esc(t)}</span>`);
  }
  const conflict = state.includeTags.filter(t => state.excludeTags.includes(t));
  if (conflict.length > 0) {
    chips.push(`<span class="chip chip-warn">Conflict on #${esc(conflict.join(", #"))} (returns empty)</span>`);
  }
  list.innerHTML = chips.join("");

  $$("#active-filter-chips .chip").forEach(chip => {
    const kind = chip.dataset.kind;
    if (!kind) return;
    chip.addEventListener("click", () => {
      if (kind === "q") { state.q = ""; $("#search").value = ""; resetPageAndLoad(); }
      else if (kind === "collection") { state.collection = null; resetPageAndLoad(); }
      else if (kind === "media") { state.media = null; resetPageAndLoad(); }
      else if (kind === "inc") { removeIncludeTag(chip.dataset.tag); }
      else if (kind === "exc") { removeExcludeTag(chip.dataset.tag); }
    });
  });
}

$("#search").addEventListener("input", (e) => {
  state.q = e.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => resetPageAndLoad(), 220);
});
$("#search-mode")?.addEventListener("change", (e) => { state.mode = e.target.value; resetPageAndLoad(); });
$("#sort").addEventListener("change", (e) => { state.sort = e.target.value; resetPageAndLoad(); });

$("#filter-collection")?.addEventListener("change", (e) => {
  state.collection = e.target.value ? e.target.value : null;
  resetPageAndLoad();
});
$("#filter-media")?.addEventListener("change", (e) => {
  state.media = e.target.value ? e.target.value : null;
  resetPageAndLoad();
});

$("#include-tag-select")?.addEventListener("change", (e) => {
  if (e.target.value) {
    addIncludeTags(e.target.value);
    e.target.value = "";
  }
});
$("#exclude-tag-select")?.addEventListener("change", (e) => {
  if (e.target.value) {
    addExcludeTags(e.target.value);
    e.target.value = "";
  }
});

$("#include-tag-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    if (e.target.value.trim()) {
      addIncludeTags(e.target.value);
      e.target.value = "";
    }
  }
});
$("#btn-add-include-tag")?.addEventListener("click", () => {
  const input = $("#include-tag-input");
  if (input && input.value.trim()) {
    addIncludeTags(input.value);
    input.value = "";
  }
});

$("#exclude-tag-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    if (e.target.value.trim()) {
      addExcludeTags(e.target.value);
      e.target.value = "";
    }
  }
});
$("#btn-add-exclude-tag")?.addEventListener("click", () => {
  const input = $("#exclude-tag-input");
  if (input && input.value.trim()) {
    addExcludeTags(input.value);
    input.value = "";
  }
});

$("#btn-clear-filters")?.addEventListener("click", clearAllFilters);
$("#btn-export-csv")?.addEventListener("click", exportCsv);

$("#btn-prev")?.addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadLibrary();
  }
});
$("#btn-next")?.addEventListener("click", () => {
  const totalPages = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
  if (state.page < totalPages) {
    state.page += 1;
    loadLibrary();
  }
});

async function loadLibrary() {
  const seq = ++latestSearchSeq;
  if (currentSearchController) {
    currentSearchController.abort();
  }
  const controller = new AbortController();
  currentSearchController = controller;

  syncFilterControls();
  renderActiveFilters();

  const p = buildFilterParams();
  p.set("mode", state.mode);
  p.set("limit", String(state.pageSize));
  const offset = Math.max(0, (state.page - 1) * state.pageSize);
  p.set("offset", String(offset));

  try {
    const data = await api("/api/assets?" + p.toString(), { signal: controller.signal });
    if (seq !== latestSearchSeq) return;

    const total = data.total || 0;
    const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
    if (state.page > totalPages && total > 0) {
      state.page = totalPages;
      return loadLibrary();
    }

    state.total = total;
    renderFacets(data.facets || {});
    renderGrid(data.items || [], total);
    renderPagination(total);
  } catch (err) {
    if (err.name === "AbortError" || seq !== latestSearchSeq) return;
    $("#grid").innerHTML = "";
    $("#empty").textContent = "Failed to load assets: " + (err.message || "Request error");
    $("#empty").classList.remove("hidden");
    renderPagination(0);
    toast("Failed to load assets", "err");
  }
}

function renderGrid(items, total) {
  $("#result-title").textContent = hasActiveFilters() ? `Matching Results (${total})` : `Library (${total})`;
  const emptyEl = $("#empty");
  emptyEl.classList.toggle("hidden", items.length > 0);
  if (items.length === 0) {
    emptyEl.textContent = hasActiveFilters()
      ? "No matching assets found for current filters."
      : "No assets yet. Click Upload to get started.";
  }
  $("#grid").innerHTML = items.map(a => `
    <div class="card" data-uuid="${a.uuid}">
      <img class="thumb" loading="lazy" src="/api/assets/${a.uuid}/thumb" alt="${esc(a.alt_text || a.title)}">
      <div class="meta">
        <span class="title" title="${esc(a.title)}">${esc(a.title)}</span>
        <span class="sub"><span class="badge">${esc(a.media_type)}</span><span>${(a.created_at || "").slice(0, 10)} &middot; ${fmtBytes(a.size)}</span></span>
      </div>
    </div>`).join("");
  $$("#grid .card").forEach(c => c.addEventListener("click", () => openAsset(c.dataset.uuid)));
}

function renderPagination(total) {
  const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
  const curPage = total === 0 ? 1 : Math.min(state.page, totalPages);
  if ($("#page-total")) $("#page-total").textContent = `Total: ${total} matching asset${total === 1 ? "" : "s"}`;
  if ($("#page-info")) $("#page-info").textContent = `Page ${curPage} of ${totalPages}`;
  if ($("#btn-prev")) $("#btn-prev").disabled = total === 0 || curPage <= 1;
  if ($("#btn-next")) $("#btn-next").disabled = total === 0 || curPage >= totalPages;
}

function renderFacets(facets) {
  $("#facet-media").innerHTML = (facets.media_types || []).map(m =>
    `<li class="${state.media === m.value ? 'active' : ''}" data-media="${esc(m.value)}"><span>${esc(m.value)}</span><span class="count" title="Catalog total">${m.count}</span></li>`).join("");
  $("#facet-tags").innerHTML = (facets.tags || []).slice(0, 18).map(t => {
    const isInc = state.includeTags.includes(t.value);
    const isExc = state.excludeTags.includes(t.value);
    const cls = isInc ? "active" : (isExc ? "active-exc" : "");
    return `<li class="${cls}" data-tag="${esc(t.value)}">
      <span class="facet-tag-name" title="Click to toggle include">${esc(t.value)}</span>
      <span class="facet-tag-right">
        <span class="count" title="Catalog total">${t.count}</span>
        <button type="button" class="facet-btn inc" data-act="inc" data-tag="${esc(t.value)}" title="Toggle include tag">+</button>
        <button type="button" class="facet-btn exc" data-act="exc" data-tag="${esc(t.value)}" title="Toggle exclude tag">&minus;</button>
      </span>
    </li>`;
  }).join("");

  $$("#facet-media li").forEach(li => li.addEventListener("click", () => {
    state.media = state.media === li.dataset.media ? null : li.dataset.media;
    resetPageAndLoad();
  }));

  $$("#facet-tags li").forEach(li => {
    const tag = li.dataset.tag;
    li.querySelector(".facet-tag-name")?.addEventListener("click", () => {
      if (state.includeTags.includes(tag)) removeIncludeTag(tag);
      else addIncludeTags([tag]);
    });
    li.querySelector('button[data-act="inc"]')?.addEventListener("click", (e) => {
      e.stopPropagation();
      if (state.includeTags.includes(tag)) removeIncludeTag(tag);
      else addIncludeTags([tag]);
    });
    li.querySelector('button[data-act="exc"]')?.addEventListener("click", (e) => {
      e.stopPropagation();
      if (state.excludeTags.includes(tag)) removeExcludeTag(tag);
      else addExcludeTags([tag]);
    });
  });
}

async function exportCsv() {
  const btn = $("#btn-export-csv");
  if (btn) { btn.disabled = true; btn.textContent = "Exporting..."; }
  try {
    const p = buildFilterParams();
    const headers = {};
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    const res = await fetch("/api/assets/export?" + p.toString(), { headers });
    if (res.status === 401) { logout(); throw new Error("Unauthorised"); }
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(errText || res.statusText || "Export failed");
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "assets_export.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("CSV exported");
  } catch (err) {
    toast("Export failed: " + (err.message || "Network error"), "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Export CSV"; }
  }
}

// ---------- asset detail drawer ----------
const drawer = $("#drawer");
$("#drawer-close").addEventListener("click", closeDrawer);
$(".drawer-backdrop").addEventListener("click", closeDrawer);
function closeDrawer() { drawer.classList.add("hidden"); document.body.classList.remove("modal-open"); }

async function openAsset(uuid) {
  drawer.classList.remove("hidden");
  document.body.classList.add("modal-open");
  $("#drawer-body").innerHTML = '<p class="muted">Loading...</p>';
  const [d, meta, sim] = await Promise.all([
    api("/api/assets/" + uuid),
    api("/api/assets/" + uuid + "/metadata"),
    api("/api/assets/" + uuid + "/similar"),
  ]);
  const a = d.asset;
  const canEdit = ["admin","editor","contributor"].includes(state.user.role);
  const preview = a.media_type === "image"
    ? `<img class="detail-preview" src="/api/assets/${uuid}/preview" alt="${esc(a.alt_text||a.title)}">`
    : `<img class="detail-preview" src="/api/assets/${uuid}/thumb" alt="${esc(a.title)}">`;
  const dc = a.dublin_core || {};
  $("#drawer-body").innerHTML = `
    ${preview}
    <h2 class="detail-title">${esc(a.title)}</h2>
    <p class="muted">${a.media_type} &middot; ${a.ext?.toUpperCase()} &middot; ${fmtBytes(a.size)} ${a.width?`&middot; ${a.width}&times;${a.height}`:""}</p>
    <div class="btn-row">
      <a class="btn" href="/api/assets/${uuid}/file" download>Download</a>
      ${canEdit?`<button class="btn" id="d-share">Create share link</button>`:""}
      ${["admin","editor"].includes(state.user.role)?`<button class="btn btn-danger" id="d-delete">Delete</button>`:""}
    </div>

    ${canEdit ? `<div class="detail-section"><h4>Edit metadata</h4>
      <div class="edit-grid">
        <label>Title<input id="e-title" value="${esc(a.title)}"></label>
        <label>Description<textarea id="e-description" rows="2">${esc(a.description)}</textarea></label>
        <label>Alt text (accessibility)<input id="e-alt_text" value="${esc(a.alt_text)}"></label>
        <label>Creator<input id="e-dc_creator" value="${esc(a.dc_creator)}"></label>
        <label>Rights / license<input id="e-rights_statement" value="${esc(a.rights_statement)}"></label>
        <label>Retention until<input id="e-retention_until" type="date" value="${esc((a.retention_until||'').slice(0,10))}"></label>
      </div>
      <div class="btn-row"><button class="btn btn-primary" id="d-save">Save changes</button></div></div>` : ""}

    <div class="detail-section"><h4>Tags</h4>
      <div class="tag-row" id="tag-row">
        ${d.tags.map(t => `<span class="tag ${t.source==='ai'?'ai':''}">${esc(t.name)}${canEdit?`<span class="rm" data-tag="${esc(t.name)}">&times;</span>`:""}</span>`).join("")}
        ${canEdit?`<input class="tag-add" id="tag-add" placeholder="+ add tag">`:""}
      </div></div>

    ${sim.items.length?`<div class="detail-section"><h4>Similar assets</h4>
      <div class="similar-strip">${sim.items.slice(0,8).map(s=>`<img src="/api/assets/${s.uuid}/thumb" data-uuid="${s.uuid}" title="${esc(s.title)}">`).join("")}</div></div>`:""}

    <div class="detail-section"><h4>Dublin Core</h4>
      ${Object.entries(dc).filter(([,v])=>v).map(([k,v])=>`<div class="field-row"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join("")}
      <div class="btn-row"><a class="btn btn-ghost" href="/oai?verb=GetRecord&metadataPrefix=oai_dc&identifier=oai::${a.uuid}" target="_blank">View OAI-PMH XML</a></div>
    </div>

    ${Object.keys(meta.exif||{}).length||Object.keys(meta.iptc||{}).length?`<div class="detail-section"><h4>Embedded metadata (EXIF / IPTC)</h4>
      <div class="kv-scroll">${[...Object.entries(meta.iptc||{}),...Object.entries(meta.exif||{})].slice(0,40).map(([k,v])=>`<div class="field-row"><span class="k">${esc(k)}</span><span class="v">${esc(typeof v==='object'?JSON.stringify(v):v)}</span></div>`).join("")}</div></div>`:""}

    ${d.versions.length?`<div class="detail-section"><h4>Versions</h4>${d.versions.map(v=>`<div class="field-row"><span class="k">v${v.version_no}</span><span class="v">${esc(v.note||'')} &middot; ${fmtBytes(v.size)} &middot; ${(v.created_at||'').slice(0,10)}</span></div>`).join("")}</div>`:""}
  `;

  // wire detail actions
  $$("#drawer-body .similar-strip img").forEach(img => img.addEventListener("click", () => openAsset(img.dataset.uuid)));
  if (canEdit) {
    $("#d-save")?.addEventListener("click", async () => {
      const body = {};
      ["title","description","alt_text","dc_creator","rights_statement","retention_until"].forEach(f => { const el = $("#e-"+f); if (el) body[f] = el.value; });
      await api("/api/assets/"+uuid, { method: "PATCH", json: body });
      toast("Saved");
      await loadFilterOptions();
      openAsset(uuid);
      loadLibrary();
    });
    $("#tag-add")?.addEventListener("keydown", async (e) => {
      if (e.key === "Enter" && e.target.value.trim()) {
        await api("/api/assets/"+uuid+"/tags", { json: { name: e.target.value.trim() } });
        await loadFilterOptions();
        openAsset(uuid);
        loadLibrary();
      }
    });
    $$("#tag-row .rm").forEach(rm => rm.addEventListener("click", async () => {
      await api("/api/assets/"+uuid+"/tags/"+encodeURIComponent(rm.dataset.tag), { method: "DELETE" });
      await loadFilterOptions();
      openAsset(uuid);
      loadLibrary();
    }));
    $("#d-share")?.addEventListener("click", async () => {
      const r = await api("/api/shares", { json: { target_type: "asset", target_id: a.id, permission: "download" } });
      await navigator.clipboard?.writeText(r.url).catch(()=>{});
      toast("Share link copied: " + r.url);
    });
  }
  $("#d-delete")?.addEventListener("click", async () => {
    if (!confirm("Delete this asset?")) return;
    await api("/api/assets/"+uuid, { method: "DELETE" });
    toast("Deleted"); closeDrawer(); await loadFilterOptions(); loadLibrary();
  });
}

// ---------- upload ----------
const uploadModal = $("#upload-modal");
$("#btn-upload").addEventListener("click", () => { uploadModal.classList.remove("hidden"); document.body.classList.add("modal-open"); });
$("#upload-cancel").addEventListener("click", async () => { uploadModal.classList.add("hidden"); document.body.classList.remove("modal-open"); await loadFilterOptions(); loadLibrary(); });
$(".modal-backdrop").addEventListener("click", () => { uploadModal.classList.add("hidden"); document.body.classList.remove("modal-open"); });
const dz = $("#dropzone"), fileInput = $("#file-input");
dz.addEventListener("click", () => fileInput.click());
dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", e => { e.preventDefault(); dz.classList.remove("drag"); uploadFiles(e.dataTransfer.files); });
fileInput.addEventListener("change", () => uploadFiles(fileInput.files));

async function uploadFiles(files) {
  const list = $("#upload-list");
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "upload-item"; row.innerHTML = `<span>${esc(file.name)}</span><span>uploading...</span>`;
    list.prepend(row);
    const fd = new FormData(); fd.append("files", file); fd.append("run_ai", $("#ai-toggle").checked);
    try {
      const r = await api("/api/assets", { method: "POST", body: fd });
      const created = r.created[0];
      row.classList.add(created.duplicate ? "dup" : "ok");
      row.lastChild.textContent = created.duplicate ? "duplicate (deduped)" : "done";
    } catch (err) { row.classList.add("err"); row.lastChild.textContent = "failed"; }
  }
}

// ---------- collections ----------
async function loadCollections() {
  const data = await api("/api/collections");
  $("#collection-list").innerHTML = data.collections.map(c =>
    `<div class="collection-card" data-id="${c.id}"><span class="n">${esc(c.name)}</span><span class="muted">${c.asset_count} assets</span><span class="muted">${esc(c.description||"")}</span></div>`).join("")
    || '<p class="muted">No collections yet.</p>';
  $$("#collection-list .collection-card").forEach(card => card.addEventListener("click", async () => {
    state.collection = card.dataset.id;
    state.page = 1;
    $$(".nav-item").forEach(b => b.classList.remove("active"));
    $('.nav-item[data-view="library"]').classList.add("active");
    $$(".view").forEach(v => v.classList.add("hidden")); $("#view-library").classList.remove("hidden");
    await loadFilterOptions();
    loadLibrary();
  }));
}
$("#btn-new-collection").addEventListener("click", async () => {
  const name = prompt("Collection name"); if (!name) return;
  await api("/api/collections", { json: { name } }); await loadFilterOptions(); loadCollections();
});

// ---------- dashboard ----------
async function loadDashboard() {
  const s = await api("/api/stats");
  const cards = [
    ["Total assets", s.total_assets], ["Storage used", fmtBytes(s.total_bytes)],
    ["Saved by dedup", fmtBytes(s.dedup_saved_bytes)], ["Tags", s.tags], ["Collections", s.collections],
    ...Object.entries(s.by_type).map(([k,v]) => [k.charAt(0).toUpperCase()+k.slice(1), v]),
  ];
  $("#stat-cards").innerHTML = cards.map(([l,n]) => `<div class="stat-card"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
}
$("#btn-fixity").addEventListener("click", async () => {
  $("#fixity-result").textContent = "Checking...";
  const r = await api("/api/admin/fixity", { method: "POST" });
  $("#fixity-result").textContent = r.ok ? `All ${r.checked} blobs verified. Integrity intact.` : `${r.failures.length} of ${r.checked} failed fixity.`;
});

// ---------- users ----------
async function loadUsers() {
  const data = await api("/api/users");
  $("#user-rows").innerHTML = data.users.map(u =>
    `<tr><td>${esc(u.username)}</td><td>${esc(u.email||"")}</td><td>${u.role}</td><td>${u.is_active?"Yes":"No"}</td><td>${(u.last_login||"").slice(0,16).replace("T"," ")}</td></tr>`).join("");
}
$("#btn-new-user").addEventListener("click", async () => {
  const username = prompt("Username"); if (!username) return;
  const password = prompt("Temporary password"); if (!password) return;
  const role = prompt("Role (admin/editor/contributor/viewer)", "contributor") || "viewer";
  await api("/api/users", { json: { username, password, role } }); loadUsers();
});

// ---------- audit ----------
async function loadAudit() {
  const data = await api("/api/audit");
  $("#audit-rows").innerHTML = data.entries.map(e =>
    `<tr><td>${(e.ts||"").slice(0,19).replace("T"," ")}</td><td>${esc(e.username||"-")}</td><td>${esc(e.action)}</td>
     <td>${esc(e.target_type||"")} ${esc((e.target_id||"").slice(0,8))}</td><td>${esc(e.detail||"")}</td><td>${esc(e.ip||"")}</td></tr>`).join("");
}

// ---------- boot ----------
(async function boot() {
  if (state.token) {
    try { state.user = await api("/api/auth/me"); enterApp(); return; } catch {}
  }
  $("#login").classList.remove("hidden");
})();
