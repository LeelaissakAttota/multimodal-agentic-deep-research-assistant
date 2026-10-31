// Settings live in chrome.storage.local only (not the synced storage area), so the token stays on this computer.
import {DEFAULT_BASE_URL} from "./api.js";

export async function loadSettings() {
  const s = await chrome.storage.local.get({baseUrl: DEFAULT_BASE_URL, token: "", mode: "live"});
  return {baseUrl: s.baseUrl, token: s.token, mode: s.mode};
}

export async function saveSettings(values) {
  await chrome.storage.local.set(values);
}
