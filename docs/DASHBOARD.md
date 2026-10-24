# Dashboard

A Streamlit dashboard on `127.0.0.1:8502`, driven by the supervised-control API on `127.0.0.1:8711`.

```powershell
pip install -e ".[dashboard]"
.\scripts\start-dashboard.ps1      # starts the control API (if not running) and the dashboard
```

The script creates a control token for the session if `DEEP_RESEARCH_CONTROL_TOKEN` is not set. It never prints the token.

| Page | What it does |
|---|---|
| Dashboard | Welcome banner, four stat tiles (total, completed, in progress, average completion time), recent researches, capabilities, quick actions, source types |
| New Research | Ask a Question / Upload Files / From URL (direct PDF or image link), Web Search toggle (= live mode), step-by-step approval. Starting a run opens the live run view with Approve / Reject, Pause / Resume / Stop |
| My Researches | Searchable, filterable list with tags and status; opens the report (Markdown, claims, numbered citations, download) or the live run |
| Knowledge Base | Reports, claims and uploaded files from all runs |
| Sources | Source types and every cited source, de-duplicated across reports |
| Tools & Integration | The engine's real tools with their current state, plus Provider Health |
| Settings | General (saved to `dashboard_runs/prefs.json`), Research (approval policy, engine limits), Notifications (coming soon), Account (masked API keys) |

All counts come from the control API and the runs saved under `dashboard_runs/`. Features the engine does not support yet (video, audio, notifications, reading web pages by URL) are shown as "coming soon". Times are shown in IST (`DEEP_RESEARCH_DISPLAY_TZ` to change).