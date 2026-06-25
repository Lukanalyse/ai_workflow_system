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

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtNum(n) { return (Number(n) || 0).toLocaleString("en-US"); }
function fmtMoney(n) { return "$" + (Number(n) || 0).toFixed(2); }
function fmtMoney4(n) { return "$" + (Number(n) || 0).toFixed(4); }

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
// Importance dot. Colored when a level is known (Phase 6); a neutral placeholder
// otherwise, so the slot already exists in the layout.
function importanceDot(em) {
  const lvl = String(em.importance || "").toLowerCase();
  const cls = lvl === "high" ? "high" : (lvl === "medium" || lvl === "med") ? "med" : lvl === "low" ? "low" : "none";
  const title = lvl ? `Importance: ${lvl}` : "Importance — coming soon";
  return `<span class="imp-dot imp-${cls}" title="${escapeHtml(title)}"></span>`;
}
// Non-score metadata badges shared by the list rows and the read pane.
function metaBadges(em) {
  const out = [];
  if (em.already_processed) out.push(`<span class="badge seen">handled</span>`);
  const nAtt = attachmentCount(em);
  if (nAtt) out.push(`<span class="badge attach">📎 ${nAtt}</span>`);
  if (em.category) {
    const slug = String(em.category).toLowerCase().replace(/[^a-z0-9]+/g, "-");
    out.push(`<span class="badge cat cat-${escapeHtml(slug)}">${escapeHtml(em.category)}</span>`);
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

// --- Health ----------------------------------------------------------------
let lastHealth = null; // last /api/health response (drives the DB checklist item)
let fsBlocked = false; // true when a blocking filesystem issue prevents startup
async function refreshHealth() {
  const el = $("health");
  try {
    const h = await api("/api/health");
    lastHealth = h;
    el.className = "health " + (h.status === "ok" ? "ok" : h.status === "degraded" ? "degraded" : "error");
    el.textContent = `Gmail: ${h.gmail.status} · LLM: ${h.llm.status} · DB: ${h.database.status}`;
    el.title = `${h.gmail.detail}\n${h.llm.detail}\n${h.database.detail}`;
    renderFsIssues(h.filesystem);
    renderOnboarding();
  } catch (e) {
    el.className = "health error";
    el.textContent = "health unavailable";
    el.title = String(e);
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

// --- Email list ------------------------------------------------------------
async function loadEmails() {
  const empty = $("list-empty");
  empty.textContent = "Loading…";
  $("email-list").innerHTML = "";
  // A fresh fetch invalidates any prior selection (different rows on screen).
  clearSelection();
  try {
    const params = new URLSearchParams({ max: pageSize, status: statusFilter });
    const { emails } = await api(`/api/emails?${params}`);
    lastEmails = emails;
    renderList();
  } catch (e) {
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
  list.innerHTML = "";
  const shown = visibleEmails();
  if (!lastEmails.length) {
    empty.textContent = "No emails found for this view.";
  } else if (!shown.length) {
    empty.textContent = "No emails match the current search/filters.";
  } else {
    empty.textContent = "";
    shown.forEach((em) => list.appendChild(renderItem(em)));
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

function renderItem(em) {
  const li = document.createElement("li");
  const isSel = selectedIds.has(em.id);
  li.className = "email-item" + (em.is_unread ? " unread" : "") + (isSel ? " selected" : "");
  li.dataset.id = em.id;
  const av = avatarFor(em);
  const badges = [`<span class="badge score ${em.replyable ? "yes" : "no"}">score ${em.score}</span>`, ...metaBadges(em)];
  const dateTitle = escapeHtml(new Date(em.received_at).toLocaleString());
  li.innerHTML = `
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
      <div class="badges">${badges.join("")}</div>
    </div>`;
  // Checkbox toggles selection without opening the detail view.
  const label = li.querySelector(".row-check");
  const cb = li.querySelector(".row-cb");
  label.addEventListener("click", (e) => e.stopPropagation());
  cb.addEventListener("change", (e) => toggleSelect(em.id, e.target.checked, li));
  li.addEventListener("click", () => selectEmail(em, li));
  return li;
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

function comingSoon(name) { toast(`${name} — coming soon`, "warn"); }

function setActionStatus(text) { const el = $("ab-status"); if (el) el.textContent = text || ""; }

// Generate + save Gmail drafts for the selected emails. Reuses the existing
// per-email endpoints (no new backend), so it is safe and incremental. Phase 5
// can swap this for a dedicated server-side bulk endpoint.
async function runSelectionDrafts() {
  if (selectionBusy) return;
  const ids = [...selectedIds];
  if (!ids.length) return;
  selectionBusy = true;
  const btn = $("ab-generate");
  btn.disabled = true;
  const tone = $("d-tone") ? $("d-tone").value : (userConfig && userConfig.default_tone) || "professional";
  const language = $("d-language") ? $("d-language").value : (userConfig && userConfig.default_language) || "auto";
  let ok = 0, fail = 0;
  for (let i = 0; i < ids.length; i++) {
    setActionStatus(`Generating ${i + 1}/${ids.length}…`);
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
  setActionStatus("");
  btn.disabled = false;
  selectionBusy = false;
  toast(`Drafts saved: ${ok}${fail ? ` · ${fail} failed` : ""}.`, fail ? "warn" : "ok");
  loadEmails();
}

function selectEmail(em, li) {
  selected = em;
  document.querySelectorAll(".email-item.active").forEach((n) => n.classList.remove("active"));
  li.classList.add("active");
  $("detail-empty").classList.add("hidden");
  $("detail").classList.remove("hidden");
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
  renderAttachments($("d-attachments"), em.attachments);
  $("d-snippet").textContent = em.snippet;
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
  const inbox = name === "inbox";
  $("view-inbox").classList.toggle("hidden", !inbox);
  $("view-usage").classList.toggle("hidden", inbox);
  $("tab-inbox").classList.toggle("active", inbox);
  $("tab-usage").classList.toggle("active", !inbox);
  if (!inbox) loadUsage();
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
  renderOnboarding();
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
    $("g-scopes").textContent = g.send_scope ? "Read · Draft · Send" : "Read · Draft";
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

async function renderOnboarding() {
  let s;
  try { s = await api("/api/setup/status"); } catch (_) { return; }
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
    $("mode-tag").textContent = sendingEnabled ? "send enabled" : "draft-only";
    $("mode-tag").classList.toggle("danger-tag", sendingEnabled);
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
  try { status = await api("/api/setup/status"); } catch (_) { return; }

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
$("refresh-btn").addEventListener("click", loadEmails);
$("generate-btn").addEventListener("click", generateDraft);
$("save-btn").addEventListener("click", saveDraft);
$("send-btn").addEventListener("click", openSendModal);
$("send-cancel").addEventListener("click", () => $("send-modal").classList.add("hidden"));
$("send-confirm").addEventListener("click", confirmSend);
$("tab-inbox").addEventListener("click", () => showTab("inbox"));
$("tab-usage").addEventListener("click", () => showTab("usage"));

document.querySelectorAll(".chip").forEach((c) =>
  c.addEventListener("click", () => setBulkCount(parseInt(c.dataset.count, 10))));
$("bulk-gensave").addEventListener("click", () => requestBulk("generate_save", bulkCount));
$("bulk-gensave-all").addEventListener("click", () => requestBulk("generate_save", 100));
$("es-cancel").addEventListener("click", () => { $("estimate-modal").classList.add("hidden"); pendingBulk = null; });
$("es-continue").addEventListener("click", startBulk);

// Inbox controls (search, status, page size, filters, selection, action bar)
$("search-box").addEventListener("input", (e) => { searchQuery = e.target.value; renderList(); });
$("page-size").addEventListener("change", (e) => { pageSize = e.target.value; loadEmails(); });
document.querySelectorAll("#status-seg .seg-btn").forEach((b) =>
  b.addEventListener("click", () => applyStatus(b.dataset.status)));
$("f-attach").addEventListener("click", toggleAttachments);
$("f-needsreply").addEventListener("click", toggleNeedsReply);
$("select-all").addEventListener("change", (e) => onSelectAll(e.target.checked));
$("ab-generate").addEventListener("click", runSelectionDrafts);
$("ab-clear").addEventListener("click", clearSelection);
document.querySelectorAll(".ab-soon").forEach((b) =>
  b.addEventListener("click", () => comingSoon(b.dataset.soon)));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && selectedIds.size && !isModalOpen()) clearSelection();
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

// Boot
refreshHealth();
loadConfig();
maybeStartWizard();
setInterval(refreshHealth, 30000);
