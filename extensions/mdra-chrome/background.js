// Service worker: context-menu entries + the action used by the popup. Never logs settings or the token.
import {buildRequest, describeResult, send} from "./api.js";
import {loadSettings} from "./storage.js";

const MENUS = [
  {id: "mdra-research-page", title: "Research this page with MDRA", contexts: ["page", "link"]},
  {id: "mdra-research-selection", title: "Research selected text with MDRA", contexts: ["selection"]},
  {id: "mdra-source-page", title: "Add page to MDRA as a source", contexts: ["page", "selection"]},
];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => MENUS.forEach((m) => chrome.contextMenus.create(m)));
});

async function dispatch(action, page) {
  const settings = await loadSettings();
  const data = await send(settings, buildRequest(action, page, settings.mode));
  return describeResult(action, data);
}

function notify(tabId, text) {
  chrome.action.setBadgeText({tabId, text});
  setTimeout(() => chrome.action.setBadgeText({tabId, text: ""}), 4000);
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const action = info.menuItemId === "mdra-source-page" ? "source" : "research";
  const page = {
    url: info.menuItemId === "mdra-research-page" && info.linkUrl ? info.linkUrl : (tab && tab.url) || info.pageUrl,
    title: (tab && tab.title) || "",
    selection: info.selectionText || "",
  };
  try {
    await dispatch(action, page);
    notify(tab && tab.id, "OK");
  } catch {
    notify(tab && tab.id, "ERR");
  }
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !message || message.type !== "mdra-send") return false;
  dispatch(message.action, message.page).then(
    (text) => reply({ok: true, text}),
    (err) => reply({ok: false, text: String(err && err.message ? err.message : err)}),
  );
  return true;
});
