DEMO Playbook
==============

1. 30-second intro
-------------------
Multimodal Agentic Deep Research Assistant is a provider-neutral Python engine that coordinates bounded research workflows: planning, tool execution, evidence extraction, evaluation, and citation-aware reporting. The release includes a deterministic offline demo and a FastAPI product boundary for resumable, traceable research sessions.

2. Problem solved
------------------
Hiring managers often want to see an AI system that performs structured, auditable research steps with reproducible outputs and clear evidence provenance without performing live web calls during interviews. This project demonstrates that capability deterministically and safely.

3. High-level architecture
-------------------------
- Orchestrator: coordinates planning → researching → analyzing → evaluating → reporting.
- Agents: Planning, Research (deterministic tools), Analysis, Evaluation, Report.
- Persistence: transactional SQLite-backed `ResearchSessionRepository` (process-local by default).
- Product: FastAPI endpoints `POST /research` and `GET /research/{session_id}` for submission and retrieval.

4. Important Agentic AI components
---------------------------------
- `MasterResearchOrchestrator` — enforces the state graph and iteration bounds.
- Deterministic agents — perform offline, fake tool work for reproducible demos.
- `ExecutionHarness` — applies runtime limits, retries, and sanitized failure handling.

5. Evidence / citation workflow
-------------------------------
Agents produce `Claim`, `Evidence`, and `Source` objects. Evidence and sources are referenced by stable identifiers in the final `report` so reviewers can trace claims back to sources stored in the session snapshot.

6. Multimodal capability (what is actually implemented)
-----------------------------------------------------
- The repo contains a modality taxonomy and deterministic/tested support for `WEB` and `DOCUMENT` tool paths via offline fakes. Other modalities (PDF, IMAGE, VIDEO, AUDIO, etc.) are infrastructure-compatible but not live-verified. The demo is explicitly zero-network and synthetic.

7. Live demo prerequisites
-------------------------
- Python 3.12
- Install project and test extras: `python -m pip install -e ".[test]"`
- No API keys or network credentials are required for the demo.

8. Exact demo sequence (commands)
--------------------------------
Run these steps locally in a terminal:

```powershell
git clone https://github.com/LeelaissakAttota/multimodal-agentic-deep-research-assistant.git
cd multimodal-agentic-deep-research-assistant
python -m pip install -e ".[test]"
madra-demo
```

Or run the FastAPI server and exercise the API:

```powershell
uvicorn deep_research.api.main:app --host 127.0.0.1 --port 8000
# In another terminal (use examples/sample_request.json):
curl -X POST "http://127.0.0.1:8000/research" -H "Content-Type: application/json" -d @examples/sample_request.json
```

9. POST /research demonstration
-------------------------------
- Submit the sample request. Expect HTTP 200 and a JSON `ResearchRunResponse` with `status: "completed"` in the deterministic demo. The response contains `session_id` and `request_id`.

10. Result explanation
----------------------
- The returned `report` includes `evidence_gathered` (IDs), `sources_consulted` (IDs), and a short `summary`. `runtime.usage` reports iteration and call counts.

11. GET /research/{session_id} demonstration
------------------------------------------
- Use the `session_id` returned from the POST to GET the terminal result. Expect HTTP 200 and the same `ResearchRunResponse` JSON.

12. Validation / error-handling demo
----------------------------------
- Try POST with an empty objective (e.g., `{ "objective": "   " }`) → API returns HTTP 422.
- Try GET with a non-existent UUID → API returns HTTP 404 and detail "Research session not found".

13. Key technical talking points
-------------------------------
- Deterministic offline fakes enable a reproducible, $0-cost demo while preserving real orchestration and persistence code paths.
- Explicit state graph and bounded loops (iterations, retries, token/time budgets) reduce runaway model/tool behavior.
- Evidence-first design: claims are always linked to traceable evidence/source ids.

14. Engineering/design decisions worth discussing
-------------------------------------------------
- Provider-neutral `ModelGateway` and `ExecutionHarness` to separate reliability concerns from provider SDKs.
- Immutable evidence history and transactional SQLite snapshots for auditability.
- Validation at API boundary to reject secret-like metadata and null/oversized inputs.

15. Known limitations
---------------------
- Demo is synthetic and zero-network; no live provider SDK or paid-model calls are included.
- Some modalities are infrastructure-ready but not live-verified (PDF/image/audio/video).
- API lookup is process-local and capped by `max_sessions`.

16. Suggested 2-minute demo version
---------------------------------
1. Run `madra-demo` and show printed JSON output (show `status: completed` and `report` evidence ids).
2. Start `uvicorn` and open `http://127.0.0.1:8000/docs` to show the API spec and a sample POST.

17. Suggested 5-minute interview demo version
------------------------------------------
1. Briefly explain architecture (30s).
2. Run `madra-demo` and point to evidence ids in output (90s).
3. Start server and perform `POST /research` and `GET /research/{session_id}` showing request→response flow and error cases (2 min).

End of playbook.
