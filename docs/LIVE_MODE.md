# Live research mode, PDF and image inputs

Detailed reference for the optional live mode. The [README](../README.md) has the short version.

By default the assistant runs fully offline with the deterministic agent
(`DEEP_RESEARCH_RESEARCH_MODE=offline`), and all tests stay offline and free.

To use real web research:

```bash
export DEEP_RESEARCH_RESEARCH_MODE=live
# optional: LLM synthesis with citations via free OpenRouter models
export DEEP_RESEARCH_OPENROUTER_API_KEY=...
# default: automatic free-model selection (no model env needed)
# export DEEP_RESEARCH_OPENROUTER_MODEL=auto
# export DEEP_RESEARCH_OPENROUTER_VISION_MODEL=auto
# optional pin (tried first, then auto-fallback): any slug ending in ":free"
# export DEEP_RESEARCH_OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free
# optional: Tavily is tried first in the automatic search chain when keyed
# export DEEP_RESEARCH_TAVILY_API_KEY=...
uvicorn deep_research.api.main:app
```

Live mode searches the web, fetches the top pages (public http(s) only, with
private-network/SSRF protection and size limits), and asks the
free-model-guarded LLM to answer with `[n]` citations. Without an OpenRouter
key it returns the real search evidence with a snippet summary instead.

### Automatic free-model selection

With `DEEP_RESEARCH_OPENROUTER_MODEL=auto` (default) and `DEEP_RESEARCH_OPENROUTER_VISION_MODEL=auto`:

1. On first use the gateway calls `GET https://openrouter.ai/api/v1/models`
   and keeps only models whose id ends in `:free`.
2. Text and vision candidates are ranked separately (vision = `image` in
   `input_modalities`): general-purpose before specialty models
   (content-safety, note/code previews), text+image before text-only for
   vision, then larger context length first.
3. **Probe + persist (first run / fresh clone):** the top
   `DEEP_RESEARCH_OPENROUTER_PROBE_COUNT` (default 3) text and vision candidates get a
   tiny concurrent probe (`"Reply with the single word: ok"`, a 1×1 image for
   vision, `max_tokens=16`, `DEEP_RESEARCH_OPENROUTER_PROBE_TIMEOUT_SECONDS`, default 8s).
   The fastest working model per kind is selected; working models are ordered
   fastest-first and failing ones go last.
4. The selection, ranked lists, probe results and a timestamp are saved as JSON
   to `DEEP_RESEARCH_OPENROUTER_MODEL_CACHE_PATH`. If unset, the per-user cache dir is
   used: `~/.cache/deep_research/openrouter_free_models.json` (Linux/XDG),
   `~/Library/Caches/deep_research/…` (macOS), `%LOCALAPPDATA%\deep_research\…` (Windows).
   Set it to `off` to disable. **The API key is never written.** Restarts reuse
   the file for `DEEP_RESEARCH_OPENROUTER_MODEL_CACHE_SECONDS` (default 6 hours); once
   it expires (e.g. the next day) the app re-discovers and re-probes
   automatically. A corrupt file, or one written for another base URL, is ignored.
5. On HTTP 403/404/429/5xx, "unavailable for free", "only available on…",
   rate-limit or overload responses, the failed model is demoted, the catalog
   refreshed and the next free model tried automatically (up to 6 per call).
6. Setting a concrete `:free` slug pins that model first; failures still fall
   back to the rest of the free catalog. `FreeModelGuard` still rejects any
   non-`:free` id.

### Automatic free search chain

With `DEEP_RESEARCH_SEARCH_PROVIDER=auto` (default) sources are tried in order and
blocked or empty ones are skipped with no extra config:

1. Tavily, only if `DEEP_RESEARCH_TAVILY_API_KEY` is set
2. DuckDuckGo HTML (`html.duckduckgo.com`); HTTP 202 / bot-challenge pages count as failure
3. DuckDuckGo Lite (`lite.duckduckgo.com`)
4. Wikipedia full-text search API
5. DuckDuckGo Instant Answer API

The search query is the research objective itself (no `latest` prefix).
`DEEP_RESEARCH_SEARCH_PROVIDER=duckduckgo` uses only the free chain;
`DEEP_RESEARCH_SEARCH_PROVIDER=tavily` uses only Tavily.

## PDF and image inputs

`POST` research submissions accept up to 5 `attachments`, each
`{"kind": "pdf" | "image", "data": "data:<mime>;base64,...", "name": "optional"}`.

* **PDF** (`pdf_reader`, pypdf): offline per-page text extraction; 20 MiB,
  200 pages and 200k characters max; encrypted and malformed files return safe
  structured errors.
  * **Scanned / image-only PDFs don't fail the run.** The result carries the
    note `no extractable text`, the page count, metadata and an inventory of
    embedded images (count, page, pixel size). If a free vision model is
    available, the first page's embedded images (up to 2) are described;
    otherwise the note says what is present.
* **Image** (`image_analyzer`, Pillow): PNG/JPEG/WebP/GIF validation (declared
  type must match real format), 10 MiB / 40 MP limits, metadata extraction.
  With `DEEP_RESEARCH_OPENROUTER_API_KEY` set it also asks the free vision model
  (`DEEP_RESEARCH_OPENROUTER_VISION_MODEL`, default `auto`; `:free` only, guard-enforced) for a
  description; otherwise it returns metadata only and never invents one.

Each attachment becomes its own research task in both offline and live modes,
scheduled **before** the web tasks. In live mode the extracted PDF text is
split into ~1.2k-character page chunks, and image or scan descriptions are
added too. Each web section gets the most query-relevant chunks (up to 4) as
**primary** sources numbered ahead of web results. The LLM is told to use and
cite them first, and the report lists them as `[n] <file> (attached pdf, p.N)
- user attachment`. An `## Attached files` section summarises each
attachment (pages; for scans: no extractable text and image count). If web
search is blocked, sections still complete from the attachments alone.

### Report sections

Section headings are short and keyword-based; they never repeat the whole
question. The first section is always **Fundamentals**. The next two come from
aspects detected in the question: **Benefits** (benefit, advantage, pros),
**Risks** (risk, drawback, cons, challenge), **Costs and economics**,
**Comparison**, **History**, **Recent developments**. If fewer than two
aspects are detected, **Recent developments** and **Significance and impact**
fill the gap. Each section's search query and LLM focus are scoped to its
aspect, which avoids three near-identical answers.

## Viewing a live run

The simplest way to try it is the Swagger UI:

```bash
DEEP_RESEARCH_RESEARCH_MODE=live DEEP_RESEARCH_OPENROUTER_API_KEY=... uvicorn deep_research.api.main:app --port 8000
# open http://127.0.0.1:8000/docs -> POST /research -> "Try it out"
```

The `report.content` field is Markdown, with numbered citations and a
`## Sources` list.

## Docker

```bash
docker build -t deep-research .
docker run --rm -p 8000:8000 deep-research                      # offline mode
docker run --rm -p 8000:8000 --env-file .env deep-research # e.g. DEEP_RESEARCH_RESEARCH_MODE=live
```
