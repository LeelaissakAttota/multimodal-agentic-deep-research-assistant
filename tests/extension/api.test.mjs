// node --test tests/extension/  (no browser needed: api.js has no chrome.* calls)
import assert from "node:assert/strict";
import test from "node:test";

import {buildRequest, describeResult, normalizeBaseUrl, pageUrl, send, validateToken} from "../../extensions/mdra-chrome/api.js";

const TOKEN = "tok-0123456789abcdef-SECRET";

test("only local control API addresses", () => {
  assert.equal(normalizeBaseUrl(""), "http://127.0.0.1:8711");
  assert.equal(normalizeBaseUrl("http://localhost:9000/"), "http://localhost:9000");
  for (const bad of ["https://127.0.0.1:8711", "http://example.com:8711", "http://127.0.0.1.evil.com", "http://u:p@127.0.0.1:8711",
                     "http://127.0.0.1:8711/x", "nonsense"]) {
    assert.throws(() => normalizeBaseUrl(bad), bad);
  }
});

test("token and page validation", () => {
  assert.throws(() => validateToken("short"));
  assert.equal(validateToken(`  ${TOKEN} `), TOKEN);
  assert.equal(pageUrl("https://user:pw@example.org/a?q=1#frag"), "https://example.org/a?q=1");
  assert.throws(() => pageUrl("chrome://settings"));
  assert.throws(() => pageUrl("file:///C:/secret.txt"));
});

test("research and source requests", () => {
  const page = {url: "https://example.org/tea#x", title: "  Green   tea ", selection: ""};
  const r = buildRequest("research", page, "live");
  assert.equal(r.path, "/control/runs");
  assert.equal(r.body.params.mode, "live");
  assert.deepEqual(r.body.params.attachments, [{kind: "url", data: "https://example.org/tea", name: "Green tea"}]);
  assert.match(r.body.params.objective, /Green tea/);
  const sel = buildRequest("research", {...page, selection: "Tea has catechins."}, "offline");
  assert.match(sel.body.params.objective, /Tea has catechins/);
  assert.equal(sel.body.params.mode, "offline");
  const s = buildRequest("source", {...page, selection: "quote"});
  assert.deepEqual(s, {path: "/control/clips", body: {url: "https://example.org/tea", title: "Green tea", selection: "quote"}});
  assert.throws(() => buildRequest("delete", page));
});

test("send uses bearer header and never leaks the token in errors", async () => {
  const calls = [];
  const ok = async (url, init) => { calls.push([url, init]); return {ok: true, status: 201, json: async () => ({run_id: "abcdef123456"})}; };
  const data = await send({baseUrl: "http://127.0.0.1:8711", token: TOKEN}, buildRequest("research", {url: "https://a.org"}), ok);
  assert.equal(calls[0][0], "http://127.0.0.1:8711/control/runs");
  assert.equal(calls[0][1].headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[0][1].credentials, "omit");
  assert.equal(describeResult("research", data), "Research started (run abcdef12). Open the MDRA dashboard to follow it.");
  const echo = async () => ({ok: false, status: 422, json: async () => ({detail: `bad ${TOKEN}`})});
  await assert.rejects(send({baseUrl: "", token: TOKEN}, buildRequest("source", {url: "https://a.org"}), echo),
    (err) => !err.message.includes(TOKEN) && err.message.includes("[token]"));
  const denied = async () => ({ok: false, status: 401, json: async () => ({})});
  await assert.rejects(send({baseUrl: "", token: TOKEN}, buildRequest("source", {url: "https://a.org"}), denied), /rejected the token/);
  const down = async () => { throw new TypeError("fetch failed"); };
  await assert.rejects(send({baseUrl: "", token: TOKEN}, buildRequest("source", {url: "https://a.org"}), down), /not reachable/);
  await assert.rejects(send({baseUrl: "http://evil.com", token: TOKEN}, buildRequest("source", {url: "https://a.org"}), ok), /local/);
});
