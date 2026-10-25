import {normalizeBaseUrl, validateToken} from "./api.js";
import {loadSettings, saveSettings} from "./storage.js";

const $ = (id) => document.getElementById(id);
const say = (text, ok) => { $("status").textContent = text; $("status").className = ok ? "ok" : "err"; };

async function init() {
  const s = await loadSettings();
  $("baseUrl").value = s.baseUrl;
  $("mode").value = s.mode;
  $("token").placeholder = s.token ? "saved (hidden) – type to replace" : "DEEP_RESEARCH_CONTROL_TOKEN";
}

$("save").addEventListener("click", async () => {
  try {
    const values = {baseUrl: normalizeBaseUrl($("baseUrl").value), mode: $("mode").value === "offline" ? "offline" : "live"};
    if ($("token").value) values.token = validateToken($("token").value);
    await saveSettings(values);
    $("token").value = "";
    await init();
    say("Saved.", true);
  } catch (err) {
    say(err.message, false);
  }
});

$("forget").addEventListener("click", async () => {
  await saveSettings({token: ""});
  await init();
  say("Token removed from this browser.", true);
});

$("test").addEventListener("click", async () => {
  try {
    const s = await loadSettings();
    const base = normalizeBaseUrl(s.baseUrl);
    const token = validateToken(s.token);
    const r = await fetch(`${base}/control/settings`, {headers: {Authorization: `Bearer ${token}`}, credentials: "omit", cache: "no-store"});
    say(r.ok ? "Connected to MDRA." : r.status === 401 ? "MDRA rejected the token." : `MDRA answered HTTP ${r.status}.`, r.ok);
  } catch (err) {
    say(err.message.startsWith("The control token") ? err.message : "MDRA is not reachable. Is the control API running?", false);
  }
});

init();
