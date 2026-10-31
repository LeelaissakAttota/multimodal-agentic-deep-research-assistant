# MDRA Chrome extension (Manifest V3)

Send the current page, a link, or selected text to your local MDRA control API:

* **Start new research** – creates a run (`POST /control/runs`) with the page as a `url` attachment; selected text becomes the question.
* **Add as source** – stores the page (+ selection) in MDRA's clip inbox (`POST /control/clips`), shown on **Sources → Clipped from browser**.

Also available from the right-click menu.

## Install (Windows, macOS, Linux)

1. Start the control API (`scripts/start-dashboard.ps1` on Windows, or `python -m deep_research.control.server`). It listens on `http://127.0.0.1:8711` only.
2. Open `chrome://extensions`, switch on **Developer mode**, click **Load unpacked**, pick this `extensions/mdra-chrome` folder.
3. Open the extension's **Options**, keep the address `http://127.0.0.1:8711`, paste your `DEEP_RESEARCH_CONTROL_TOKEN`, **Save**, then **Test connection**.

## Security

* Host permissions are limited to `http://127.0.0.1/*` and `http://localhost/*`; the options page rejects any other address.
* The token is kept in `chrome.storage.local` (this browser profile, never synced), only sent in the `Authorization` header, never logged, and never shown again after saving.
* The control API also refuses clips from non-loopback clients, and every call needs the bearer token.
* Page URLs are sent without credentials or `#fragment`; only http(s) pages can be sent.

Tests: `node --test tests/extension/` (run automatically by pytest when `node` is installed).
