"use strict";

const $ = (id) => document.getElementById(id);
let selected = null; // currently selected email candidate
let sendingEnabled = false; // populated from /api/settings
let bulkCount = 20; // selected bulk count
let pendingBulk = null; // { count, mode } awaiting estimate confirmation
let bulkRunning = false;
let userConfig = null; // populated from /api/config
let lastEmails = []; // last fetched candidates (unfiltered)
let availableModels = { openai: [], anthropic: [] };
let settingsCache = null; // last /api/settings response
let gmailCache = null; // last /api/gmail/status response
let connectedEmail = null; // connected Gmail account (shown as the recipient)

// --- Inbox controls state (Phase 2) -----------------------------------------
let pageSize = "20";          // #page-size: 20|50|100|200|all
let statusFilter = "unread";  // status segment: unread|read|all (drives refetch)
let searchQuery = "";         // instant client-side search
let filterAttachments = false; // "Has attachments" chip (client-side)
let filterNeedsReply = true;   // "Needs reply" chip (client-side; persists as show_only_replyable)
const selectedIds = new Set(); // selected email ids for bulk actions
let selectionBusy = false;     // a bulk action is running

// --- Archive workspace state (Phase 7) --------------------------------------
let currentTab = "inbox";        // inbox | archive | usage
let mailContext = "inbox";       // which list owns the shared detail pane: inbox | archive
let archiveFolders = [];         // [{id,name,total,read,unread}]
let archiveFolder = null;        // currently open folder
let archiveEmails = [];          // loaded emails for the open folder (paged)
let archiveNextToken = null;     // Gmail nextPageToken for the open folder
let archiveSearch = "";          // client-side search within the open folder
let archiveLoading = false;      // a folder page load is in flight
let folderGen = 0;               // bumped on every folder navigation; stale loads are dropped
let foldersInFlight = null;      // dedupes concurrent folder-list loads
let prefetchInFlight = false;    // caps hover-prefetch to one request at a time
const archiveSelectedIds = new Set();
// In-memory cache so tab/folder switches are instant (stale-while-revalidate).
let foldersLoadedAt = 0;                  // when the folder list was last fetched
const folderCache = new Map();            // labelId -> { emails, nextToken, at }
const prefetchedFolders = new Set();      // folders we've hover-prefetched this session
const ARCHIVE_CACHE_TTL = 60000;          // serve cache instantly; revalidate past this
function archiveCacheStale(ts) { return Date.now() - ts > ARCHIVE_CACHE_TTL; }
function invalidateArchiveCache() {
  folderCache.clear();
  prefetchedFolders.clear();
  foldersLoadedAt = 0;
}

// The list a shared helper should act on, based on the active mail context.
function emailPool() { return mailContext === "archive" ? archiveEmails : lastEmails; }
function findEmailById(id) {
  return lastEmails.find((e) => e.id === id) || archiveEmails.find((e) => e.id === id) || null;
}

async function api(path, options = {}) {
  // Optional per-call timeout via AbortController. Default (no timeoutMs) keeps
  // the previous behaviour, so slow-by-nature calls (LLM drafts) are untouched;
  // archive calls pass a timeout so a hung Gmail request fails cleanly instead
  // of leaving the UI stuck in a loading state.
  const { timeoutMs, ...fetchOpts } = options;
  let controller = null;
  let timer = null;
  if (timeoutMs) {
    controller = new AbortController();
    timer = setTimeout(() => controller.abort(), timeoutMs);
    fetchOpts.signal = controller.signal;
  }
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...fetchOpts,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  } catch (e) {
    if (e && e.name === "AbortError") throw new Error("Request timed out. Please try again.");
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
  }
}
const ARCHIVE_TIMEOUT_MS = 20000;

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtNum(n) { return (Number(n) || 0).toLocaleString("en-US"); }
function fmtMoney(n) { return "$" + (Number(n) || 0).toFixed(2); }
function fmtMoney4(n) { return "$" + (Number(n) || 0).toFixed(4); }

// Coalesce rapid-fire events (e.g. search keystrokes) into a single trailing
// call, so we re-render once the user pauses instead of on every character.
function debounce(fn, ms = 120) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => { t = null; fn(...args); }, ms);
  };
}

// Compact, Gmail-style relative date: "now", "12m", "5h", "Mar 3", "Mar 3 2025".
function fmtRelativeDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const min = Math.round((now - d) / 60000);
  if (min < 1) return "now";
  if (min < 60) return min + "m";
  const hr = Math.round(min / 60);
  if (hr < 24) return hr + "h";
  const day = Math.round(hr / 24);
  if (day < 7) return day + "d";
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString("en-US",
    sameYear ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
}

// --- Attachments (metadata only; content is never downloaded) ---------------
function attachmentCount(em) {
  return (em.attachments || []).length || (em.has_attachments ? 1 : 0);
}
function attachIcon(mime, name) {
  const m = (mime || "").toLowerCase();
  const n = (name || "").toLowerCase();
  if (m.includes("pdf") || n.endsWith(".pdf")) return "📄";
  if (m.startsWith("image/") || /\.(png|jpe?g|gif|webp|heic|svg)$/.test(n)) return "📷";
  if (m.includes("sheet") || m.includes("excel") || m.includes("csv") || /\.(xlsx?|csv)$/.test(n)) return "📊";
  if (m.includes("word") || m.includes("document") || /\.(docx?)$/.test(n)) return "📝";
  if (m.includes("zip") || m.includes("compress") || /\.(zip|rar|7z|tar|gz)$/.test(n)) return "🗜️";
  return "📎";
}
function fmtSize(bytes) {
  bytes = Number(bytes) || 0;
  if (!bytes) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}
function renderAttachments(el, attachments) {
  if (!el) return;
  attachments = attachments || [];
  if (!attachments.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  // Each chip reserves a slot for a future PDF/image preview (Phase 9): clicking
  // is a no-op today, but the markup/affordance is already in place.
  el.innerHTML = attachments.map((a) => {
    const size = fmtSize(a.size);
    return `<span class="attach-chip" title="${escapeHtml(a.mime_type || "")} — preview coming soon">` +
      `<span class="att-icon">${attachIcon(a.mime_type, a.name)}</span>` +
      `<span class="att-name">${escapeHtml(a.name)}</span>` +
      `${size ? `<span class="att-size">${size}</span>` : ""}</span>`;
  }).join("");
}

// --- Visual identity: avatars, badges, importance ---------------------------
const AVATAR_COLORS = [
  "#2563eb", "#7c3aed", "#db2777", "#dc2626", "#ea580c",
  "#d97706", "#16a34a", "#0891b2", "#4f46e5", "#0d9488",
];
function initialsFor(name, email) {
  const n = (name || "").trim();
  if (n) {
    const parts = n.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    return n.slice(0, 2).toUpperCase();
  }
  return (email || "?").trim().slice(0, 2).toUpperCase() || "?";
}
function avatarFor(em) {
  const email = (em.sender_email || em.sender_name || "?").trim().toLowerCase();
  let h = 0;
  for (let i = 0; i < email.length; i++) h = (h * 31 + email.charCodeAt(i)) >>> 0;
  return { text: initialsFor(em.sender_name, em.sender_email), color: AVATAR_COLORS[h % AVATAR_COLORS.length] };
}
// Priority dot, driven by the cached AI analysis (em.ai.priority). Neutral
// placeholder until the email has been analyzed.
function importanceDot(em) {
  const lvl = String((em.ai && em.ai.priority) || "").toLowerCase();
  const cls = lvl === "critical" ? "crit" : lvl === "high" ? "high"
    : lvl === "medium" ? "med" : lvl === "low" ? "low" : "none";
  const title = lvl ? `Priority: ${em.ai.priority}` : "Not analyzed yet";
  return `<span class="imp-dot imp-${cls}" title="${escapeHtml(title)}"></span>`;
}
function categorySlug(name) {
  return String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-");
}
// Non-score metadata badges shared by the list rows and the read pane.
function metaBadges(em) {
  const out = [];
  if (em.already_processed) out.push(`<span class="badge seen">handled</span>`);
  const nAtt = attachmentCount(em);
  if (nAtt) out.push(`<span class="badge attach">📎 ${nAtt}</span>`);
  const category = em.ai && em.ai.category;
  if (category) {
    out.push(`<span class="badge cat cat-${escapeHtml(categorySlug(category))}">${escapeHtml(category)}</span>`);
  }
  return out;
}

// --- Toast notifications ----------------------------------------------------
let toastTimer = null;
function toast(msg, kind = "ok") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}

// --- Setup status (deduped + short-cached) ----------------------------------
// /api/setup/status calls Gmail under the hood, so we avoid firing it several
// times on boot (onboarding + wizard) or on every idle health tick. Concurrent
// callers share one in-flight request; results are reused for a few seconds.
let setupStatusCache = null;
let setupStatusInFlight = null;
let setupStatusAt = 0;
let appConfigured = false; // true once LLM + Gmail are ready (skips idle re-fetch)
const SETUP_STATUS_TTL_MS = 4000;

function fetchSetupStatus(force = false) {
  const fresh = Date.now() - setupStatusAt < SETUP_STATUS_TTL_MS;
  if (!force && setupStatusCache && fresh) return Promise.resolve(setupStatusCache);
  if (setupStatusInFlight) return setupStatusInFlight;
  setupStatusInFlight = api("/api/setup/status")
    .then((s) => { setupStatusCache = s; setupStatusAt = Date.now(); setupStatusInFlight = null; return s; })
    .catch((e) => { setupStatusInFlight = null; throw e; });
  return setupStatusInFlight;
}

// --- Health ----------------------------------------------------------------
let lastHealth = null; // last /api/health response (drives the DB checklist item)
let fsBlocked = false; // true when a blocking filesystem issue prevents startup
async function refreshHealth() {
  const el = $("health");
  try {
    const h = await api("/api/health");
    lastHealth = h;
    const state = h.status === "ok" ? "ok" : h.status === "degraded" ? "degraded" : "error";
    el.className = "status-dot " + state;
    el.title = `System status: ${h.status}\nGmail: ${h.gmail.detail}\nLLM: ${h.llm.detail}\nDB: ${h.database.detail}`;
    renderFsIssues(h.filesystem);
    renderOnboarding();
  } catch (e) {
    el.className = "status-dot error";
    el.title = "Health unavailable: " + String(e);
  }
}

// --- Filesystem / installation problems ------------------------------------
function renderFsIssues(fs) {
  const banner = $("fs-banner");
  const list = $("fs-list");
  if (!banner || !list) return;
  const issues = (fs && fs.issues) || [];
  const blocking = issues.filter((i) => i.severity === "error");
  fsBlocked = blocking.length > 0;
  const shown = fsBlocked ? blocking : issues; // errors first; else show warnings
  if (!shown.length) {
    banner.classList.add("hidden");
    list.innerHTML = "";
    return;
  }
  list.innerHTML = shown
    .map((i) => `<li class="fs-${escapeHtml(i.severity)}">${escapeHtml(i.message)}</li>`)
    .join("");
  banner.classList.toggle("hidden", false);
  // When startup is blocked, the onboarding/wizard cannot help — hide them.
  if (fsBlocked) {
    $("onboarding").classList.add("hidden");
    $("wizard").classList.add("hidden");
  }
}

// --- Skeleton loaders (perceived speed) -------------------------------------
// Lightweight placeholders shown while a fetch is in flight, so the layout is
// stable and the wait feels shorter than a bare "Loading…" line.
function skeletonRows(n = 8) {
  const row = `<li class="email-item skel" aria-hidden="true">
      <div class="sk sk-av"></div>
      <div class="email-main">
        <div class="sk sk-line w40"></div>
        <div class="sk sk-line w75"></div>
        <div class="sk sk-line w55"></div>
      </div>
    </li>`;
  return row.repeat(n);
}
function skeletonCards(n = 6) {
  const card = `<div class="folder-card skel" aria-hidden="true">
      <div class="sk sk-ico"></div>
      <div class="sk sk-line w60"></div>
      <div class="sk sk-line w35"></div>
    </div>`;
  return card.repeat(n);
}
// One-shot fade for freshly loaded content (real fetches only, not filtering),
// so the skeleton→content swap feels smooth without flickering on every render.
function flashIn(el) {
  if (!el) return;
  el.classList.remove("fade-in");
  void el.offsetWidth; // restart the animation
  el.classList.add("fade-in");
}

// --- Email list ------------------------------------------------------------
async function loadEmails() {
  const empty = $("list-empty");
  empty.textContent = "";
  // A fresh fetch invalidates any prior selection (different rows on screen).
  clearSelection();
  $("email-list").innerHTML = skeletonRows();
  try {
    const params = new URLSearchParams({ max: pageSize, status: statusFilter });
    const { emails } = await api(`/api/emails?${params}`);
    lastEmails = emails;
    renderList();
    flashIn($("email-list"));
  } catch (e) {
    $("email-list").innerHTML = "";
    empty.textContent = "Error: " + e.message;
  }
}

// Client-side filtering pipeline. Search is intentionally simple and pluggable:
// a future AI/semantic search can replace `matchesSearch` without touching the
// rest of the rendering flow.
function matchesSearch(em, q) {
  if (!q) return true;
  const hay = `${em.subject || ""} ${em.sender_name || ""} ${em.sender_email || ""} ${em.snippet || ""}`.toLowerCase();
  return q.split(/\s+/).filter(Boolean).every((term) => hay.includes(term));
}
function visibleEmails() {
  const q = searchQuery.trim().toLowerCase();
  return lastEmails.filter((em) =>
    matchesSearch(em, q) &&
    (!filterAttachments || attachmentCount(em) > 0) &&
    (!filterNeedsReply || em.replyable)
  );
}

function renderList() {
  const list = $("email-list");
  const empty = $("list-empty");
  const shown = visibleEmails();
  if (!lastEmails.length) {
    list.innerHTML = "";
    empty.textContent = "No emails found for this view.";
  } else if (!shown.length) {
    list.innerHTML = "";
    empty.textContent = "No emails match the current search/filters.";
  } else {
    empty.textContent = "";
    // Build the whole list in one DOM write and rely on event delegation
    // (wired once on #email-list) instead of attaching listeners per row.
    list.innerHTML = shown.map((em) => emailRowHtml(em, selectedIds.has(em.id))).join("");
  }
  updateResultCount(shown.length);
  updateSelectionUI();
}

function updateResultCount(shownCount) {
  const el = $("result-count");
  if (!el) return;
  const total = lastEmails.length;
  el.textContent = total ? (shownCount === total ? `${total} emails` : `${shownCount} of ${total}`) : "";
}

// Shared row markup for both the Inbox and Archive lists (identical layout).
// Pure string builder — selection/open handlers are delegated on the <ul>.
function emailRowHtml(em, isSel) {
  const isActive = selected && selected.id === em.id;
  const cls = "email-item" + (em.is_unread ? " unread" : "") + (isSel ? " selected" : "") + (isActive ? " active" : "");
  const av = avatarFor(em);
  // Lighter row: priority dot conveys importance; only meaningful badges
  // (handled / attachments / category) show — no noisy "score" chip.
  const badges = metaBadges(em);
  const dateTitle = escapeHtml(new Date(em.received_at).toLocaleString());
  return `<li class="${cls}" data-id="${escapeHtml(em.id)}">
    <label class="row-check" title="Select email">
      <input type="checkbox" class="row-cb" ${isSel ? "checked" : ""} />
    </label>
    <div class="avatar" style="background:${av.color}">${escapeHtml(av.text)}</div>
    <div class="email-main">
      <div class="row-top">
        <span class="from">${escapeHtml(em.sender_name || em.sender_email)}</span>
        <span class="date" title="${dateTitle}">${escapeHtml(fmtRelativeDate(em.received_at))}</span>
      </div>
      <div class="subj-line">${importanceDot(em)}<span class="subj">${escapeHtml(em.subject)}</span></div>
      <div class="snippet-line">${escapeHtml(em.snippet || "")}</div>
      ${badges.length ? `<div class="badges">${badges.join("")}</div>` : ""}
    </div>
  </li>`;
}

// Delegated click/change handling for an email list. Attached once per list.
// `pool()` returns the backing array; `ctx` is the mail context for selectEmail.
function wireEmailListDelegation(listId, pool, onToggle, ctx) {
  const list = $(listId);
  if (!list) return;
  list.addEventListener("click", (e) => {
    if (e.target.closest(".row-check")) return; // checkbox column never opens detail
    const li = e.target.closest(".email-item");
    if (!li) return;
    const em = pool().find((x) => x.id === li.dataset.id);
    if (em) selectEmail(em, li, ctx);
  });
  list.addEventListener("change", (e) => {
    const cb = e.target.closest(".row-cb");
    if (!cb) return;
    const li = cb.closest(".email-item");
    if (li) onToggle(li.dataset.id, cb.checked, li);
  });
}

// --- Multi-selection + bulk action bar --------------------------------------
function toggleSelect(id, checked, li) {
  if (checked) selectedIds.add(id); else selectedIds.delete(id);
  if (li) li.classList.toggle("selected", checked);
  updateSelectionUI();
}

function clearSelection() {
  selectedIds.clear();
  document.querySelectorAll(".email-item.selected").forEach((n) => n.classList.remove("selected"));
  document.querySelectorAll(".row-cb:checked").forEach((cb) => { cb.checked = false; });
  const menu = $("ab-menu");
  if (menu) menu.classList.add("hidden");
  updateSelectionUI();
}

function updateSelectionUI() {
  const n = selectedIds.size;
  const bar = $("action-bar");
  if (bar) bar.classList.toggle("hidden", n === 0);
  const nEl = $("ab-n");
  if (nEl) nEl.textContent = n;
  // Reflect the visible set on the select-all checkbox (checked / indeterminate).
  const shown = visibleEmails();
  const shownSelected = shown.filter((e) => selectedIds.has(e.id)).length;
  const sa = $("select-all");
  if (sa) {
    sa.checked = shown.length > 0 && shownSelected === shown.length;
    sa.indeterminate = shownSelected > 0 && shownSelected < shown.length;
  }
}

function onSelectAll(checked) {
  const shown = visibleEmails();
  shown.forEach((em) => { if (checked) selectedIds.add(em.id); else selectedIds.delete(em.id); });
  document.querySelectorAll(".email-item").forEach((li) => {
    const on = selectedIds.has(li.dataset.id);
    li.classList.toggle("selected", on);
    const cb = li.querySelector(".row-cb");
    if (cb) cb.checked = on;
  });
  updateSelectionUI();
}

function setActionStatus(text) { const el = $("ab-status"); if (el) el.textContent = text || ""; }

// --- Gmail mailbox actions (mark read/unread, archive, label) ---------------
// All actions accept 1..N ids and update the UI optimistically (no full reload).
function disableActionButtons(disabled) {
  ["ab-generate", "ab-smart", "ab-more"].forEach((id) => {
    const b = $(id); if (b) b.disabled = disabled;
  });
}

function handleMailboxError(e) {
  // The backend 403 detail already explains the reconnect step; surface it.
  toast(e.message || "Action failed.", "error");
}

function setUnreadLocal(ids, unread) {
  const set = new Set(ids);
  lastEmails.forEach((em) => {
    if (!set.has(em.id)) return;
    em.is_unread = unread;
    const labels = new Set((em.label_ids || []).map((l) => l.toUpperCase()));
    if (unread) labels.add("UNREAD"); else labels.delete("UNREAD");
    em.label_ids = [...labels];
  });
}
function archiveLocal(ids) {
  const set = new Set(ids);
  lastEmails = lastEmails.filter((em) => !set.has(em.id));
  if (selected && set.has(selected.id)) closeDetail();
}
function closeDetail() {
  selected = null;
  $("detail").classList.add("hidden");
  $("detail-empty").classList.remove("hidden");
}

async function runMailbox(actionLabel, path, ids, optimisticFn) {
  if (!ids.length || selectionBusy) return;
  selectionBusy = true;
  disableActionButtons(true);
  setActionStatus(`${actionLabel}…`);
  try {
    const r = await api(path, { method: "POST", body: JSON.stringify({ message_ids: ids }) });
    if (optimisticFn) optimisticFn(ids);
    clearSelection();
    renderList();
    const failed = r.failed || 0;
    toast(`${actionLabel}: ${r.modified} email(s)${failed ? ` · ${failed} failed` : ""}.`, failed ? "warn" : "ok");
  } catch (e) {
    handleMailboxError(e);
  } finally {
    selectionBusy = false;
    disableActionButtons(false);
    setActionStatus("");
  }
}

function actionIds() { return [...selectedIds]; }
function doMarkRead(ids) { return runMailbox("Marked read", "/api/mailbox/mark-read", ids, (i) => setUnreadLocal(i, false)); }
function doMarkUnread(ids) { return runMailbox("Marked unread", "/api/mailbox/mark-unread", ids, (i) => setUnreadLocal(i, true)); }

// --- Label dialog -----------------------------------------------------------
let labelTargetIds = [];
// mode "label" = apply label only; "move" = apply label + archive (Move to folder).
async function openLabelModal(ids, mode = "label") {
  if (!ids.length) return;
  labelTargetIds = ids;
  const isMove = mode === "move";
  $("label-modal-title").textContent = isMove ? "Move to folder" : "Apply a label";
  $("label-apply").textContent = isMove ? "Move" : "Apply label";
  // Move implies archiving, so the toggle is preset and hidden; Apply leaves it visible/off.
  $("label-archive").checked = isMove;
  $("label-archive-row").classList.toggle("hidden", isMove);
  $("label-target-count").textContent = ids.length;
  $("label-new").value = "";
  $("label-select").innerHTML = `<option value="">— select —</option>`;
  $("label-status").textContent = "Loading labels…";
  $("label-modal").classList.remove("hidden");
  try {
    const { labels } = await api("/api/labels");
    const opts = (labels || []).filter((l) => l.type === "user")
      .map((l) => `<option value="${escapeHtml(l.id)}">${escapeHtml(l.name)}</option>`).join("");
    $("label-select").innerHTML = `<option value="">— select —</option>` + opts;
    $("label-status").textContent = opts ? "" : "No labels yet — create one below.";
  } catch (e) {
    // Still allow creating a label even if listing failed (e.g. scope prompt).
    $("label-status").textContent = e.message || "Could not load labels.";
  }
}

async function applyLabel() {
  const ids = labelTargetIds;
  if (!ids.length) return;
  const labelId = $("label-select").value;
  const newName = $("label-new").value.trim();
  if (!labelId && !newName) { $("label-status").textContent = "Choose or create a label."; return; }
  const archive = $("label-archive").checked;
  $("label-apply").disabled = true;
  $("label-status").textContent = "Applying…";
  try {
    const body = { message_ids: ids, archive };
    if (newName) body.label_name = newName; else body.label_id = labelId;
    const r = await api("/api/mailbox/label", { method: "POST", body: JSON.stringify(body) });
    $("label-modal").classList.add("hidden");
    if (archive) archiveLocal(ids);
    clearSelection();
    renderList();
    toast(`Labelled ${r.modified} email(s)${archive ? " and archived" : ""}.`, r.failed ? "warn" : "ok");
  } catch (e) {
    $("label-status").textContent = "Error: " + e.message;
  } finally {
    $("label-apply").disabled = false;
  }
}

// --- AI analysis card (read pane) -------------------------------------------
// On-demand only; never auto-runs (cost control) and never modifies Gmail.
function renderAiSection(em) {
  const wrap = $("d-ai-wrap");
  if (!wrap) return;
  const a = em.ai;
  if (!a) {
    wrap.innerHTML =
      `<div class="ai-head"><span>✨ AI Assistant</span></div>` +
      `<div class="ai-empty">` +
      `<span class="muted">Understand this email in one click — summary, category, priority and a suggested reply.</span>` +
      `<button id="d-analyze" class="btn small primary">✨ Analyze with AI</button></div>`;
    $("d-analyze").addEventListener("click", () => analyzeCurrent(false));
    return;
  }
  const conf = Math.round((Number(a.confidence) || 0) * 100);
  const prioCls = "prio-" + String(a.priority || "").toLowerCase();
  const catCls = "cat-" + categorySlug(a.category);
  wrap.innerHTML =
    `<div class="ai-card">
      <div class="ai-head"><span>✨ AI Assistant</span><span class="ai-conf" title="Model confidence">${conf}%</span></div>
      <div class="ai-grid">
        <div class="ai-row"><span class="ai-k">Category</span><span class="badge cat ${catCls}">${escapeHtml(a.category)}</span></div>
        <div class="ai-row"><span class="ai-k">Priority</span><span class="prio ${prioCls}">${escapeHtml(a.priority)}</span></div>
        <div class="ai-row"><span class="ai-k">Reply</span><span>${a.needs_reply ? "Recommended" : "Not needed"}</span></div>
        <div class="ai-row"><span class="ai-k">Action</span><span>${escapeHtml(a.action_recommended)}</span></div>
      </div>
      <div class="ai-summary"><span class="ai-k">Summary</span><p>${escapeHtml(a.summary || "—")}</p></div>
      <div class="ai-foot"><span class="muted">Read-only${a.model ? " · " + escapeHtml(a.model) : ""}</span>` +
      `<button id="d-reanalyze" class="link-btn">Re-analyze</button></div>
    </div>`;
  const rb = $("d-reanalyze");
  if (rb) rb.addEventListener("click", () => analyzeCurrent(true));
}

async function analyzeCurrent(force) {
  if (!selected) return;
  const wrap = $("d-ai-wrap");
  wrap.innerHTML = `<div class="ai-card"><div class="ai-head"><span>✨ AI Analysis</span></div><p class="muted">Analyzing…</p></div>`;
  try {
    const a = await api(`/api/emails/${selected.id}/analyze${force ? "?force=true" : ""}`, { method: "POST" });
    selected.ai = a;
    const row = findEmailById(selected.id);
    if (row) row.ai = a;
    renderAiSection(selected);
    // Surface category/priority on the originating list row.
    if (mailContext === "archive") renderArchiveList(); else renderList();
    toast("Email analyzed.");
  } catch (e) {
    renderAiSection(selected);
    toast("Analysis failed: " + e.message, "error");
  }
}

// --- Smart Archive (AI filing; rules + learning + cache, no new LLM call) ----
let smartTargetIds = [];
let smartUnanalyzedIds = []; // emails the backend couldn't decide (need analysis)
let smartPlanItems = [];     // last preview items (label, count, confidence, message_ids)
let smartOverrides = {};     // originalLabel -> user-chosen label
let smartContext = "inbox";  // which view launched Smart Archive (inbox | archive)
// Label choices offered when the user edits a suggestion.
const CATEGORY_OPTIONS = [
  "Finance", "Administration", "Work", "Research", "Shopping", "Gaming",
  "Travel", "Personal", "Newsletter", "Spam", "Utilities", "Development", "Other",
];
// id + sender pairs so the backend rules engine can decide without a Gmail fetch.
function refsFor(ids) {
  return ids.map((id) => {
    const em = findEmailById(id);
    return { id, sender: em ? em.sender_email || "" : "" };
  });
}
function confClass(conf) {
  const pct = Math.round((Number(conf) || 0) * 100);
  return pct >= 85 ? "conf-high" : pct >= 60 ? "conf-med" : "conf-low";
}
// Read-only label/count row (used in the result summary).
function planRow(label, count) {
  return `<div class="plan-row"><span class="badge cat cat-${categorySlug(label)}">${escapeHtml(label)}</span>` +
    `<span class="plan-fill"></span><b>${count}</b></div>`;
}
// Editable suggestion row: confidence + an editable label + count.
function smartItemRow(item) {
  const pct = Math.round((Number(item.confidence) || 0) * 100);
  const current = smartOverrides[item.label] || item.label;
  const opts = [...new Set([current, item.label, ...CATEGORY_OPTIONS])]
    .map((o) => `<option value="${escapeHtml(o)}"${o === current ? " selected" : ""}>${escapeHtml(o)}</option>`)
    .join("");
  return `<div class="plan-row">
      <select class="plan-label-sel" data-orig="${escapeHtml(item.label)}">${opts}</select>
      <span class="conf ${confClass(item.confidence)}" title="AI confidence">${pct}%</span>
      <span class="plan-fill"></span><b>${item.count}</b>
    </div>`;
}
async function openSmartArchive(ids, ctx = "inbox") {
  if (!ids.length) return;
  smartContext = ctx;
  smartTargetIds = ids;
  $("smart-title").textContent = "✨ Smart Archive";
  $("smart-cancel").textContent = "Cancel";
  $("smart-confirm").classList.remove("hidden");
  $("smart-confirm").disabled = true;
  $("smart-modal").classList.remove("hidden");
  await reRunPreview();
}

async function reRunPreview() {
  $("smart-body").innerHTML = `<p class="muted">Planning…</p>`;
  try {
    const plan = await api("/api/smart-archive/preview", {
      method: "POST", body: JSON.stringify({ emails: refsFor(smartTargetIds) }),
    });
    renderSmartPlan(plan);
  } catch (e) {
    $("smart-body").innerHTML = `<p class="warn-box">${escapeHtml(e.message)}</p>`;
  }
}

function renderSmartPlan(plan) {
  const body = $("smart-body");
  const confirm = $("smart-confirm");
  confirm.classList.remove("hidden");
  // The backend decides what still needs analysis (rules/learning file many
  // emails with no AI pass at all); trust its list rather than guessing.
  smartUnanalyzedIds = plan.unanalyzed_ids || [];
  smartPlanItems = plan.items || [];
  smartOverrides = {};
  const rows = smartPlanItems.map((i) => smartItemRow(i)).join("");
  const planBlock = plan.decided
    ? `<p class="muted">${plan.decided} email(s) suggested — edit any label if needed:</p>` +
      `<div class="plan-list">${rows}</div>`
    : "";

  if (smartUnanalyzedIds.length) {
    // Offer to do the analysis inline — one click, no dead-end.
    body.innerHTML = planBlock +
      `<p class="muted plan-note">${smartUnanalyzedIds.length} email(s) need a quick AI pass first — ` +
      `“Analyze &amp; Continue” will handle it automatically.</p>`;
    confirm.textContent = "Analyze & Continue";
    confirm.dataset.mode = "analyze";
    confirm.disabled = false;
  } else {
    body.innerHTML = planBlock || `<p class="muted">Nothing to archive.</p>`;
    confirm.textContent = "Archive";
    confirm.dataset.mode = "archive";
    confirm.disabled = !plan.decided;
  }
  // Track per-group label overrides (memorized server-side on Archive).
  body.querySelectorAll(".plan-label-sel").forEach((sel) =>
    sel.addEventListener("change", (e) => { smartOverrides[e.target.dataset.orig] = e.target.value; }));
}

// Analyze the still-undecided selection, then re-plan and show it. Caching is
// handled server-side by /analyze, so this stays a single seamless step.
async function analyzeAndContinue() {
  const ids = smartUnanalyzedIds.slice();
  if (!ids.length) return reRunPreview();
  const confirm = $("smart-confirm");
  confirm.disabled = true;
  let done = 0, failed = 0;
  for (const id of ids) {
    $("smart-body").innerHTML = `<p class="muted">Analyzing ${done + 1} / ${ids.length}…</p>`;
    try {
      const a = await api(`/api/emails/${id}/analyze`, { method: "POST" });
      const em = findEmailById(id);
      if (em) em.ai = a;
      if (selected && selected.id === id) { selected.ai = a; renderAiSection(selected); }
      done++;
    } catch (_) {
      failed++;
    }
  }
  // Newly analyzed rows light up their category/priority in whichever list.
  if (smartContext === "archive") renderArchiveList(); else renderList();
  if (failed) toast(`${failed} email(s) could not be analyzed.`, "warn");
  await reRunPreview(); // re-runs Smart Archive and shows the filing plan
}

function onSmartConfirm() {
  if ($("smart-confirm").dataset.mode === "analyze") analyzeAndContinue();
  else confirmSmartArchive();
}
async function confirmSmartArchive() {
  const ids = smartTargetIds;
  if (!ids.length) return;
  $("smart-confirm").disabled = true;
  $("smart-body").innerHTML = `<p class="muted">Filing & archiving…</p>`;
  try {
    // Build refs from the (possibly edited) plan; only edited groups carry an
    // override, so unedited ones keep their rule/learned/ai source in history.
    const refs = [];
    for (const item of smartPlanItems) {
      const chosen = smartOverrides[item.label] || item.label;
      const overridden = chosen !== item.label;
      for (const mid of item.message_ids || []) {
        const em = findEmailById(mid);
        const sender = em ? em.sender_email || "" : "";
        refs.push(overridden ? { id: mid, sender, override: chosen } : { id: mid, sender });
      }
    }
    const r = await api("/api/smart-archive/execute", {
      method: "POST", body: JSON.stringify({ emails: refs }),
    });
    renderSmartResult(r);
    if (smartContext === "archive") {
      // Reclassified emails move between folders — drop stale caches and refresh.
      invalidateArchiveCache();
      clearArchiveSelection();
      reloadArchiveFolder();
      loadFolders({ force: true });
    } else {
      clearSelection();
      loadEmails(); // archived emails drop out of the inbox view
    }
    toast(`Smart Archive: ${r.archived} email(s) filed.`, r.failed ? "warn" : "ok");
  } catch (e) {
    $("smart-body").innerHTML = `<p class="warn-box">${escapeHtml(e.message)}</p>`;
    $("smart-confirm").classList.remove("hidden");
    $("smart-confirm").disabled = false;
  }
}
function renderSmartResult(r) {
  $("smart-title").textContent = "✓ Smart Archive complete";
  const byLabel = Object.entries(r.by_label || {}).map(([l, n]) => planRow(l, n)).join("");
  const created = (r.labels_created || []).length
    ? `<p class="muted plan-note">Labels created: ${r.labels_created.map(escapeHtml).join(", ")}</p>` : "";
  const skipped = r.needs_analysis
    ? `<p class="muted plan-note">${r.needs_analysis} still need analysis.</p>` : "";
  const failed = r.failed
    ? `<p class="warn-box">${r.failed} label group(s) failed and were left untouched.</p>` : "";
  $("smart-body").innerHTML = `<p><b>${r.archived}</b> email(s) archived.</p>` +
    `<div class="plan-list">${byLabel}</div>${created}${skipped}${failed}`;
  $("smart-confirm").classList.add("hidden");
  $("smart-cancel").textContent = "Done";
}

// Quick actions for the currently-open email (operate on a single id). The set
// of actions depends on which list the email came from (inbox vs archive).
function renderDetailActions(em) {
  const el = $("d-actions");
  if (!el) return;
  // Discreet icon buttons for the secondary actions; the read pane's primary
  // action (Generate / Restore) stands on its own.
  const readBtn = em.is_unread
    ? `<button class="btn ghost icon small" data-act="read" title="Mark as read" aria-label="Mark as read">✓</button>`
    : `<button class="btn ghost icon small" data-act="unread" title="Mark as unread" aria-label="Mark as unread">◌</button>`;
  const archiveButtons = mailContext === "archive"
    ? `<button class="btn small primary" data-act="restore">↩ Restore to Inbox</button>`
    : `<button class="btn ghost icon small" data-act="move" title="Move to folder" aria-label="Move to folder">📁</button>` +
      `<button class="btn ghost icon small" data-act="label" title="Apply label" aria-label="Apply label">🏷️</button>`;
  el.innerHTML = readBtn + archiveButtons;
  el.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      const ids = [em.id];
      const act = b.dataset.act;
      const archived = mailContext === "archive";
      if (act === "read") archived ? doArchMarkRead(ids) : doMarkRead(ids);
      else if (act === "unread") archived ? doArchMarkUnread(ids) : doMarkUnread(ids);
      else if (act === "move") openLabelModal(ids, "move");
      else if (act === "label") openLabelModal(ids, "label");
      else if (act === "restore") doRestore(ids);
    }));
}

// Generate + save Gmail drafts for the selected emails. Reuses the existing
// per-email endpoints (no new backend), so it is safe and incremental. Phase 5
// can swap this for a dedicated server-side bulk endpoint.
async function generateDraftsFor(ids, { btnId, statusFn, afterFn } = {}) {
  if (selectionBusy || !ids.length) return;
  selectionBusy = true;
  const btn = btnId ? $(btnId) : null;
  if (btn) btn.disabled = true;
  const setStatus = statusFn || setActionStatus;
  const tone = $("d-tone") ? $("d-tone").value : (userConfig && userConfig.default_tone) || "professional";
  const language = $("d-language") ? $("d-language").value : (userConfig && userConfig.default_language) || "auto";
  let ok = 0, fail = 0;
  for (let i = 0; i < ids.length; i++) {
    setStatus(`Generating ${i + 1}/${ids.length}…`);
    try {
      const r = await api(`/api/emails/${ids[i]}/draft`, {
        method: "POST", body: JSON.stringify({ tone, language }),
      });
      await api(`/api/emails/${ids[i]}/save-draft`, {
        method: "POST", body: JSON.stringify({ draft: r.draft }),
      });
      ok++;
    } catch (_) { fail++; }
  }
  setStatus("");
  if (btn) btn.disabled = false;
  selectionBusy = false;
  toast(`Drafts saved: ${ok}${fail ? ` · ${fail} failed` : ""}.`, fail ? "warn" : "ok");
  if (afterFn) afterFn();
}

// Generate + save Gmail drafts for the inbox selection.
function runSelectionDrafts() {
  return generateDraftsFor([...selectedIds], { btnId: "ab-generate", afterFn: loadEmails });
}

// ====================== Archive workspace (Phase 7) =========================
// A label-as-folder browser over the emails Smart Archive filed. It reuses the
// inbox's row rendering, the shared read pane, AI analysis, drafts and Smart
// Archive — only the data source (a label page) and the actions (Restore) differ.
function colorForLabel(name) {
  const s = (name || "?").toLowerCase();
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function enterArchive() {
  // Re-entering the tab keeps the open folder; otherwise show the folder grid.
  if (archiveFolder) {
    $("archive-home").classList.add("hidden");
    $("archive-folder").classList.remove("hidden");
    return;
  }
  $("archive-home").classList.remove("hidden");
  $("archive-folder").classList.add("hidden");
  if (archiveFolders.length) {
    renderFolderGrid();                                    // instant from memory
    if (archiveCacheStale(foldersLoadedAt)) loadFolders({ background: true });
  } else {
    loadFolders();
  }
}

async function loadFolders({ force = false, background = false } = {}) {
  // Serve the in-memory grid instantly when it's still fresh.
  if (!force && !background && archiveFolders.length && !archiveCacheStale(foldersLoadedAt)) {
    renderFolderGrid();
    return;
  }
  // Dedupe concurrent loads (rapid tab switches) — share one request so the
  // last-arriving response can't clobber an earlier render out of order.
  if (foldersInFlight) return foldersInFlight;
  const empty = $("folder-empty");
  empty.classList.add("hidden");
  // Skeletons only on a true first load; a background refresh keeps the current
  // grid visible (no flicker).
  if (!archiveFolders.length && !background) $("folder-grid").innerHTML = skeletonCards();
  foldersInFlight = (async () => {
    try {
      const { folders } = await api("/api/archive/folders", { timeoutMs: ARCHIVE_TIMEOUT_MS });
      archiveFolders = folders || [];
      foldersLoadedAt = Date.now();
      renderFolderGrid();
    } catch (e) {
      // A silent background refresh must never wipe the grid the user is looking at.
      if (background) return;
      renderFolderError(e.message);
    } finally {
      foldersInFlight = null;
    }
  })();
  return foldersInFlight;
}

function renderFolderError(message) {
  $("folder-grid").innerHTML = "";
  const empty = $("folder-empty");
  empty.innerHTML = `Could not load folders: ${escapeHtml(message)} ` +
    `<button id="folder-retry" class="btn small ghost">Retry</button>`;
  empty.classList.remove("hidden");
  const btn = $("folder-retry");
  if (btn) btn.addEventListener("click", () => loadFolders({ force: true }));
}

// Prefetch a folder's first page on hover-intent (once per folder) so opening
// it is instant. Cost is bounded: one page, one prefetch in flight at a time,
// deduped against the cache + prefetches already done.
function prefetchFolder(folderId) {
  if (prefetchInFlight || folderCache.has(folderId) || prefetchedFolders.has(folderId)) return;
  prefetchInFlight = true;
  prefetchedFolders.add(folderId);
  const params = new URLSearchParams({ label_id: folderId, page_size: "25" });
  api(`/api/archive/emails?${params}`, { timeoutMs: ARCHIVE_TIMEOUT_MS })
    .then((r) => folderCache.set(folderId, { emails: r.emails || [], nextToken: r.next_page_token || null, at: Date.now() }))
    .catch(() => prefetchedFolders.delete(folderId)) // allow a later retry
    .finally(() => { prefetchInFlight = false; });
}

function renderFolderGrid() {
  const grid = $("folder-grid");
  const empty = $("folder-empty");
  if (!archiveFolders.length) {
    grid.innerHTML = "";
    empty.textContent = "No archived folders yet. Use ✨ Smart Archive on the Inbox to file emails by label.";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  grid.innerHTML = archiveFolders.map((f) => {
    const color = colorForLabel(f.name);
    return `<button class="folder-card" data-id="${escapeHtml(f.id)}">
        <span class="folder-card-top">
          <span class="folder-ico" style="background:${color}1a;color:${color}">📁</span>
          ${f.unread ? `<span class="folder-unread">${f.unread}</span>` : ""}
        </span>
        <span class="folder-name">${escapeHtml(f.name)}</span>
        <span class="folder-meta">${fmtNum(f.total)} email${f.total === 1 ? "" : "s"}</span>
        <span class="folder-substats">
          <span class="fs-read">${fmtNum(f.read)} read</span>
          <span class="fs-dot">·</span>
          <span class="fs-unread${f.unread ? " has" : ""}">${fmtNum(f.unread)} unread</span>
        </span>
      </button>`;
  }).join("");
  grid.querySelectorAll(".folder-card").forEach((c) => {
    c.addEventListener("click", () => {
      const folder = archiveFolders.find((f) => f.id === c.dataset.id);
      if (folder) openFolder(folder);
    });
    // Hover-intent prefetch (180ms) so a click opens instantly.
    let hoverTimer = null;
    c.addEventListener("mouseenter", () => { hoverTimer = setTimeout(() => prefetchFolder(c.dataset.id), 180); });
    c.addEventListener("mouseleave", () => { if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; } });
  });
  flashIn(grid);
}

function openFolder(folder) {
  folderGen++;             // invalidate any in-flight load from a prior folder
  archiveFolder = folder;
  archiveSearch = "";
  clearArchiveSelection();
  $("arch-search").value = "";
  $("af-name").textContent = folder.name;
  $("af-dot").style.background = colorForLabel(folder.name);
  $("archive-home").classList.add("hidden");
  $("archive-folder").classList.remove("hidden");
  updateFolderHeaderCount();

  const cached = folderCache.get(folder.id);
  if (cached) {
    // Instant open from memory (incl. hover-prefetch); revalidate if stale.
    archiveEmails = cached.emails.slice();
    archiveNextToken = cached.nextToken;
    renderArchiveList();
    flashIn($("arch-email-list"));
    if (archiveCacheStale(cached.at)) refreshOpenFolder();
  } else {
    archiveEmails = [];
    archiveNextToken = null;
    $("arch-email-list").innerHTML = "";
    loadFolderEmails(true);
  }
}

function backToFolders() {
  folderGen++;             // drop any pending folder-email load
  archiveFolder = null;
  archiveLoading = false;
  clearArchiveSelection();
  $("archive-folder").classList.add("hidden");
  $("archive-home").classList.remove("hidden");
  if (archiveFolders.length) {
    renderFolderGrid();                       // instant from memory
    loadFolders({ background: true });         // refresh counts silently
  } else {
    loadFolders();
  }
}

// Silent first-page revalidation of the open folder (no skeleton flicker).
async function refreshOpenFolder() {
  if (!archiveFolder) return;
  const folderId = archiveFolder.id;
  const gen = folderGen;
  try {
    const params = new URLSearchParams({ label_id: folderId, page_size: "25" });
    const r = await api(`/api/archive/emails?${params}`, { timeoutMs: ARCHIVE_TIMEOUT_MS });
    // Drop the response if the user navigated/reloaded meanwhile.
    if (gen !== folderGen || !archiveFolder || archiveFolder.id !== folderId) return;
    archiveEmails = r.emails || [];
    archiveNextToken = r.next_page_token || null;
    folderCache.set(folderId, { emails: archiveEmails.slice(), nextToken: archiveNextToken, at: Date.now() });
    renderArchiveList();
  } catch (_) { /* keep the cached view on failure */ }
}

function updateFolderHeaderCount() {
  if (!archiveFolder) return;
  const f = archiveFolder;
  $("af-count").textContent = `${fmtNum(f.total)} email${f.total === 1 ? "" : "s"} · ${fmtNum(f.unread)} unread`;
}

function reloadArchiveFolder() {
  if (!archiveFolder) return;
  folderGen++;             // supersede any in-flight load before reloading
  archiveLoading = false;
  archiveEmails = [];
  archiveNextToken = null;
  $("arch-email-list").innerHTML = "";
  loadFolderEmails(true);
}

async function loadFolderEmails(reset) {
  if (!archiveFolder || archiveLoading) return;
  archiveLoading = true;
  const gen = folderGen;
  const folder = archiveFolder;
  const empty = $("arch-empty");
  if (reset) { empty.textContent = ""; $("arch-email-list").innerHTML = skeletonRows(6); }
  const moreBtn = $("arch-load-more");
  moreBtn.disabled = true;
  try {
    const params = new URLSearchParams({ label_id: folder.id, page_size: "25" });
    if (!reset && archiveNextToken) params.set("page_token", archiveNextToken);
    const r = await api(`/api/archive/emails?${params}`, { timeoutMs: ARCHIVE_TIMEOUT_MS });
    // Stale guard: the user opened another folder / went back while we waited.
    if (gen !== folderGen) return;
    const incoming = r.emails || [];
    // De-dupe defensively in case a page boundary repeats an id.
    const have = new Set(archiveEmails.map((e) => e.id));
    archiveEmails.push(...incoming.filter((e) => !have.has(e.id)));
    archiveNextToken = r.next_page_token || null;
    renderArchiveList();
    if (reset) flashIn($("arch-email-list"));
    // Cache the loaded set so reopening this folder is instant.
    folderCache.set(folder.id, {
      emails: archiveEmails.slice(), nextToken: archiveNextToken, at: Date.now(),
    });
  } catch (e) {
    if (gen === folderGen) renderFolderEmailsError(e.message, reset);
  } finally {
    if (gen === folderGen) archiveLoading = false;
    moreBtn.disabled = false;
  }
}

function renderFolderEmailsError(message, reset) {
  const empty = $("arch-empty");
  if (reset) $("arch-email-list").innerHTML = "";
  empty.innerHTML = `Could not load this folder: ${escapeHtml(message)} ` +
    `<button id="arch-retry" class="btn small ghost">Retry</button>`;
  const btn = $("arch-retry");
  if (btn) btn.addEventListener("click", () => { archiveLoading = false; loadFolderEmails(true); });
}

function visibleArchiveEmails() {
  const q = archiveSearch.trim().toLowerCase();
  return archiveEmails.filter((em) => matchesSearch(em, q));
}

function renderArchiveList() {
  const list = $("arch-email-list");
  const empty = $("arch-empty");
  const shown = visibleArchiveEmails();
  if (!archiveEmails.length) {
    list.innerHTML = "";
    empty.textContent = "This folder is empty.";
  } else if (!shown.length) {
    list.innerHTML = "";
    empty.textContent = "No emails match your search.";
  } else {
    empty.textContent = "";
    // Same shared builder + delegated handlers as the inbox (wired once).
    list.innerHTML = shown.map((em) => emailRowHtml(em, archiveSelectedIds.has(em.id))).join("");
  }
  // Result count + "Load more" affordance (paged server-side).
  const rc = $("arch-result-count");
  const total = archiveEmails.length;
  rc.textContent = total ? (shown.length === total ? `${total} loaded` : `${shown.length} of ${total} loaded`) : "";
  $("arch-more").classList.toggle("hidden", !archiveNextToken);
  updateArchiveSelectionUI();
}

// --- Archive selection ------------------------------------------------------
function toggleArchiveSelect(id, checked, li) {
  if (checked) archiveSelectedIds.add(id); else archiveSelectedIds.delete(id);
  if (li) li.classList.toggle("selected", checked);
  updateArchiveSelectionUI();
}
function clearArchiveSelection() {
  archiveSelectedIds.clear();
  document.querySelectorAll("#arch-email-list .email-item.selected").forEach((n) => n.classList.remove("selected"));
  document.querySelectorAll("#arch-email-list .row-cb:checked").forEach((cb) => { cb.checked = false; });
  const menu = $("arch-menu");
  if (menu) menu.classList.add("hidden");
  updateArchiveSelectionUI();
}
function updateArchiveSelectionUI() {
  const n = archiveSelectedIds.size;
  const bar = $("arch-action-bar");
  if (bar) bar.classList.toggle("hidden", n === 0);
  const nEl = $("arch-ab-n");
  if (nEl) nEl.textContent = n;
  const shown = visibleArchiveEmails();
  const shownSelected = shown.filter((e) => archiveSelectedIds.has(e.id)).length;
  const sa = $("arch-select-all");
  if (sa) {
    sa.checked = shown.length > 0 && shownSelected === shown.length;
    sa.indeterminate = shownSelected > 0 && shownSelected < shown.length;
  }
}
function onArchiveSelectAll(checked) {
  const shown = visibleArchiveEmails();
  shown.forEach((em) => { if (checked) archiveSelectedIds.add(em.id); else archiveSelectedIds.delete(em.id); });
  document.querySelectorAll("#arch-email-list .email-item").forEach((li) => {
    const on = archiveSelectedIds.has(li.dataset.id);
    li.classList.toggle("selected", on);
    const cb = li.querySelector(".row-cb");
    if (cb) cb.checked = on;
  });
  updateArchiveSelectionUI();
}
function archActionIds() { return [...archiveSelectedIds]; }
function setArchStatus(text) { const el = $("arch-ab-status"); if (el) el.textContent = text || ""; }

// --- Archive actions (restore, read-state) ----------------------------------
function setUnreadArchiveLocal(ids, unread) {
  const set = new Set(ids);
  let delta = 0;
  archiveEmails.forEach((em) => {
    if (!set.has(em.id)) return;
    if (em.is_unread !== unread) delta += unread ? 1 : -1;
    em.is_unread = unread;
    const labels = new Set((em.label_ids || []).map((l) => l.toUpperCase()));
    if (unread) labels.add("UNREAD"); else labels.delete("UNREAD");
    em.label_ids = [...labels];
  });
  if (archiveFolder) {
    archiveFolder.unread = Math.max(0, (archiveFolder.unread || 0) + delta);
    updateFolderHeaderCount();
  }
}

async function runArchiveMailbox(actionLabel, path, ids, optimisticFn) {
  if (!ids.length || selectionBusy) return;
  selectionBusy = true;
  setArchStatus(`${actionLabel}…`);
  try {
    const r = await api(path, { method: "POST", body: JSON.stringify({ message_ids: ids }) });
    if (optimisticFn) optimisticFn(ids);
    clearArchiveSelection();
    renderArchiveList();
    const failed = r.failed || 0;
    toast(`${actionLabel}: ${r.modified} email(s)${failed ? ` · ${failed} failed` : ""}.`, failed ? "warn" : "ok");
  } catch (e) {
    handleMailboxError(e);
  } finally {
    selectionBusy = false;
    setArchStatus("");
  }
}

function doRestore(ids) {
  // Restore keeps the filing label, so emails legitimately stay in this folder
  // (now also in the inbox). The folder count is unchanged; a toast confirms.
  return runArchiveMailbox("Restored to Inbox", "/api/archive/restore", ids, null);
}
function doArchMarkRead(ids) {
  return runArchiveMailbox("Marked read", "/api/mailbox/mark-read", ids, (i) => setUnreadArchiveLocal(i, false));
}
function doArchMarkUnread(ids) {
  return runArchiveMailbox("Marked unread", "/api/mailbox/mark-unread", ids, (i) => setUnreadArchiveLocal(i, true));
}
function runArchiveDrafts() {
  return generateDraftsFor(archActionIds(), { btnId: "arch-generate", statusFn: setArchStatus });
}

// Full email body — fetched on open, cached so re-opening is instant. The
// server returns readable text (HTML flattened), rendered as text (no XSS risk).
const bodyCache = new Map();
async function renderBody(em) {
  const el = $("d-body");
  if (!el) return;
  el.classList.remove("loading");
  if (bodyCache.has(em.id)) { el.textContent = bodyCache.get(em.id); return; }
  el.textContent = em.snippet || "";        // instant placeholder
  el.classList.add("loading");
  try {
    const data = await api(`/api/emails/${em.id}`);
    if (!selected || selected.id !== em.id) return;  // user navigated away
    const body = (data.body || em.snippet || "").trim();
    bodyCache.set(em.id, body);
    el.textContent = body;
    if (Array.isArray(data.attachments) && data.attachments.length) {
      renderAttachmentCard(em, data.attachments);
    }
  } catch (_) {
    /* keep the snippet placeholder on failure */
  } finally {
    if (selected && selected.id === em.id) el.classList.remove("loading");
  }
}

function renderAttachmentCard(em, attachments) {
  const list = attachments || em.attachments || [];
  renderAttachments($("d-attachments"), list);
  $("d-attach-count").textContent = list.length ? `· ${list.length}` : "";
  $("d-attach-card").classList.toggle("hidden", list.length === 0);
}

function selectEmail(em, li, ctx = "inbox") {
  selected = em;
  mailContext = ctx;
  document.querySelectorAll(".email-item.active").forEach((n) => n.classList.remove("active"));
  li.classList.add("active");
  $("detail-empty").classList.add("hidden");
  $("detail").classList.remove("hidden");
  flashIn($("detail"));
  $("d-subject").textContent = em.subject;
  $("d-sender").textContent = em.sender_name ? `${em.sender_name} <${em.sender_email}>` : em.sender_email;
  $("d-date").textContent = new Date(em.received_at).toLocaleString();
  const av = avatarFor(em);
  const avEl = $("d-avatar");
  avEl.textContent = av.text;
  avEl.style.background = av.color;
  $("d-recipient").textContent = connectedEmail || "you";
  const statusBadge = `<span class="badge ${em.is_unread ? "unread-badge" : "read-badge"}">${em.is_unread ? "Unread" : "Read"}</span>`;
  $("d-badges").innerHTML = importanceDot(em) + statusBadge + metaBadges(em).join("");
  renderDetailActions(em);
  renderAiSection(em);
  renderAttachmentCard(em, em.attachments);
  renderBody(em);
  $("d-score").textContent = em.score;
  const cls = $("d-class");
  cls.textContent = em.classification || (em.replyable ? "Reply required" : "No reply needed");
  cls.className = "reply-class " + (em.replyable ? "reply-yes" : "reply-no");
  $("d-reasons").innerHTML = (em.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
  if (userConfig) {
    if (userConfig.default_tone) $("d-tone").value = userConfig.default_tone;
    if (userConfig.default_language) $("d-language").value = userConfig.default_language;
  }
  $("d-summary-wrap").classList.add("hidden");
  $("d-draft").value = "";
  $("save-btn").disabled = true;
  $("save-status").textContent = "";
  const sendBtn = $("send-btn");
  sendBtn.classList.toggle("hidden", !sendingEnabled);
  sendBtn.disabled = true;
}

// --- Draft generation / save ----------------------------------------------
async function generateDraft() {
  if (!selected) return;
  const btn = $("generate-btn");
  btn.disabled = true; btn.textContent = "Generating…";
  try {
    const body = { tone: $("d-tone").value, language: $("d-language").value };
    const r = await api(`/api/emails/${selected.id}/draft`, { method: "POST", body: JSON.stringify(body) });
    $("d-summary").textContent = r.summary;
    $("d-summary-wrap").classList.remove("hidden");
    $("d-draft").value = r.draft;
    $("save-btn").disabled = false;
    if (sendingEnabled) $("send-btn").disabled = false;
    toast("Draft generated.");
  } catch (e) {
    toast("Generate failed: " + e.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Generate draft";
  }
}

async function saveDraft() {
  if (!selected) return;
  const btn = $("save-btn");
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    const r = await api(`/api/emails/${selected.id}/save-draft`, {
      method: "POST", body: JSON.stringify({ draft: $("d-draft").value }),
    });
    $("save-status").textContent = `Saved to Gmail Drafts (id ${r.draft_id}).`;
    toast("Saved to Gmail Drafts.");
  } catch (e) {
    $("save-status").textContent = "Error: " + e.message;
    toast("Save failed: " + e.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Save to Gmail Drafts";
  }
}

// --- Manual send (explicit, confirmed) -------------------------------------
function openSendModal() {
  if (!selected || !sendingEnabled) return;
  if (!$("d-draft").value.trim()) { toast("Draft is empty.", "error"); return; }
  $("send-modal").classList.remove("hidden");
}

async function confirmSend() {
  $("send-modal").classList.add("hidden");
  if (!selected) return;
  const btn = $("send-btn");
  btn.disabled = true; btn.textContent = "Sending…";
  try {
    const r = await api(`/api/emails/${selected.id}/send`, {
      method: "POST", body: JSON.stringify({ body: $("d-draft").value, confirm: true }),
    });
    $("save-status").textContent = `Sent (message ${r.message_id}).`;
    toast("Email sent.");
  } catch (e) {
    $("save-status").textContent = "Send error: " + e.message;
    toast("Send failed: " + e.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Send email";
  }
}

// --- Bulk generation -------------------------------------------------------
function setBulkCount(n) {
  bulkCount = n;
  document.querySelectorAll(".chip").forEach((c) =>
    c.classList.toggle("active", parseInt(c.dataset.count, 10) === n));
}

async function requestBulk(mode, count) {
  if (bulkRunning) { toast("A bulk run is already in progress.", "warn"); return; }
  try {
    const est = await api("/api/bulk/preview", {
      method: "POST", body: JSON.stringify({ count, mode }),
    });
    pendingBulk = { count, mode };
    $("es-count").textContent = fmtNum(est.emails_selected);
    $("es-in").textContent = fmtNum(est.est_input_tokens);
    $("es-out").textContent = fmtNum(est.est_output_tokens);
    $("es-cost").textContent = fmtMoney4(est.est_cost) + " " + est.currency;
    const warn = $("es-budget");
    if (est.exceeds_budget) {
      warn.textContent = `This operation may exceed your monthly budget ` +
        `(projected ${fmtMoney(est.projected_month_cost)} of ${fmtMoney(est.monthly_budget)}).`;
      warn.classList.remove("hidden");
    } else {
      warn.classList.add("hidden");
    }
    $("estimate-modal").classList.remove("hidden");
  } catch (e) {
    toast("Estimate failed: " + e.message, "error");
  }
}

function startBulk() {
  $("estimate-modal").classList.add("hidden");
  if (!pendingBulk) return;
  const { count, mode } = pendingBulk;
  pendingBulk = null;
  runBulkStream(count, mode);
}

function runBulkStream(count, mode) {
  bulkRunning = true;
  const tone = $("d-tone").value;
  const language = $("d-language").value;
  const prog = $("bulk-progress");
  const report = $("bulk-report");
  const bar = $("bp-bar");
  const text = $("bp-text");
  report.classList.add("hidden");
  prog.classList.remove("hidden");
  bar.style.width = "0%";
  text.textContent = "Starting…";

  const url = `/api/bulk/stream?count=${count}&mode=${encodeURIComponent(mode)}` +
    `&tone=${encodeURIComponent(tone)}&language=${encodeURIComponent(language)}`;
  const es = new EventSource(url);
  let total = 0;

  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "start") {
      total = msg.total;
      text.textContent = total ? `Processing 0 / ${total}` : "No replyable emails found.";
    } else if (msg.type === "progress") {
      const pct = total ? Math.round((msg.index / total) * 100) : 0;
      bar.style.width = pct + "%";
      text.textContent = `Processing ${msg.index} / ${total}`;
    } else if (msg.type === "done") {
      bar.style.width = "100%";
      renderBulkReport(msg.report);
      es.close();
      bulkRunning = false;
      loadEmails();
      toast("Bulk run complete.");
    } else if (msg.type === "error") {
      text.textContent = "Error: " + msg.error;
      es.close();
      bulkRunning = false;
      toast("Bulk failed: " + msg.error, "error");
    }
  };
  es.onerror = () => {
    if (bulkRunning) { text.textContent = "Connection lost."; toast("Bulk connection lost.", "error"); }
    es.close();
    bulkRunning = false;
  };
}

function renderBulkReport(r) {
  const report = $("bulk-report");
  report.classList.remove("hidden");
  report.innerHTML = `
    <div class="report-title">Bulk run report</div>
    <div class="urow"><span>Emails analyzed</span><b>${fmtNum(r.emails_analyzed)}</b></div>
    <div class="urow"><span>Drafts generated</span><b>${fmtNum(r.drafts_generated)}</b></div>
    <div class="urow"><span>Drafts saved</span><b>${fmtNum(r.drafts_saved)}</b></div>
    <div class="urow"><span>Skipped</span><b>${fmtNum(r.skipped)}</b></div>
    <div class="urow"><span>Failures</span><b>${fmtNum(r.failures)}</b></div>
    <div class="urow"><span>Elapsed</span><b>${r.duration_seconds}s</b></div>`;
}

// --- Tabs / Usage dashboard ------------------------------------------------
function showTab(name) {
  currentTab = name;
  const isUsage = name === "usage";
  const isArchive = name === "archive";
  // Inbox and Archive share one mail workspace (and one detail pane); only the
  // left list-pane swaps. Usage is a separate full-width view.
  $("view-mail").classList.toggle("hidden", isUsage);
  $("view-usage").classList.toggle("hidden", !isUsage);
  $("inbox-pane").classList.toggle("hidden", isArchive);
  $("archive-pane").classList.toggle("hidden", !isArchive);
  $("tab-inbox").classList.toggle("active", name === "inbox");
  $("tab-archive").classList.toggle("active", isArchive);
  $("tab-usage").classList.toggle("active", isUsage);
  if (isUsage) loadUsage();
  if (isArchive) enterArchive();
}

function renderBreakdown(el, rows, labelKey) {
  if (!rows.length) { el.innerHTML = `<p class="muted">No usage yet.</p>`; return; }
  el.innerHTML = rows.map((r) => `
    <div class="urow">
      <span>${escapeHtml(r[labelKey])}</span>
      <b>${fmtNum(r.tokens)} tok · ${fmtMoney4(r.cost)}</b>
    </div>`).join("");
}

async function loadUsage() {
  const status = $("usage-status");
  status.textContent = "Loading…";
  try {
    const u = await api("/api/usage/summary");
    $("u-analyzed").textContent = fmtNum(u.today.emails_analyzed);
    $("u-generated").textContent = fmtNum(u.today.drafts_generated);
    $("u-sent").textContent = fmtNum(u.today.drafts_sent);
    $("u-in").textContent = fmtNum(u.today.input_tokens);
    $("u-out").textContent = fmtNum(u.today.output_tokens);
    $("u-tot").textContent = fmtNum(u.today.total_tokens);
    $("u-cost-today").textContent = fmtMoney4(u.today.cost);

    $("u-month-cost").textContent = fmtMoney(u.month.cost);
    $("u-budget").textContent = fmtMoney(u.month.budget);
    $("u-remaining").textContent = fmtMoney(u.month.remaining);
    $("u-m-generated").textContent = fmtNum(u.month.drafts_generated);
    $("u-m-sent").textContent = fmtNum(u.month.drafts_sent);
    $("u-m-tokens").textContent = fmtNum(u.month.total_tokens);
    const pct = Math.max(0, Math.min(100, u.month.pct));
    $("u-pct").textContent = u.month.pct;
    const bar = $("u-bar");
    bar.style.width = pct + "%";
    bar.className = "bar-fill" + (u.month.pct >= 100 ? " over" : u.month.pct >= 80 ? " warn" : "");

    renderBreakdown($("u-providers"), u.providers, "provider");
    renderBreakdown($("u-models"), u.models, "model");

    const daily = $("u-daily");
    if (!u.daily.length) {
      daily.innerHTML = `<p class="muted">No usage yet this month.</p>`;
    } else {
      const maxCost = Math.max(...u.daily.map((d) => d.cost), 0.0001);
      daily.innerHTML = u.daily.map((d) => `
        <div class="daily-row">
          <span class="daily-date">${d.date.slice(5)}</span>
          <div class="daily-bar"><div class="daily-fill" style="width:${Math.round((d.cost / maxCost) * 100)}%"></div></div>
          <span class="daily-cost">${fmtMoney4(d.cost)}</span>
        </div>`).join("");
    }
    status.textContent = "";
  } catch (e) {
    status.textContent = "Error: " + e.message;
  }
}

// ====================== Settings modal ======================================
function openSettings(pane) {
  $("settings-modal").classList.remove("hidden");
  loadSettingsInto();
  loadConfigInto();
  refreshGmailStatus();
  if (pane) switchSettingsPane(pane);
}

function switchSettingsPane(pane) {
  document.querySelectorAll(".set-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.pane === pane));
  document.querySelectorAll(".set-pane").forEach((p) =>
    p.classList.toggle("hidden", p.id !== "pane-" + pane));
}

function populateModelDropdown(provider, current) {
  const sel = $("s-model");
  const models = availableModels[provider] || [];
  sel.innerHTML = models.map((m) => `<option value="${m}">${m}</option>`).join("");
  if (current && models.includes(current)) sel.value = current;
}

function onProviderChange() {
  const provider = $("s-provider").value;
  $("s-apikey-label").childNodes[0].nodeValue =
    provider === "anthropic" ? "Anthropic API key " : "OpenAI API key ";
  const masked = settingsCache
    ? (provider === "anthropic" ? settingsCache.anthropic_api_key : settingsCache.openai_api_key)
    : "";
  $("s-apikey").placeholder = masked || (provider === "anthropic" ? "sk-ant-…" : "sk-…");
  $("s-apikey").value = "";
  // Keep the saved model only when it still belongs to this provider.
  const keep = settingsCache && settingsCache.llm_provider === provider ? settingsCache.llm_model : "";
  populateModelDropdown(provider, keep);
}

async function loadSettingsInto() {
  try {
    const s = await api("/api/settings");
    settingsCache = s;
    availableModels = s.available_models || availableModels;
    const provider = ["openai", "anthropic"].includes(s.llm_provider) ? s.llm_provider : "openai";
    $("s-provider").value = provider;
    onProviderChange();
    populateModelDropdown(provider, s.llm_model);
    $("s-temp").value = s.llm_temperature != null ? s.llm_temperature : 0.2;
    $("s-temp-val").textContent = $("s-temp").value;
    $("s-budget").value = s.monthly_budget != null ? s.monthly_budget : "";
    $("g-send-toggle").checked = !!s.email_sending_enabled;
    $("ai-status").textContent = "";
  } catch (e) {
    toast("Failed to load settings: " + e.message, "error");
  }
}

async function saveAiSettings() {
  const provider = $("s-provider").value;
  const body = {
    llm_provider: provider,
    llm_model: $("s-model").value,
    llm_temperature: parseFloat($("s-temp").value),
  };
  const key = $("s-apikey").value.trim();
  if (key) {
    if (provider === "anthropic") body.anthropic_api_key = key;
    else body.openai_api_key = key;
  }
  $("ai-status").textContent = "Saving…";
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    // Reply-behaviour fields live in user config.
    await saveConfig(true);
    $("ai-status").textContent = "Saved.";
    toast("AI settings saved.");
    refreshHealth();
    loadConfig();
    loadSettingsInto();
  } catch (e) {
    $("ai-status").textContent = "Error: " + e.message;
    toast("Save failed: " + e.message, "error");
  }
}

async function saveBudget() {
  $("budget-status").textContent = "Saving…";
  try {
    const v = $("s-budget").value;
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ monthly_budget: v === "" ? 0 : parseFloat(v) }),
    });
    $("budget-status").textContent = "Saved.";
    toast("Budget saved.");
  } catch (e) {
    $("budget-status").textContent = "Error: " + e.message;
  }
}

// ====================== Gmail connection ====================================
async function refreshGmailStatus() {
  try {
    const g = await api("/api/gmail/status");
    gmailCache = g;
    if (g.email) connectedEmail = g.email;
    renderGmailStatus(g);
  } catch (e) {
    toast("Gmail status error: " + e.message, "error");
  }
  // Connection state may have just changed — force a fresh setup/status.
  renderOnboarding(true);
}

function renderGmailStatus(g) {
  const badge = $("g-badge");
  const details = $("g-details");
  const connectBtn = $("g-connect");
  const disconnectBtn = $("g-disconnect");
  const credsMissing = $("g-creds-missing");
  const credsLabel = $("g-creds-label");

  // Credentials are always uploadable; the label/warning reflect their state.
  credsMissing.classList.toggle("hidden", !!g.credentials_available);
  credsLabel.textContent = g.credentials_available
    ? "Replace your Google OAuth client file"
    : "Upload your Google OAuth client file";

  if (g.connected) {
    badge.textContent = "✓ Connected";
    badge.className = "gmail-badge on";
    details.classList.remove("hidden");
    $("g-email").textContent = g.email || "–";
    const tok = g.valid ? "✓ Valid" : (g.expired ? "⚠ Expired (auto-refresh)" : "⚠ Needs reconnect");
    $("g-token").textContent = tok;
    $("g-token").className = g.valid ? "ok-text" : "warn-text";
    $("g-refresh").textContent = g.last_refresh ? new Date(g.last_refresh).toLocaleString() : "–";
    const perms = ["Read", "Draft"];
    if (g.modify_scope) perms.push("Actions");
    if (g.send_scope) perms.push("Send");
    $("g-scopes").textContent = perms.join(" · ") + (g.modify_scope ? "" : " · (reconnect to enable actions)");
    $("g-oauth").textContent = g.error ? ("⚠ " + g.error) : "OAuth client present · token stored server-side";
    $("g-oauth").className = g.error ? "warn-text" : "ok-text";
    connectBtn.textContent = "Reconnect Gmail";
    disconnectBtn.classList.remove("hidden");
  } else {
    badge.textContent = g.error ? "⚠ Error" : "Not connected";
    badge.className = "gmail-badge " + (g.error ? "err" : "off");
    details.classList.toggle("hidden", !g.error);
    if (g.error) {
      $("g-email").textContent = "–";
      $("g-token").textContent = "–";
      $("g-refresh").textContent = "–";
      $("g-scopes").textContent = "–";
      $("g-oauth").textContent = "⚠ " + g.error;
      $("g-oauth").className = "warn-text";
    }
    connectBtn.textContent = "Connect Gmail";
    disconnectBtn.classList.add("hidden");
  }
  connectBtn.disabled = !g.credentials_available;
  connectBtn.title = g.credentials_available ? "" : "Upload credentials.json first";
}

// ====================== Onboarding banner + setup checklist =================
function setCheck(key, done) {
  const li = document.querySelector(`#ob-checklist li[data-key="${key}"]`);
  if (!li) return;
  li.classList.toggle("done", !!done);
  li.querySelector(".ck").textContent = done ? "✓" : "○";
}

async function renderOnboarding(force = false) {
  // Once fully configured the banner stays hidden, so an idle refresh need not
  // re-hit setup/status (and Gmail) at all — only re-check when asked to.
  if (appConfigured && !force) { $("onboarding").classList.add("hidden"); return; }
  let s;
  try { s = await fetchSetupStatus(force); } catch (_) { return; }
  appConfigured = !!s.configured;
  if (s.gmail_email) connectedEmail = s.gmail_email;
  // A blocking filesystem problem takes priority over onboarding: surface it
  // and hide the setup checklist (which can't proceed until disk is fixed).
  if (s.filesystem_ok === false) {
    renderFsIssues({ issues: s.filesystem_issues || [] });
    $("onboarding").classList.add("hidden");
    return;
  }
  const banner = $("onboarding");
  const credsOk = !!s.credentials_available;
  const dbOk = lastHealth ? lastHealth.database.status === "ok" : true;

  setCheck("llm", s.llm_ready);
  setCheck("creds", credsOk);
  setCheck("gmail", s.gmail_connected);
  setCheck("db", dbOk);

  const fullyReady = s.llm_ready && s.gmail_connected;
  banner.classList.toggle("hidden", fullyReady);
  if (fullyReady) return;

  // Tailor the message + which actions are most relevant right now.
  let msg, title;
  if (!credsOk) {
    title = "Connect Gmail to get started";
    msg = "Gmail is not connected. Upload your Google OAuth credentials.json, then click Connect Gmail.";
  } else if (!s.gmail_connected) {
    title = "Connect Gmail to get started";
    msg = "Credentials uploaded. Now click Connect Gmail and sign in with Google.";
  } else if (!s.llm_ready) {
    title = "Add your AI provider key";
    msg = "Gmail is connected. Open Settings → AI and add your OpenAI or Anthropic API key.";
  } else {
    title = "Finish setup";
    msg = "";
  }
  $("ob-title").textContent = title;
  $("ob-msg").textContent = msg;

  $("ob-upload").classList.toggle("hidden", credsOk);
  $("ob-connect").classList.toggle("hidden", s.gmail_connected || !credsOk);
}

async function connectGmail() {
  try {
    const { auth_url } = await api("/api/gmail/connect");
    // Full-page redirect to Google; the server callback returns us to the app.
    window.location.href = auth_url;
  } catch (e) {
    toast("Could not start Gmail connection: " + e.message, "error");
  }
}

function openDisconnect() { $("disconnect-modal").classList.remove("hidden"); }

async function confirmDisconnect() {
  $("disconnect-modal").classList.add("hidden");
  try {
    await api("/api/gmail/disconnect", { method: "POST" });
    toast("Gmail disconnected.");
    refreshGmailStatus();
    refreshHealth();
  } catch (e) {
    toast("Disconnect failed: " + e.message, "error");
  }
}

function readFileText(input) {
  return new Promise((resolve, reject) => {
    const file = input.files && input.files[0];
    if (!file) { reject(new Error("No file selected.")); return; }
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Could not read file."));
    reader.readAsText(file);
  });
}

async function uploadCredentials(inputId, statusId, onDone) {
  const status = $(statusId);
  status.textContent = "Uploading…";
  try {
    const text = await readFileText($(inputId));
    await api("/api/gmail/credentials", { method: "POST", body: JSON.stringify({ credentials: text }) });
    status.textContent = "✓ Stored.";
    toast("Google OAuth client stored.");
    if (onDone) onDone();
  } catch (e) {
    status.textContent = "Error: " + e.message;
    toast("Upload failed: " + e.message, "error");
  }
}

async function toggleSending() {
  const enabled = $("g-send-toggle").checked;
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ enable_email_sending: enabled }) });
    toast(enabled ? "Sending enabled — reconnect Gmail to grant the send permission." : "Sending disabled.");
    refreshHealth();
    loadConfig();
    refreshGmailStatus();
  } catch (e) {
    $("g-send-toggle").checked = !enabled;
    toast("Could not change sending: " + e.message, "error");
  }
}

// ====================== User config (AI prefs + filtering) ==================
function linesToList(text) {
  return (text || "").split("\n").map((l) => l.trim()).filter(Boolean);
}

async function loadConfigInto() {
  try {
    const c = await api("/api/config");
    userConfig = c;
    $("c-prompt").value = c.custom_prompt || "";
    $("c-signature").value = c.signature || "";
    $("c-language").value = c.default_language || "auto";
    $("c-tone").value = c.default_tone || "professional";
    $("c-banned").value = (c.banned_senders || []).join("\n");
    $("c-allowed").value = (c.allowed_senders || []).join("\n");
    $("c-keywords").value = (c.ignore_keywords || []).join("\n");
    $("c-threshold").value = c.replyability_threshold;
    const w = c.replyability_weights || {};
    $("w-question").value = w.question_detected;
    $("w-known").value = w.known_contact;
    $("w-personal").value = w.personal_sender;
    $("w-newsletter").value = w.contains_newsletter;
    $("w-noreply").value = w.noreply_sender;
  } catch (e) { toast("Failed to load config: " + e.message, "error"); }
}

function buildConfigPayload() {
  return {
    custom_prompt: $("c-prompt").value,
    signature: $("c-signature").value,
    default_language: $("c-language").value,
    default_tone: $("c-tone").value,
    banned_senders: linesToList($("c-banned").value),
    allowed_senders: linesToList($("c-allowed").value),
    ignore_keywords: linesToList($("c-keywords").value),
    replyability_threshold: parseInt($("c-threshold").value, 10) || 0,
    replyability_weights: {
      question_detected: parseInt($("w-question").value, 10) || 0,
      known_contact: parseInt($("w-known").value, 10) || 0,
      personal_sender: parseInt($("w-personal").value, 10) || 0,
      contains_newsletter: parseInt($("w-newsletter").value, 10) || 0,
      noreply_sender: parseInt($("w-noreply").value, 10) || 0,
    },
    show_only_replyable: filterNeedsReply,
  };
}

async function saveConfig(silent) {
  userConfig = await api("/api/config", { method: "POST", body: JSON.stringify(buildConfigPayload()) });
  if (!silent) {
    toast("Saved.");
    loadEmails();
  }
}

async function saveFiltering() {
  $("filter-status").textContent = "Saving…";
  try {
    await saveConfig(true);
    $("filter-status").textContent = "Saved.";
    toast("Filtering rules saved.");
    loadEmails();
  } catch (e) {
    $("filter-status").textContent = "Error: " + e.message;
    toast("Save failed: " + e.message, "error");
  }
}

// Non-secret config that affects the inbox UI (sending toggle + user config).
async function loadConfig() {
  try {
    const s = await api("/api/settings");
    sendingEnabled = !!s.email_sending_enabled;
    // Only surface the safety indicator when sending is actually enabled.
    $("mode-tag").classList.toggle("hidden", !sendingEnabled);
    if (selected) $("send-btn").classList.toggle("hidden", !sendingEnabled);
  } catch (_) {}
  try {
    userConfig = await api("/api/config");
    // "Needs reply" chip mirrors the long-standing show_only_replyable pref.
    filterNeedsReply = !!userConfig.show_only_replyable;
    setFilterChip("f-needsreply", filterNeedsReply);
    if (lastEmails.length) renderList();
  } catch (_) {}
}

// --- Inbox controls: search, status, page size, filter chips ----------------
function setFilterChip(id, on) {
  const el = $(id);
  if (el) el.classList.toggle("active", !!on);
}
function applyStatus(status) {
  if (status === statusFilter) return;
  statusFilter = status;
  document.querySelectorAll("#status-seg .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.status === status));
  loadEmails(); // status maps to the server query → refetch
}
function toggleAttachments() {
  filterAttachments = !filterAttachments;
  setFilterChip("f-attach", filterAttachments);
  renderList();
}
async function toggleNeedsReply() {
  filterNeedsReply = !filterNeedsReply;
  setFilterChip("f-needsreply", filterNeedsReply);
  renderList();
  if (userConfig) {
    try {
      userConfig.show_only_replyable = filterNeedsReply;
      await api("/api/config", { method: "POST", body: JSON.stringify(userConfig) });
    } catch (_) {}
  }
}
function isModalOpen() {
  return !!document.querySelector(".modal:not(.hidden)");
}

// ====================== First-run setup wizard ==============================
let wizProvider = "openai";

function showWizardStep(n) {
  for (let i = 1; i <= 4; i++) $("wiz-" + i).classList.toggle("hidden", i !== n);
  document.querySelectorAll(".wiz-step").forEach((s) =>
    s.classList.toggle("active", parseInt(s.dataset.step, 10) <= n));
}

async function maybeStartWizard() {
  const params = new URLSearchParams(window.location.search);
  const gmailParam = params.get("gmail");
  if (gmailParam) {
    // Clean the URL so a refresh doesn't re-trigger.
    window.history.replaceState({}, "", window.location.pathname);
  }
  let status;
  // Just back from OAuth (gmail param present) -> force fresh; else share cache.
  try { status = await fetchSetupStatus(!!gmailParam); } catch (_) { return; }

  // Don't launch the wizard while a filesystem problem blocks startup — the
  // installation banner explains what to fix first.
  if (status.filesystem_ok === false) {
    renderFsIssues({ issues: status.filesystem_issues || [] });
    return;
  }

  if (gmailParam === "error") {
    toast("Gmail connection failed: " + (params.get("detail") || "unknown"), "error");
  }

  // OAuth just succeeded: refresh everything and enable email loading.
  if (gmailParam === "connected") {
    toast("Gmail connected as " + (status.gmail_email || ""));
    refreshGmailStatus();
    refreshHealth();
    loadEmails();
  }

  renderOnboarding();

  if (status.configured) {
    return; // fully set up — no wizard
  }

  // Resume the wizard at the first incomplete step.
  $("wizard").classList.remove("hidden");
  wizProvider = status.llm_provider || "openai";
  $("wiz-creds-block").classList.toggle("hidden", !!status.credentials_available);
  if (!status.llm_ready) {
    showWizardStep(1);
  } else if (!status.gmail_connected) {
    showWizardStep(3);
  } else {
    finishWizardStep4(status);
  }
}

function pickProvider(p) {
  wizProvider = p;
  $("wiz-provider-name").textContent = p === "anthropic" ? "Anthropic" : "OpenAI";
  $("wiz-apikey").placeholder = p === "anthropic" ? "sk-ant-…" : "sk-…";
  showWizardStep(2);
}

async function wizSaveKey() {
  const key = $("wiz-apikey").value.trim();
  if (!key) { $("wiz-key-status").textContent = "Please paste your API key."; return; }
  $("wiz-key-status").textContent = "Saving…";
  const body = { llm_provider: wizProvider };
  if (wizProvider === "anthropic") body.anthropic_api_key = key;
  else body.openai_api_key = key;
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    $("wiz-key-status").textContent = "";
    refreshHealth();
    // Refresh credentials availability before the Gmail step.
    const st = await api("/api/setup/status");
    $("wiz-creds-block").classList.toggle("hidden", !!st.credentials_available);
    showWizardStep(3);
  } catch (e) {
    $("wiz-key-status").textContent = "Error: " + e.message;
  }
}

async function finishWizardStep4(status) {
  showWizardStep(4);
  const s = status || (await api("/api/setup/status"));
  $("wiz-done-provider").textContent = (s.llm_provider || "—") + (s.llm_ready ? " ✓" : "");
  $("wiz-done-gmail").textContent = s.gmail_connected ? (s.gmail_email + " ✓") : "not connected";
}

// ====================== Wire up =============================================
// Event delegation: one click/change listener per list, attached once, instead
// of three listeners per row rebuilt on every render.
wireEmailListDelegation("email-list", () => lastEmails, toggleSelect, "inbox");
wireEmailListDelegation("arch-email-list", () => archiveEmails, toggleArchiveSelect, "archive");
const debouncedRenderList = debounce(renderList, 120);
const debouncedRenderArchive = debounce(renderArchiveList, 120);

$("refresh-btn").addEventListener("click", loadEmails);
$("generate-btn").addEventListener("click", generateDraft);
$("save-btn").addEventListener("click", saveDraft);
$("send-btn").addEventListener("click", openSendModal);
$("send-cancel").addEventListener("click", () => $("send-modal").classList.add("hidden"));
$("send-confirm").addEventListener("click", confirmSend);
$("tab-inbox").addEventListener("click", () => showTab("inbox"));
$("tab-archive").addEventListener("click", () => showTab("archive"));
$("tab-usage").addEventListener("click", () => showTab("usage"));

// Archive workspace
$("arch-refresh").addEventListener("click", () => { invalidateArchiveCache(); loadFolders({ force: true }); });
$("arch-back").addEventListener("click", backToFolders);
$("arch-search").addEventListener("input", (e) => { archiveSearch = e.target.value; debouncedRenderArchive(); });
$("arch-select-all").addEventListener("change", (e) => onArchiveSelectAll(e.target.checked));
$("arch-restore").addEventListener("click", () => doRestore(archActionIds()));
$("arch-smart").addEventListener("click", () => openSmartArchive(archActionIds(), "archive"));
$("arch-clear").addEventListener("click", clearArchiveSelection);
// arch-generate / arch-read / arch-unread are wired via the archive More menu.
$("arch-load-more").addEventListener("click", () => loadFolderEmails(false));

document.querySelectorAll(".chip").forEach((c) =>
  c.addEventListener("click", () => setBulkCount(parseInt(c.dataset.count, 10))));
$("bulk-gensave").addEventListener("click", () => requestBulk("generate_save", bulkCount));
$("bulk-gensave-all").addEventListener("click", () => requestBulk("generate_save", 100));
$("es-cancel").addEventListener("click", () => { $("estimate-modal").classList.add("hidden"); pendingBulk = null; });
$("es-continue").addEventListener("click", startBulk);

// Inbox controls (search, status, page size, filters, selection, action bar)
$("search-box").addEventListener("input", (e) => { searchQuery = e.target.value; debouncedRenderList(); });
$("page-size").addEventListener("change", (e) => { pageSize = e.target.value; loadEmails(); });
document.querySelectorAll("#status-seg .seg-btn").forEach((b) =>
  b.addEventListener("click", () => applyStatus(b.dataset.status)));
$("f-attach").addEventListener("click", toggleAttachments);
$("f-needsreply").addEventListener("click", toggleNeedsReply);
$("select-all").addEventListener("change", (e) => onSelectAll(e.target.checked));
$("ab-generate").addEventListener("click", runSelectionDrafts);
$("ab-smart").addEventListener("click", () => openSmartArchive(actionIds()));
$("ab-clear").addEventListener("click", clearSelection);
// More actions menus (inbox + archive share one open/close helper).
function toggleMenu(menuId, btnId, open) {
  const menu = $(menuId);
  if (!menu) return;
  const show = open === undefined ? menu.classList.contains("hidden") : open;
  menu.classList.toggle("hidden", !show);
  const btn = $(btnId); if (btn) btn.setAttribute("aria-expanded", show ? "true" : "false");
}
function closeAllMenus() {
  toggleMenu("ab-menu", "ab-more", false);
  toggleMenu("arch-menu", "arch-more-btn", false);
}
const closeMoreMenu = closeAllMenus; // back-compat alias
// Inbox selection menu
$("ab-more").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("ab-menu", "ab-more"); });
$("ab-move").addEventListener("click", () => { closeAllMenus(); openLabelModal(actionIds(), "move"); });
$("ab-label").addEventListener("click", () => { closeAllMenus(); openLabelModal(actionIds(), "label"); });
$("ab-read").addEventListener("click", () => { closeAllMenus(); doMarkRead(actionIds()); });
$("ab-unread").addEventListener("click", () => { closeAllMenus(); doMarkUnread(actionIds()); });
$("ab-clear-menu").addEventListener("click", () => { closeAllMenus(); clearSelection(); });
// Archive selection menu
$("arch-more-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("arch-menu", "arch-more-btn"); });
$("arch-generate").addEventListener("click", () => { closeAllMenus(); runArchiveDrafts(); });
$("arch-read").addEventListener("click", () => { closeAllMenus(); doArchMarkRead(archActionIds()); });
$("arch-unread").addEventListener("click", () => { closeAllMenus(); doArchMarkUnread(archActionIds()); });
$("arch-clear-menu").addEventListener("click", () => { closeAllMenus(); clearArchiveSelection(); });
document.addEventListener("click", (e) => { if (!e.target.closest(".menu-wrap")) closeAllMenus(); });
$("label-cancel").addEventListener("click", () => $("label-modal").classList.add("hidden"));
$("label-apply").addEventListener("click", applyLabel);
$("smart-cancel").addEventListener("click", () => $("smart-modal").classList.add("hidden"));
$("smart-confirm").addEventListener("click", onSmartConfirm);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || isModalOpen()) return;
  const menuOpen = !$("ab-menu").classList.contains("hidden") || !$("arch-menu").classList.contains("hidden");
  if (menuOpen) { closeAllMenus(); return; }
  if (currentTab === "archive" && archiveSelectedIds.size) clearArchiveSelection();
  else if (selectedIds.size) clearSelection();
});
setFilterChip("f-needsreply", filterNeedsReply);

// Settings
$("settings-btn").addEventListener("click", () => openSettings("ai"));
$("set-close").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
document.querySelectorAll(".set-tab").forEach((b) =>
  b.addEventListener("click", () => switchSettingsPane(b.dataset.pane)));
$("s-provider").addEventListener("change", onProviderChange);
$("s-temp").addEventListener("input", () => { $("s-temp-val").textContent = $("s-temp").value; });
$("ai-save").addEventListener("click", saveAiSettings);
$("filter-save").addEventListener("click", saveFiltering);
$("budget-save").addEventListener("click", saveBudget);
$("goto-usage").addEventListener("click", (e) => {
  e.preventDefault();
  $("settings-modal").classList.add("hidden");
  showTab("usage");
});

// Gmail
$("g-connect").addEventListener("click", connectGmail);
$("g-disconnect").addEventListener("click", openDisconnect);
$("dc-cancel").addEventListener("click", () => $("disconnect-modal").classList.add("hidden"));
$("dc-confirm").addEventListener("click", confirmDisconnect);
$("g-send-toggle").addEventListener("change", toggleSending);
$("g-creds-file").addEventListener("change", () =>
  uploadCredentials("g-creds-file", "g-creds-status", refreshGmailStatus));

// Onboarding banner
$("ob-connect").addEventListener("click", connectGmail);
$("ob-settings").addEventListener("click", () => openSettings("gmail"));
$("ob-upload").addEventListener("click", () => $("ob-creds-file").click());
$("ob-creds-file").addEventListener("change", () =>
  uploadCredentials("ob-creds-file", "ob-status", () => { refreshGmailStatus(); renderOnboarding(); }));

// Wizard
document.querySelectorAll(".provider-pick").forEach((b) =>
  b.addEventListener("click", () => pickProvider(b.dataset.provider)));
$("wiz-back-2").addEventListener("click", () => showWizardStep(1));
$("wiz-save-key").addEventListener("click", wizSaveKey);
$("wiz-connect").addEventListener("click", connectGmail);
$("wiz-creds-file").addEventListener("change", () =>
  uploadCredentials("wiz-creds-file", "wiz-creds-status", async () => {
    $("wiz-creds-block").classList.add("hidden");
  }));
$("wiz-finish").addEventListener("click", () => $("wizard").classList.add("hidden"));

// --- Health polling (visibility-aware) --------------------------------------
// Only poll while the tab is visible, and refresh immediately when it becomes
// visible again. A hidden tab makes zero background calls (no wasted Gmail/LLM
// hits or quota burn); the interval is 60s since health is just a heartbeat.
let healthTimer = null;
function startHealthPolling() {
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(() => { if (!document.hidden) refreshHealth(); }, 60000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshHealth(); });

// Boot. Skipped under the test harness (window.__APP_TEST__) so specs can wire
// fetch stubs and drive flows deterministically without the auto-boot firing.
if (!window.__APP_TEST__) {
  refreshHealth();
  loadConfig();
  maybeStartWizard();
  startHealthPolling();
}
