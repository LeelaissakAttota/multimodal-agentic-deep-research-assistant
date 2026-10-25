import {pageUrl} from "./api.js";
import {loadSettings} from "./storage.js";

const $ = (id) => document.getElementById(id);
let page = null;

async function currentPage() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  let selection = "";
  try {
    const [res] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => String(getSelection() || "")});
    selection = (res && res.result) || "";
  } catch {
    selection = "";  // chrome:// pages and the Web Store cannot be scripted
  }
  return {url: tab.url, title: tab.title || "", selection};
}

async function go(action) {
  $("status").textContent = "Sending…";
  const res = await chrome.runtime.sendMessage({type: "mdra-send", action, page});
  $("status").textContent = res && res.text ? res.text : "No answer from the extension.";
  $("status").className = res && res.ok ? "ok" : "err";
}

async function init() {
  $("options").addEventListener("click", (ev) => { ev.preventDefault(); chrome.runtime.openOptionsPage(); });
  const settings = await loadSettings();
  if (!settings.token) {
    $("page").textContent = "Set the MDRA control token in the extension options first.";
    return;
  }
  page = await currentPage();
  try {
    pageUrl(page.url);
  } catch (err) {
    $("page").textContent = err.message;
    return;
  }
  $("page").textContent = page.title || page.url;
  if (page.selection) {
    $("selection").hidden = false;
    $("selection").textContent = page.selection.slice(0, 300);
  }
  $("research").disabled = false;
  $("source").disabled = false;
  $("research").addEventListener("click", () => go("research"));
  $("source").addEventListener("click", () => go("source"));
}

init();
