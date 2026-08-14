API Examples
============

This document provides minimal, copy-paste examples for the product API implemented in `src/deep_research/api`.

1) Minimal POST /research request (file: `examples/sample_request.json`)

```json
{
  "objective": "Assess reliability considerations for grid-scale battery storage",
  "metadata": { "scenario": "demo", "mode": "offline_deterministic" }
}
```

Expected behavior:
- `POST /research` returns HTTP 200 with a `ResearchRunResponse` containing `status: "completed"` for the deterministic demo.

2) Minimal GET /research/{session_id}

After receiving the `session_id` from the POST response, retrieve the terminal result:

```http
GET /research/{session_id} HTTP/1.1
Host: 127.0.0.1:8000

```

Expected behavior:
- `GET /research/{session_id}` returns HTTP 200 and the same `ResearchRunResponse` JSON as returned by the POST.

3) Validation & error cases

- Empty objective (POST with "objective": "   ") → HTTP 422 (validation error)
- Unknown session id (GET /research/<non-existent-uuid>) → HTTP 404 with detail "Research session not found"

Notes:
- Do not provide secrets in `metadata`. The API rejects secret-like metadata fields.
- For offline demos, the implementation is deterministic and makes no external network calls.
