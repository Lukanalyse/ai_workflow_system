// Frontend test harness: loads the real index.html + app.js into jsdom with a
// stubbed, routable fetch so we can drive the actual UI code through its
// critical flows. The app's auto-boot is skipped via window.__APP_TEST__.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const here = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.resolve(here, "..");
const rawHtml = readFileSync(path.join(staticDir, "index.html"), "utf8");
const appJs = readFileSync(path.join(staticDir, "app.js"), "utf8");
// Strip the external <script src=app.js>; we inject the file inline so jsdom
// runs it (no resource loader / network in tests).
const html = rawHtml.replace(/<script src="\/static\/app\.js[^"]*"><\/script>/, "");

function abortError() {
  const e = new Error("The operation was aborted.");
  e.name = "AbortError";
  return e;
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "status " + status,
    json: async () => body,
  };
}

// routes: { "GET /api/emails": value | fn, "/api/x": value, ... }
// A route value may be: a plain object (200 JSON), an Error (thrown),
// { __status, body } (custom status), { __abort: true } (AbortError), or a
// function returning any of those (optionally async).
function makeFetch(routes, calls) {
  return async (input, opts = {}) => {
    const url = typeof input === "string" ? input : String(input);
    const method = (opts.method || "GET").toUpperCase();
    const pathOnly = url.split("?")[0];
    let body = null;
    if (opts.body) {
      try { body = JSON.parse(opts.body); } catch { body = opts.body; }
    }
    calls.push({ method, path: pathOnly, url, body });
    if (opts.signal && opts.signal.aborted) throw abortError();

    const route = routes[`${method} ${pathOnly}`] ?? routes[pathOnly];
    let result = typeof route === "function" ? route({ url, method, body, opts }) : route;
    if (result && typeof result.then === "function") result = await result;

    if (result instanceof Error) throw result;
    if (result && result.__abort) throw abortError();
    if (result && typeof result.__status === "number") {
      return jsonResponse(result.body ?? {}, result.__status);
    }
    return jsonResponse(result ?? {}, 200);
  };
}

export function makeApp({ routes = {} } = {}) {
  const calls = [];
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    url: "http://localhost/",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  window.__APP_TEST__ = true;
  window.fetch = makeFetch(routes, calls);
  window.EventSource = class {
    constructor() {}
    close() {}
    addEventListener() {}
  };
  // Inject app.js as an inline classic script so its top-level function
  // declarations become callable window globals.
  const script = window.document.createElement("script");
  script.textContent = appJs;
  window.document.body.appendChild(script);

  return {
    dom,
    window,
    document: window.document,
    calls,
    setRoutes(extra) { Object.assign(routes, extra); },
    callsTo(method, pathOnly) {
      return calls.filter((c) => c.method === method && c.path === pathOnly);
    },
  };
}

// --- test data + interaction helpers ----------------------------------------
export function cand(id, subject = "Subject " + id, over = {}) {
  return {
    id, thread_id: "t" + id, subject,
    sender_email: id + "@x.com", sender_name: id.toUpperCase(),
    received_at: "2026-06-01T00:00:00+00:00", snippet: "snippet of " + id,
    is_unread: true, label_ids: ["UNREAD"], has_attachments: false, attachments: [],
    replyable: true, reply_reason: "", already_processed: false, score: 5,
    classification: "", reasons: [], ai: null, ...over,
  };
}

export function folder(id, name, total = 5, unread = 2) {
  return { id, name, total, unread, read: total - unread };
}

export function click(window, el) {
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
}

export const delay = (ms) => new Promise((r) => setTimeout(r, ms));

export async function waitFor(pred, { timeout = 1500, interval = 10 } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (pred()) return true;
    await delay(interval);
  }
  throw new Error("waitFor: condition not met within " + timeout + "ms");
}
