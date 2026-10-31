// Shared, side-effect free helpers (imported by the service worker, popup, options page and node tests).
// The control token is only ever placed in the Authorization header. It is never logged or shown.

export const DEFAULT_BASE_URL = "http://127.0.0.1:8711";
export const MIN_TOKEN_LENGTH = 16;

/** Only http://127.0.0.1:<port> or http://localhost:<port> (the control API never listens elsewhere). */
export function normalizeBaseUrl(value) {
  let url;
  try {
    url = new URL(String(value || "").trim() || DEFAULT_BASE_URL);
  } catch {
    throw new Error("Enter a URL like http://127.0.0.1:8711");
  }
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("Only a local MDRA control API (127.0.0.1 or localhost) is allowed");
  }
  if (url.username || url.password || (url.pathname !== "/" && url.pathname !== "") || url.search || url.hash) {
    throw new Error("Use just scheme, host and port, e.g. http://127.0.0.1:8711");
  }
  return `${url.protocol}//${url.host}`;
}

export function validateToken(token) {
  const t = String(token || "").trim();
  if (t.length < MIN_TOKEN_LENGTH) throw new Error(`The control token must be at least ${MIN_TOKEN_LENGTH} characters`);
  return t;
}

/** Pages the extension can send: http(s) only, credentials and #fragment removed. */
export function pageUrl(value) {
  let url;
  try {
    url = new URL(String(value || ""));
  } catch {
    throw new Error("This page has no web address that MDRA can read");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("Only http(s) pages can be sent");
  url.username = "";
  url.password = "";
  url.hash = "";
  return url.toString();
}

/** Build the request for "research" (new run) or "source" (clip). Returns {path, body}. */
export function buildRequest(action, page, mode = "live") {
  const url = pageUrl(page.url);
  const title = String(page.title || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const selection = String(page.selection || "").trim();
  if (action === "source") {
    return {path: "/control/clips", body: {url, title, selection: selection.slice(0, 20000)}};
  }
  if (action !== "research") throw new Error(`unknown action ${action}`);
  const topic = selection ? selection.slice(0, 1500) : `${title || url}`;
  const objective = selection
    ? `Research and fact-check this passage from ${title || url}: "${topic}"`
    : `Research the topic of this page: ${topic}`;
  return {
    path: "/control/runs",
    body: {params: {objective: objective.slice(0, 2000), mode: mode === "offline" ? "offline" : "live",
                    attachments: [{kind: "url", data: url, name: title.slice(0, 200) || undefined}]}},
  };
}

/** POST to the local control API. `fetchImpl` is injectable for tests. Errors never include the token. */
export async function send(settings, request, fetchImpl = fetch) {
  const base = normalizeBaseUrl(settings.baseUrl);
  const token = validateToken(settings.token);
  let response;
  try {
    response = await fetchImpl(base + request.path, {
      method: "POST",
      headers: {"Content-Type": "application/json", "Authorization": `Bearer ${token}`, "X-Operator": "chrome-extension"},
      body: JSON.stringify(request.body),
      credentials: "omit",
      cache: "no-store",
    });
  } catch {
    throw new Error(`MDRA is not reachable at ${base}. Is the control API running?`);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.status === 401) throw new Error("MDRA rejected the token. Check it in the extension options.");
  if (!response.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`;
    throw new Error(`MDRA error: ${detail}`.replaceAll(token, "[token]"));
  }
  return data || {};
}

export function describeResult(action, data) {
  if (action === "source") return "Saved as a source in MDRA (Sources → Clipped from browser).";
  const id = data && data.run_id ? String(data.run_id).slice(0, 8) : "";
  return id ? `Research started (run ${id}). Open the MDRA dashboard to follow it.` : "Research started.";
}
