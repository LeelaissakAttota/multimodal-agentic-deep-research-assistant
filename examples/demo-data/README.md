# MDRA (Deep Research) demo data - fictional "ZetaCell"

ZetaCell is an invented battery company. Every name and number here is made up for demos.

| Path | What it is |
|---|---|
| `data/field_note.pdf` | 1-page text PDF: ZetaCell Z2 pilot results (efficiency 88%, 94% capacity after 500 cycles) |
| `data/chart.png` | Capacity-retention line chart (100% -> 94% over 500 cycles) |
| `research_questions.csv` | Questions Q01-Q06 and which attachment they use |
| `requests/R01-R08_*.json` | Research API (port 8000): health/ready/version + research runs (text, PDF, image, both) |
| `requests/C01-C07_*.json` | Control API (port 8711): health, settings, start run, runs, approvals, audit, masked keys |
| `requests/E01-E09_*.json` | Errors: 422 (validation, value never echoed back), 404 (unknown session / run), 401 (no / wrong token) |
| `send.ps1` | Send one request file and pretty-print the response |
| `run-all.ps1` | Run all requests and print a PASS/FAIL table |

Requests R06-R08 already contain the PDF / PNG as base64 data URLs, so no upload step is needed.

## Start (PowerShell, repo root) - use the SAME terminal settings for both servers and the scripts

Terminal 1 - research API:

```powershell
$env:PYTHONPATH = "src"; $env:DEEP_RESEARCH_LOG_FORMAT = "text"
$env:DEEP_RESEARCH_RESEARCH_MODE = "offline"      # or "live" (needs internet + free OpenRouter key)
.\.venv\Scripts\python.exe -m uvicorn deep_research.api.main:app --host 127.0.0.1 --port 8000
```

Terminal 2 - control API (choose your own token, at least 16 characters, and set the same value in terminal 3):

```powershell
$env:PYTHONPATH = "src"; $env:DEEP_RESEARCH_LOG_FORMAT = "text"
$env:DEEP_RESEARCH_CONTROL_TOKEN = "<your-local-demo-token-16+chars>"
.\.venv\Scripts\python.exe -m deep_research.control.server --port 8711
```

Terminal 3 - requests:

```powershell
$env:DEEP_RESEARCH_CONTROL_TOKEN = "<same token as terminal 2>"
cd examples\demo-data
.\run-all.ps1               # all 24
.\run-all.ps1 -Filter "R*"  # research API only
.\send.ps1 requests\R06_research_pdf.json
```

## Logs

`DEEP_RESEARCH_LOG_FORMAT=text` -> one readable line per event (`research_submitted`, `research_finished`,
`research_session_not_found`, `request_validation_failed` with field + reason, `http_error`, `unhandled_exception`
with `exception_where`). JSON is the default. OpenRouter / NVIDIA / HF / Tavily keys, Bearer tokens and `key=` values
show as `[REDACTED]`, also in control-API run logs.
