# Supervised-control API

A small local API that lets the ToolBridge dashboard supervise runs of this project.

Start it (binds to **127.0.0.1 only**):

```bash
export DEEP_RESEARCH_CONTROL_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
python -m deep_research.control.server --port 8711
```

Every endpoint except `GET /control/health` requires `Authorization: Bearer <token>`
(token >= 16 chars, constant-time compared). `X-Operator` names the human in the audit log.

| Endpoint | Purpose |
|---|---|
| `GET/PUT /control/settings` | `approval_mode`, `auto_approve_low_risk` (persisted, both default **off**) |
| `POST /control/runs` | start a run (`{"params": {...}}`) |
| `GET /control/runs[/{id}]`, `/steps` | status, steps, pending approvals |
| `POST /control/runs/{id}/pause|resume|stop` | lifecycle |
| `POST .../steps/{sid}/approve` | approve; optional `inputs` override the step inputs |
| `POST .../steps/{sid}/reject` | reject (step is skipped/failed, never executed) |
| `POST .../steps/{sid}/retry` | re-run a finished step, optionally with new `inputs` |
| `GET /control/runs/{id}/logs?after=N` / `logs/stream` | polling / SSE log stream |
| `GET /control/approvals`, `/control/audit` | approval feed and audit log |
| `GET /control/keys`, `PUT /control/keys/{NAME}` | masked view (last 4) / write-only update |

## Gates

With `approval_mode` on, the engine calls the gate **before** each step executes and the
worker thread blocks until a human approves, rejects, or stops the run. With
`auto_approve_low_risk` also on, steps classified LOW (data fetching, analysis, formatting,
report rendering) are approved automatically and recorded as `auto`; HIGH steps (messages,
writes/deletes, spending, publishing, key changes, and **anything unknown**) always wait.
With approval mode off, behaviour is unchanged.

## Keys

Stored in `~/.config/deep-research/secrets.json` (dir 0700, file 0600, atomic writes), outside
the repo. Values are never returned by the API (masked to `****` + last 4), the update
endpoint is write-only, and key changes are audited by name only. The file is re-read on
each run, so new keys apply without a restart.
