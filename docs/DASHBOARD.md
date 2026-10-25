# Dashboard

A Streamlit dashboard on `127.0.0.1:8502`, driven by the supervised-control API on `127.0.0.1:8711`.

```powershell
pip install -e ".[dashboard]"
.\scripts\start-dashboard.ps1      # starts the control API (if not running) and the dashboard
```

The script creates a control token for the session if `DEEP_RESEARCH_CONTROL_TOKEN` is not set. It never prints the token.

| Page | What it does |
|---|---|
| Dashboard | Welcome banner, four stat tiles (total, completed, in progress, average completion time), recent researches, upcoming reminders, capabilities, quick actions, source types |
| New Research | Ask a Question / Upload Files (PDF, DOCX, PPTX, TXT, MD, images) / From URL (any public web page, read as a cited source in live mode, or a direct PDF/document/image link; every hop is SSRF-checked), Web Search toggle (= live mode), step-by-step approval. Starting a run opens the live run view with Approve / Reject, Pause / Resume / Stop |
| My Researches | Searchable, filterable list with tags and status; opens the report (Markdown, per-claim confidence, numbered citations with credibility badges, PDF / DOCX / PPTX / JSON / Markdown download) or the live run. The report page has **Ask this report** (grounded follow-up chat), **Notes & reminders** and **Share** (static HTML, revocable read-only link with an expiry in days, 0 = never; the list shows active/revoked/expired and the expiry time) tabs |
| Knowledge Base | Reports, claims and uploaded files from all runs |
| Sources | Source types and every cited source with its credibility badge, de-duplicated across reports |
| Tools & Integration | The engine's real tools with their current state, plus Provider Health |
| Settings | General (saved to `dashboard_runs/prefs.json`), Research (approval policy, engine limits), Profile & Preferences (saved topics, preferred/blocked sources and domains, default depth), Reminders (local only; the Dashboard's Reminders **Manage** link opens this tab via `?page=settings&tab=reminders`), Account (masked API keys) |

All counts come from the control API and the runs saved under `dashboard_runs/`. Audio and video analysis shows its real state (off / not installed / on) from `MDRA_MEDIA_*` and the installed `media` extra; uploads of MP3/WAV/M4A/OGG/FLAC/MP4/WEBM/MOV/MKV and YouTube links are accepted only when it is on. Chat history, profile, notes, reminders and shares are stored in `data/assistant.db` (see [ASSISTANT.md](ASSISTANT.md)).

Credibility badges (High / Medium / Unverified / Low, or Provided for your own files) come from a domain heuristic; hover for the reason. Claim confidence scores each cited statement by the number of distinct sources and their credibility, lowered for hedged wording. Times are shown in IST (`DEEP_RESEARCH_DISPLAY_TZ` to change).
## Live Research View

On a run's live screen, the left panel stacks the real sources the agents read (web pages from search hits and fetched pages, plus attached files) as overlapping cards that slide in as they arrive; the right panel is a green terminal tailing the run log: plan tasks, search queries, fetched sources, evaluator verdicts and step approvals. Everything comes from `/control/runs/{id}/logs`; the orchestrator emits these events through an optional `trace()` hook on the step gate. Offline mode has no web sources, so the card stack stays empty there.
