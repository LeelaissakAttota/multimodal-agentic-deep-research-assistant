Screenshot assets
=================

This folder contains demo artifacts captured for resume/demo packaging.

- `demo-output.json`: UTF-8 JSON containing a sample run of `madra-demo` (deterministic offline output).
- `swagger-ui.html`: HTML snapshot of the Swagger UI that loads `/openapi.json` when the server is running.

How to reproduce PNG screenshots locally
--------------------------------------
1. Start the server:

```powershell
uvicorn deep_research.api.main:app --host 127.0.0.1 --port 8000
```

2. Open `http://127.0.0.1:8000/docs` in a browser and take a screenshot of the visible UI.

Alternatively, use Playwright or a browser automation tool to capture a PNG programmatically and save it under this folder as `swagger-ui.png`.
