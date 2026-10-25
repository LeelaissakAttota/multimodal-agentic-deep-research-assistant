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
| `GET /control/runs/{id}/export?format=html` | self-contained static HTML (also `pdf`, `docx`, `pptx`, `json`, `md`) |
| `POST /control/runs/{id}/shares` | create a read-only share link (`{"report": {...}}` optional, for runs only saved by the dashboard); returns `path` with the token **once** |
| `GET /control/shares[?run_id=]` | list shares (id, run, created, active; never tokens) |
| `POST /control/shares/{share_id}/revoke` | revoke a share (kept as revoked; the link stops working) |
| `GET /share/{share_id}?token=...` | **no bearer token**: the share token alone gives read-only access to that one HTML snapshot |

Share links: the token (32 random bytes) is returned once; only its SHA-256 hash is stored in
`data/assistant.db` and it is compared with `hmac.compare_digest`. Unknown, wrong or revoked tokens all
return the same 404. The page is served with `Content-Security-Policy: default-src 'none'`, `Cache-Control:
no-store`, `X-Robots-Tag: noindex` and `Referrer-Policy: no-referrer`. Share routes exist only when the
server is started with the assistant store (`deep-research-control` does this). The server runs with
access logging off, so URLs carrying tokens are not logged; share create/revoke events are audited
without the token.

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
