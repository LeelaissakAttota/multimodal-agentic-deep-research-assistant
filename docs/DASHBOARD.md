# Dashboard

A Streamlit dashboard on `127.0.0.1:8502`, driven by the supervised-control API on `127.0.0.1:8711`.

```powershell
pip install -e ".[dashboard]"
.\scripts\start-dashboard.ps1      # starts the control API (if not running) and the dashboard
```

The script creates a control token for the session if `DEEP_RESEARCH_CONTROL_TOKEN` is not set. It never prints the token.

| Page | What it does |
|---|---|
| New Research | Question, optional PDF and image, offline or live mode, step-by-step approval toggle |
| Live Run | Stage-by-stage pipeline (Plan, Research, Analyze, Evaluate, Report) with Approve / Reject, Pause / Resume / Stop |
| Report Viewer | The Markdown report, numbered citations (web pages and PDF pages), claims, Markdown download |
| All Runs | Every run, saved to `dashboard_runs/` (git-ignored) so the list survives restarts |
| Provider Health | Which providers are configured (key values never shown) |
| Settings | Approval policy, provider settings, write-only API key updates (masked, last 4 only) |
