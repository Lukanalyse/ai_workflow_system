// Frontend safety-net: the critical user flows. Deliberately scoped — this is
// not full coverage, just the parcours we must not break while iterating on UX:
// inbox load, Inbox/Archive/labels nav, opening an email, archive/restore,
// read-state sync, and network error handling (timeout + retry).

import test from "node:test";
import assert from "node:assert/strict";
import { makeApp, cand, folder, click, waitFor } from "./harness.mjs";

const $ = (doc, id) => doc.getElementById(id);
const rows = (doc, sel) => doc.querySelectorAll(`${sel} .email-item:not(.skel)`);

// --- Inbox load -------------------------------------------------------------
test("inbox load renders fetched emails", async () => {
  const app = makeApp({ routes: { "GET /api/emails": { emails: [cand("m1", "Hello"), cand("m2", "World")] } } });
  await app.window.loadEmails();
  assert.equal(rows(app.document, "#email-list").length, 2);
  assert.match($(app.document, "result-count").textContent, /2 emails/);
  assert.equal(app.callsTo("GET", "/api/emails").length, 1);
});

test("inbox 'needs reply' filter hides non-replyable by default", async () => {
  const app = makeApp({ routes: { "GET /api/emails": { emails: [cand("m1"), cand("m2", "Spam", { replyable: false })] } } });
  await app.window.loadEmails();
  // Default filterNeedsReply=true -> only the replyable email shows.
  assert.equal(rows(app.document, "#email-list").length, 1);
  assert.match($(app.document, "result-count").textContent, /1 of 2/);
});

// --- Open an email ----------------------------------------------------------
test("clicking an email opens the read pane", async () => {
  const app = makeApp({ routes: { "GET /api/emails": { emails: [cand("m1", "Quarterly report")] } } });
  await app.window.loadEmails();
  click(app.window, app.document.querySelector("#email-list .email-item .subj"));
  assert.equal($(app.document, "d-subject").textContent, "Quarterly report");
  assert.ok(!$(app.document, "detail").classList.contains("hidden"));
  assert.ok($(app.document, "detail-empty").classList.contains("hidden"));
});

// --- Inbox / Archive navigation ---------------------------------------------
test("switching tabs toggles inbox/archive panes", async () => {
  const app = makeApp({ routes: { "GET /api/archive/folders": { folders: [] } } });
  app.window.showTab("archive");
  assert.ok($(app.document, "inbox-pane").classList.contains("hidden"));
  assert.ok(!$(app.document, "archive-pane").classList.contains("hidden"));
  assert.ok($(app.document, "tab-archive").classList.contains("active"));
  app.window.showTab("inbox");
  assert.ok(!$(app.document, "inbox-pane").classList.contains("hidden"));
  assert.ok($(app.document, "archive-pane").classList.contains("hidden"));
});

test("archive folders (labels) render with counts", async () => {
  const app = makeApp({ routes: { "GET /api/archive/folders": { folders: [folder("L1", "Finance", 24, 5), folder("L2", "Personal", 6, 0)] } } });
  await app.window.loadFolders();
  const cards = app.document.querySelectorAll("#folder-grid .folder-card:not(.skel)");
  assert.equal(cards.length, 2);
  assert.match(app.document.querySelector(".folder-name").textContent, /Finance/);
});

test("opening a folder loads only that label's emails", async () => {
  const app = makeApp({ routes: {
    "GET /api/archive/folders": { folders: [folder("L1", "Finance", 2, 1)] },
    "GET /api/archive/emails": { label_id: "L1", emails: [cand("a1"), cand("a2")], next_page_token: null },
  } });
  await app.window.loadFolders();
  click(app.window, app.document.querySelector("#folder-grid .folder-card"));
  await waitFor(() => rows(app.document, "#arch-email-list").length === 2);
  assert.equal(app.callsTo("GET", "/api/archive/emails").length, 1);
  assert.ok(!$(app.document, "archive-folder").classList.contains("hidden"));
});

// --- Archive + restore + read-state -----------------------------------------
function selectRow(window, row) {
  const cb = row.querySelector(".row-cb");
  cb.checked = true;
  cb.dispatchEvent(new window.Event("change", { bubbles: true }));
}

test("the Archive button is gone; More menu exposes the secondary actions", async () => {
  const app = makeApp({ routes: { "GET /api/emails": { emails: [cand("m1")] } } });
  await app.window.loadEmails();
  assert.equal(app.document.getElementById("ab-archive"), null); // ambiguous button removed
  selectRow(app.window, app.document.querySelector('#email-list .email-item[data-id="m1"]'));
  // Primary actions stay visible; the menu is closed until opened.
  assert.ok($(app.document, "ab-generate") && $(app.document, "ab-smart"));
  assert.ok($(app.document, "ab-menu").classList.contains("hidden"));
  click(app.window, $(app.document, "ab-more"));
  assert.ok(!$(app.document, "ab-menu").classList.contains("hidden"));
  for (const id of ["ab-move", "ab-label", "ab-read", "ab-unread", "ab-clear-menu"]) {
    assert.ok($(app.document, id), id + " should be in the menu");
  }
});

test("Move to folder archives the selection and calls the label API", async () => {
  const app = makeApp({ routes: {
    "GET /api/emails": { emails: [cand("m1"), cand("m2")] },
    "GET /api/labels": { labels: [] },
    "POST /api/mailbox/label": { action: "apply_label", requested: 1, modified: 1, failed: 0, failures: [], label_id: "L_new" },
  } });
  await app.window.loadEmails();
  selectRow(app.window, app.document.querySelector('#email-list .email-item[data-id="m1"]'));
  click(app.window, $(app.document, "ab-more"));
  click(app.window, $(app.document, "ab-move"));
  await waitFor(() => !$(app.document, "label-modal").classList.contains("hidden"));
  // "Move" preset the archive flag (folder = label + remove from inbox).
  assert.equal($(app.document, "label-archive").checked, true);
  $(app.document, "label-new").value = "Finance";
  click(app.window, $(app.document, "label-apply"));
  await waitFor(() => !app.document.querySelector('#email-list .email-item[data-id="m1"]'));
  const calls = app.callsTo("POST", "/api/mailbox/label");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.label_name, "Finance");
  assert.equal(calls[0].body.archive, true);
  assert.ok(app.document.querySelector('#email-list .email-item[data-id="m2"]')); // others untouched
});

test("marking read via the More menu syncs the row's unread state", async () => {
  const app = makeApp({ routes: {
    "GET /api/emails": { emails: [cand("m1")] },
    "POST /api/mailbox/mark-read": { action: "mark_read", requested: 1, modified: 1, failed: 0, failures: [] },
  } });
  await app.window.loadEmails();
  assert.ok(app.document.querySelector('#email-list .email-item[data-id="m1"]').classList.contains("unread"));
  selectRow(app.window, app.document.querySelector('#email-list .email-item[data-id="m1"]'));
  click(app.window, $(app.document, "ab-more"));
  click(app.window, $(app.document, "ab-read"));
  await waitFor(() => {
    const r = app.document.querySelector('#email-list .email-item[data-id="m1"]');
    return r && !r.classList.contains("unread");
  });
  assert.equal(app.callsTo("POST", "/api/mailbox/mark-read").length, 1);
});

test("restore-to-inbox from a folder calls the archive restore API", async () => {
  const app = makeApp({ routes: {
    "GET /api/archive/folders": { folders: [folder("L1", "Finance", 1, 0)] },
    "GET /api/archive/emails": { label_id: "L1", emails: [cand("a1", "Invoice", { is_unread: false, label_ids: ["L1"] })], next_page_token: null },
    "POST /api/archive/restore": { action: "restore", requested: 1, modified: 1, failed: 0, failures: [] },
  } });
  await app.window.loadFolders();
  click(app.window, app.document.querySelector("#folder-grid .folder-card"));
  await waitFor(() => rows(app.document, "#arch-email-list").length === 1);
  selectRow(app.window, app.document.querySelector('#arch-email-list .email-item[data-id="a1"]'));
  click(app.window, $(app.document, "arch-restore"));
  await waitFor(() => app.callsTo("POST", "/api/archive/restore").length === 1);
  assert.deepEqual(app.callsTo("POST", "/api/archive/restore")[0].body.message_ids, ["a1"]);
});

test("archive folder: Restore is primary; secondary actions live in More", async () => {
  const app = makeApp({ routes: {
    "GET /api/archive/folders": { folders: [folder("L1", "Finance", 1, 1)] },
    "GET /api/archive/emails": { label_id: "L1", emails: [cand("a1")], next_page_token: null },
    "POST /api/mailbox/mark-read": { action: "mark_read", requested: 1, modified: 1, failed: 0, failures: [] },
  } });
  await app.window.loadFolders();
  click(app.window, app.document.querySelector("#folder-grid .folder-card"));
  await waitFor(() => rows(app.document, "#arch-email-list").length === 1);
  selectRow(app.window, app.document.querySelector('#arch-email-list .email-item[data-id="a1"]'));
  assert.ok($(app.document, "arch-restore"));                       // primary stays direct
  assert.ok($(app.document, "arch-menu").classList.contains("hidden"));
  click(app.window, $(app.document, "arch-more-btn"));
  assert.ok(!$(app.document, "arch-menu").classList.contains("hidden"));
  click(app.window, $(app.document, "arch-read"));                 // Mark read via menu
  await waitFor(() => app.callsTo("POST", "/api/mailbox/mark-read").length === 1);
});

// --- Network error handling -------------------------------------------------
test("inbox load surfaces a network error", async () => {
  const app = makeApp({ routes: { "GET /api/emails": { __status: 502, body: { detail: "Gmail unreachable" } } } });
  await app.window.loadEmails();
  assert.match($(app.document, "list-empty").textContent, /Gmail unreachable/);
  assert.equal(rows(app.document, "#email-list").length, 0);
});

test("folders error shows a Retry that recovers", async () => {
  const app = makeApp({ routes: { "GET /api/archive/folders": { __status: 500, body: { detail: "Server boom" } } } });
  await app.window.loadFolders();
  assert.match($(app.document, "folder-empty").textContent, /Server boom/);
  const retry = $(app.document, "folder-retry");
  assert.ok(retry, "a Retry button should be offered");
  // Recover: next call succeeds.
  app.setRoutes({ "GET /api/archive/folders": { folders: [folder("L1", "Finance", 3, 1)] } });
  click(app.window, retry);
  await waitFor(() => app.document.querySelectorAll("#folder-grid .folder-card:not(.skel)").length === 1);
});

test("a timed-out archive request reports a timeout (not a silent hang)", async () => {
  const app = makeApp({ routes: { "GET /api/archive/folders": { __abort: true } } });
  await app.window.loadFolders();
  assert.match($(app.document, "folder-empty").textContent, /timed out/i);
  assert.ok($(app.document, "folder-retry"), "timeout should still offer a Retry");
});
