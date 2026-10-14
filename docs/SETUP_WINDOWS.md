# Setup on Windows (PowerShell)

Project folder used below:
`C:\Users\leela\Desktop\My Projects\multimodal-agentic-deep-research-assistant-main`
(it already has `.venv` and a `.env`).

## 1. Open the project and activate the venv

```powershell
cd "C:\Users\leela\Desktop\My Projects\multimodal-agentic-deep-research-assistant-main"
.\.venv\Scripts\Activate.ps1
# If you see "running scripts is disabled", run this once, then activate again:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -e ".[test]"
```

Re-run the `pip install` after you pull this update, so the renamed demo
command (`deep-research-demo`) is installed.

## 2. Update `.env` (old-prefix variables are no longer read)

The settings prefix is now `DEEP_RESEARCH_` (the old 5-letter acronym prefix is ignored). Rename every variable in your
existing `.env` with one command:

```powershell
(Get-Content .env) -replace '^[M]ADRA_', 'DEEP_RESEARCH_' | Set-Content .env -Encoding utf8
Select-String -Path .env -Pattern '^[M]ADRA_'   # should print nothing
```

Or copy a fresh template and fill it in: `Copy-Item .env.example .env -Force`.

Then make sure these lines are set in `.env`. Open it with `notepad .env`.

```dotenv
# Live mode: real web search + LLM answers
DEEP_RESEARCH_RESEARCH_MODE=live

# REQUIRED for LLM answers and image/scanned-PDF descriptions (free models only).
# Get a key at https://openrouter.ai/keys. Paste it after '=' with no quotes or spaces.
DEEP_RESEARCH_OPENROUTER_API_KEY=sk-or-v1-...

# Leave these on auto: the app picks and tests free models itself
DEEP_RESEARCH_OPENROUTER_MODEL=auto
DEEP_RESEARCH_OPENROUTER_VISION_MODEL=auto

# OPTIONAL: Tavily web search (better results). Leave empty to use free DuckDuckGo/Wikipedia.
DEEP_RESEARCH_TAVILY_API_KEY=
DEEP_RESEARCH_SEARCH_PROVIDER=auto

# Live runs take 1-2 minutes on free models; give them room
DEEP_RESEARCH_MAX_RESEARCH_TIME_SECONDS=600
DEEP_RESEARCH_MAX_TOOL_CALL_TIME_SECONDS=240
DEEP_RESEARCH_MAX_MODEL_CALL_TIME_SECONDS=240
```

Without an OpenRouter key, live mode still runs, but the answers are just
search snippets. Never commit `.env`.

## 3. Start the server

```powershell
python -m uvicorn deep_research.api.main:app --port 8000
```

Leave this window open; the server logs appear in it. Stop it with `Ctrl+C`.
Quick check in a second window: `Invoke-RestMethod http://127.0.0.1:8000/health`

## 4. Try it in Swagger

1. Open **http://127.0.0.1:8000/docs** in the browser.
2. Expand **POST /research**, click **Try it out**, replace the body with:

```json
{
  "objective": "What are the main benefits and risks of small modular nuclear reactors?"
}
```

3. Click **Execute** and wait (about 1-2 minutes). The response has `status`
   and `report.content` (Markdown with `[n]` citations and a `## Sources`
   list), plus `report.citations` and `report.models_used`.
4. To fetch a run again later: **GET /research/{session_id}** with the
   `session_id` from the response.

### With a PDF attachment

Swagger needs the PDF as base64 text. Building the request in PowerShell is
easiest:

```powershell
$pdf  = "C:\Users\leela\Desktop\report.pdf"          # your PDF
$b64  = [Convert]::ToBase64String([IO.File]::ReadAllBytes($pdf))
$body = @{
  objective   = "Using the attached report and the web, what are the key risks?"
  attachments = @(@{ kind = "pdf"; name = "report.pdf"; data = "data:application/pdf;base64,$b64" })
} | ConvertTo-Json -Depth 5
$r = Invoke-RestMethod -Uri http://127.0.0.1:8000/research -Method Post -ContentType "application/json" -Body $body -TimeoutSec 900
$r.status
$r.report.content | Out-File -Encoding utf8 research-report.md
$r | ConvertTo-Json -Depth 20 | Out-File -Encoding utf8 research-run.json
notepad research-report.md
```

The JSON shape is the same in Swagger:
`{"objective": "...", "attachments": [{"kind": "pdf", "name": "report.pdf", "data": "data:application/pdf;base64,JVBERi0..."}]}`.

- Images work the same way: `kind = "image"` and `data:image/png;base64,...`.
- You can attach up to 5 files.
- PDF facts are cited as `[n] report.pdf (attached pdf, p.N) - user attachment`.
- A scanned PDF has no text layer. It still completes: the report says
  "no extractable text", and a free vision model describes the page image.

## 5. Where things live

| What | Where |
|---|---|
| Run results | In memory on the server: the POST response and `GET /research/{session_id}`. They are lost when the server stops, so save them as shown above (`research-run.json`, `research-report.md`) |
| Free-model cache (chosen models + ranking + timestamp, **no API key**) | `%LOCALAPPDATA%\deep_research\openrouter_free_models.json`, i.e. `C:\Users\leela\AppData\Local\deep_research\` (refreshed automatically after 6h) |
| Change or disable the cache | `DEEP_RESEARCH_OPENROUTER_MODEL_CACHE_PATH=C:\path\file.json` or `=off` |
| Logs | The terminal running uvicorn |

To force fresh model discovery, delete the cache file:
`Remove-Item "$env:LOCALAPPDATA\deep_research\openrouter_free_models.json"`.

## 6. Run the checks (optional)

```powershell
python -m pytest -q
ruff check src tests
mypy src
```
