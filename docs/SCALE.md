# Scaling MDRA (Phase 6): optional backends

Every backend here is **optional and off by default**. With no `MDRA_*` variables set, MDRA behaves exactly
as before: SQLite session storage, exports only downloaded (or saved to a local folder on request), research
runs in the control API process, no tracing, no experiments, and a knowledge graph built locally in memory.
Everything is free and runs locally; no paid service is needed.

The dashboard shows the real state of each backend under **Tools & Integration → Scale backends**
(probed live, cached 60 s): `not configured` (using the default), `configured`/`connected`, or clearly
marked `unavailable` (missing Python extra), `unreachable` (service down) or `invalid config`. Status rows
never contain passwords, keys or URLs with credentials.

| # | Feature | Enable with | Python extra | Default without it |
|---|---|---|---|---|
| 1 | Background job queue (RQ on Redis) | `MDRA_REDIS_URL` | `queue` | in-process runs |
| 2 | Session database (Postgres) | `MDRA_DATABASE_URL` | `postgres` | `data/research_memory.sqlite` |
| 3 | Artifact storage (MinIO / S3) | `MDRA_S3_BUCKET` (+ `MDRA_S3_*`) | `s3` | `data/artifacts/` folder |
| 4 | Tracing (OpenTelemetry) | `MDRA_OTEL_EXPORTER=console\|otlp`, `MDRA_TRACE_STORE=sqlite\|jsonl` | `tracing` | off |
| 5 | A/B experiments | `MDRA_AB_CONFIG` | – | default prompts |
| 6 | Knowledge graph sync (Neo4j) + GraphRAG | `MDRA_NEO4J_URI`, `MDRA_GRAPH_RAG`, `MDRA_GRAPH_AUTO_SYNC` | `graph` | local graph only |
| 7 | Chrome extension | load `extensions/mdra-chrome` | – | – |

Install extras as needed, e.g. `pip install -e ".[dashboard,queue,postgres,s3,tracing,graph]"`.

## Local services: docker-compose

`docker-compose.yml` starts free local services, all bound to `127.0.0.1`:

```bash
docker compose up -d                   # redis :6379, postgres :5433, minio/Silo :9000 (console :9001), otel-collector :4318
docker compose --profile graph up -d   # + neo4j :7474/:7687
```

Host ports are configurable (always bound to `127.0.0.1`). Postgres uses host port **5433** by default so it
does not clash with a locally installed PostgreSQL on 5432 (common on Windows with Docker Desktop). Set any of
these in your shell or in the `.env` next to `docker-compose.yml`, and change the matching `MDRA_*` URL below:

| Variable | Default | Service |
|---|---|---|
| `MDRA_PG_PORT` | 5433 | Postgres |
| `MDRA_REDIS_PORT` | 6379 | Redis |
| `MDRA_MINIO_PORT` / `MDRA_MINIO_CONSOLE_PORT` | 9000 / 9001 | MinIO API / console |
| `MDRA_OTEL_PORT` | 4318 | OTLP/HTTP collector |
| `MDRA_NEO4J_HTTP_PORT` / `MDRA_NEO4J_BOLT_PORT` | 7474 / 7687 | Neo4j browser / Bolt |

PowerShell example: `$env:MDRA_PG_PORT=5434; docker compose up -d`, then use `...@localhost:5434/mdra`.

**Object storage image.** The upstream `minio/minio` / `minio/mc` images (Docker Hub and quay.io) are no longer
published, so `docker compose` uses **Silo** (`docker.io/pgsty/silo`), the community-maintained MinIO fork with the
same S3 API, `MINIO_*` variables, ports 9000/9001 and on-disk format, plus `docker.io/pgsty/mc` for bucket setup. Both
are pinned to `RELEASE.2026-10-18T00-00-00Z` with a multi-arch (amd64/arm64) digest. Any S3-compatible endpoint
works instead: point `MDRA_S3_ENDPOINT` at it.

**Pinned images.** Every image is pinned to an exact version plus its multi-arch digest (checked on Docker Hub,
anonymous pull, amd64 + arm64), so `docker compose pull` is reproducible:

| Service | Image |
|---|---|
| redis | `redis:7.4.11-alpine` |
| postgres | `pgvector/pgvector:0.8.7-pg16-bookworm` (PostgreSQL 16.15 + pgvector 0.8.7; stay on major 16 - the `postgres-data` volume is a PG16 cluster) |
| minio | `pgsty/silo:RELEASE.2026-10-18T00-00-00Z` |
| minio-init | `pgsty/mc:RELEASE.2026-10-18T00-00-00Z` |
| otel-collector | `otel/opentelemetry-collector:0.163.2` |
| neo4j | `neo4j:5.26.32-community` |

To upgrade, change tag and digest together (`docker buildx imagetools inspect <image:tag>` prints the digest).

The compose file uses local development passwords (`mdra-local-dev`); override them with
`MDRA_PG_PASSWORD`, `MDRA_MINIO_USER`, `MDRA_MINIO_PASSWORD`, `MDRA_NEO4J_PASSWORD`. Matching `.env` entries:

```dotenv
MDRA_REDIS_URL=redis://localhost:6379/0
MDRA_DATABASE_URL=postgresql://mdra:mdra-local-dev@localhost:5433/mdra
MDRA_S3_BUCKET=mdra-artifacts
MDRA_S3_ENDPOINT=http://localhost:9000
MDRA_S3_ACCESS_KEY=mdra-local
MDRA_S3_SECRET_KEY=mdra-local-dev
MDRA_OTEL_EXPORTER=otlp
MDRA_OTEL_ENDPOINT=http://localhost:4318/v1/traces
MDRA_NEO4J_URI=bolt://localhost:7687
MDRA_NEO4J_PASSWORD=mdra-local-dev
```

On Windows without Docker, Redis-compatible **Memurai** (free developer edition) or Redis under WSL work for the
queue, and the PostgreSQL Windows installer works for the database; or simply leave them unset.

## 1. Job queue

With `MDRA_REDIS_URL` set, the control API enqueues each research run on the RQ queue `MDRA_RQ_QUEUE`
(default `mdra`) and follows the job; logs, stop requests and results still appear in the dashboard. Start
one or more workers:

```bash
deep-research-worker            # uses SimpleWorker automatically on Windows (RQ's default worker needs fork)
deep-research-worker --simple   # force SimpleWorker anywhere
deep-research-worker --burst    # drain the queue and exit
```

Notes: approval-mode runs need step-by-step approvals, so they always run in-process. Jobs carry only the
objective, mode, attachments and run id; workers read API keys from their own `.env`/secret store, never from
Redis. If the queue extra is missing or the URL is invalid, the control API keeps running in-process and the
status row explains why. `MDRA_JOB_TIMEOUT_SECONDS` (default 1800) and `MDRA_JOB_POLL_SECONDS` tune jobs.

## 2. Postgres session storage

`MDRA_DATABASE_URL` switches the research-session repository (same protocol as the SQLite one, with row locks
on Postgres) to SQLAlchemy + psycopg. `postgres://` and `postgresql://` URLs are accepted. Move data with:

```bash
deep-research-migrate copy --from data/research_memory.sqlite --to env    # env = MDRA_DATABASE_URL
deep-research-migrate export --from env --out sessions.jsonl
deep-research-migrate import --in sessions.jsonl --to data/research_memory.sqlite
```

The assistant database (`data/assistant.db`: chat, notes, reminders, shares) stays on SQLite.

**pgvector image.** The compose Postgres is `pgvector/pgvector:0.8.7-pg16-bookworm`: the official PostgreSQL
16.15 build plus the `vector` extension, so the existing PG16 `postgres-data` volume opens as-is (same major,
same minor). It replaced `postgres:16.15-alpine`; Alpine (musl) and Debian (glibc) sort non-ASCII text
differently, so after the first start on an existing volume rebuild text indexes once (safe and quick at this
size):

```bash
docker compose exec postgres psql -U mdra -d mdra -c "REINDEX DATABASE mdra" -c "ALTER DATABASE mdra REFRESH COLLATION VERSION"
```

RAG creates the extension itself (`CREATE EXTENSION IF NOT EXISTS vector`) on first use; see section 8.

## 3. Artifact storage

Saving a copy of an export (dashboard **Save copy**, or `POST /control/runs/{id}/artifacts?format=pdf`) writes
to `runs/<run_id>/<file>` in the MinIO/S3 bucket when `MDRA_S3_BUCKET` is set (path-style addressing,
`MDRA_S3_ENDPOINT` for MinIO, `MDRA_S3_PREFIX` default `mdra`), otherwise to `MDRA_ARTIFACTS_DIR`
(default `data/artifacts`). Nothing is written unless you ask for it. `GET /control/artifacts?run_id=` lists
saved copies; `GET /control/artifacts/{key}` downloads one.

## 4. Tracing

`MDRA_OTEL_EXPORTER=console` prints spans to stdout; `otlp` sends them over OTLP/HTTP to
`MDRA_OTEL_ENDPOINT` (the compose collector prints them; point it at Jaeger for a UI). Spans:

* `mdra.run` – mode, agentic flag, objective length (not the text), attachment kinds, status, iterations
* `mdra.agent` – planning, research, analysis, evaluation, reporting (+ task tool/section for research)
* `mdra.tool` – tool name, parameter **names** only, success

No objective text, prompts, parameter values, keys or tokens are put in spans; attributes go through a
redactor that drops sensitive keys and strips URL credentials, tokens and key-like strings. Exceptions are
recorded by type only.

## 5. A/B experiments

```dotenv
MDRA_AB_CONFIG={"name":"planner-style","variants":[{"name":"control"},{"name":"broad","overrides":{"agent_plan_style":"broad","agent_max_plan_tasks":5}}]}
```

(or a path to a JSON file). Each control-API run is assigned a variant from `sha256(experiment:run_id)`, so the
same run always gets the same variant, also on a queue worker. Overrides are limited to research settings
(`agent_plan_style` default/broad/focused, `agent_max_plan_tasks`, `agent_tool_steps`, `agent_mode`,
`max_research_iterations`, `live_max_search_results`, `live_max_fetch_pages`, `openrouter_model`) and are
validated; keys can never be overridden. The run log and the result's `experiment` field record the variant.
Compare variants with the existing evaluation suite on saved run JSON files:

```bash
deep-research-eval data/dashboard_runs/*.json --by-variant
```

## 6. Knowledge graph

**Knowledge Base → Graph** builds a graph from every finished report: report → claim (`makes`), claim →
source (`supported_by`), report → source (`cites`), source → site domain (`hosted_on`). Claims are merged
across runs by normalized text and sources by URL (credentials, query tokens and fragments removed). It is
rendered with Graphviz in the browser; `networkx` (graph extra) is used for analysis when installed. With
`MDRA_NEO4J_URI` (+ `MDRA_NEO4J_USER`, `MDRA_NEO4J_PASSWORD`) a **Sync graph to Neo4j** button MERGEs the
graph as `:MdraNode` nodes and `:REL` relationships (idempotent).

## 7. Chrome extension

See [extensions/mdra-chrome/README.md](../extensions/mdra-chrome/README.md). It sends the current page or
selection to the local control API as a new research (`POST /control/runs`) or as a source
(`POST /control/clips`, shown under **Sources → Clipped from browser**). Host permissions are limited to
`127.0.0.1`/`localhost`, the token lives only in `chrome.storage.local` and is never logged, and the clip
endpoints also refuse non-loopback clients.

## 8. Report RAG (hybrid retrieval)

Off by default. `MDRA_RAG_ENABLED=true` makes the report follow-up chat and the Knowledge Base memory search
use hybrid retrieval: Okapi BM25 over chunk text plus vector similarity, fused with reciprocal rank fusion
(RRF, k = 60). Answers keep the report's `[n]` citations, and a question is answered only when a cited chunk
is close enough (`MDRA_RAG_MIN_SIMILARITY`) or shares enough terms with it; otherwise the chat refuses.

| Variable | Default | Meaning |
|---|---|---|
| `MDRA_RAG_ENABLED` | `false` | turn RAG on (off = the original word-overlap retrieval) |
| `MDRA_RAG_EMBEDDINGS` | `local` | `local` (sentence-transformers) or `api` (OpenAI-compatible `/embeddings`) |
| `MDRA_RAG_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | local model (free, runs on CPU) |
| `MDRA_RAG_API_BASE` | `https://openrouter.ai/api/v1` | API endpoint; `http://localhost:11434/v1` for Ollama |
| `MDRA_RAG_API_MODEL` | `nvidia/nemotron-3-embed-1b:free` | must end in `:free` unless the endpoint is on localhost |
| `MDRA_RAG_API_KEY` | (OpenRouter key) | defaults to `DEEP_RESEARCH_OPENROUTER_API_KEY` for openrouter.ai |
| `MDRA_RAG_INDEX` | `auto` | `auto` = pgvector when `MDRA_DATABASE_URL` is set and the extension exists, else local; `local`; `pgvector` |
| `MDRA_RAG_INDEX_DIR` | `data/rag` | local store (one `chunks.jsonl` per embedding model) |
| `MDRA_RAG_CHUNK_WORDS` / `MDRA_RAG_OVERLAP_WORDS` | `120` / `30` | chunk window and carried-over context |
| `MDRA_RAG_TOP_K` | `6` | chunks retrieved per question |
| `MDRA_RAG_MIN_SIMILARITY` | `0.35` | cosine needed to treat a chunk as supporting |

Install `pip install -e ".[rag]"` (sentence-transformers, faiss-cpu, numpy); add `postgres` for pgvector.
Without the embeddings dependencies (or with a non-free API model, or no key) everything falls back to word
overlap and the Knowledge Base shows why.

**Chunking.** Report body: split at headings (the Sources/References list is dropped), then sentences are
packed into windows of at most `CHUNK_WORDS` words (a hard cap: text without sentence breaks, e.g. lowercase or
Telugu, is cut into overlapping word windows), carrying whole trailing sentences up to `OVERLAP_WORDS` into the next one.
Each claim is one chunk; each citation's title + excerpt is windowed the same way and tagged with its number.
Only citation numbers present in the report are kept. A report is re-embedded only when its content, claims
or citations change (hash).

**Index schema.** pgvector table `mdra_rag_chunks(model, chunk_id, run_id, kind, ord, text, citations JSON,
title, section, doc_hash, embedding vector, created_at)`, primary key `(model, chunk_id)`, index
`(model, run_id)`, plus `mdra_rag_docs(model, run_id, doc_hash, chunks, updated_at)` so reports with no chunks are
remembered too; exact cosine search with `<=>`. The local store keeps the same per-report hashes in `docs.json`. The local store uses FAISS `IndexFlatIP` when installed,
else numpy, over unit vectors.

**Backfill** existing reports (saved under `dashboard_runs/`):

```bash
deep-research rag-backfill                      # or --runs-dir PATH, --force to re-embed everything
```

New reports are indexed the first time you ask about them or search the Knowledge Base.

## 9. Audio, video and YouTube analysis

Off by default. `MDRA_MEDIA_ENABLED=true` plus `pip install -e ".[media]"` (faster-whisper, yt-dlp, PyAV)
turns on the `media_analyzer` tool. The dashboard, CLI (`--file talk.mp3`, `--file https://youtu.be/...`) and
API (`{"kind": "media", "data": "data:audio/...;base64,..." | "https://..."}`) accept audio/video files (up to
20 MB inline) and YouTube/Vimeo or direct media links. When it is off or a dependency is missing, the attachment
task fails with a message that names the missing piece and the install command; nothing is invented.

Pipeline:
1. **YouTube / links:** yt-dlp reads the metadata. With `MDRA_MEDIA_PREFER_CAPTIONS=true` (default) manual
   subtitles are used first, then the original-language auto captions (never machine translations), parsed from
   json3/VTT with rolling duplicates removed. With no captions, yt-dlp downloads the audio (size cap
   `MDRA_MEDIA_MAX_DOWNLOAD_MB`) and it is transcribed.
2. **Transcription:** `MDRA_MEDIA_TRANSCRIBER=local` (default) runs faster-whisper (`MDRA_MEDIA_WHISPER_MODEL`,
   default `base`; it decodes audio and video containers itself, ffmpeg not needed). `api` posts to an
   OpenAI-compatible `/audio/transcriptions` (`verbose_json`) at `MDRA_MEDIA_API_BASE`; only localhost/`.local`
   servers (e.g. faster-whisper-server / speaches), or `:free` models on OpenRouter (`openrouter.ai`), are
   accepted. The `:free` suffix counts on no other host. With ffmpeg on PATH the
   audio is first reduced to 16 kHz mono MP3.
3. **Keyframes (video):** `MDRA_MEDIA_KEYFRAMES` (default 6) evenly spaced frames are grabbed with
   ffmpeg/ffprobe, or PyAV when ffmpeg is missing, and captioned by the configured free vision model. Without a
   vision model the frames are skipped with a note. For YouTube this needs `MDRA_MEDIA_YOUTUBE_FRAMES=true`
   (downloads a <=360p video).
4. **Evidence:** speech is grouped into `MDRA_MEDIA_WINDOW_SECONDS` (default 60) windows, each frame caption
   is its own entry; every entry is labelled `mm:ss` (`h:mm:ss` past an hour). Sources are cited as
   `Title (attached video, at 01:15)`, YouTube citations deep-link with `&t=75s`, and the excerpts
   (`[01:15-02:10] ...`) go into the report, so report RAG (section 8) indexes them with their timestamps.

| Variable | Default | Meaning |
|---|---|---|
| `MDRA_MEDIA_ENABLED` | `false` | master switch |
| `MDRA_MEDIA_TRANSCRIBER` | `local` | `local` (faster-whisper) or `api` |
| `MDRA_MEDIA_WHISPER_MODEL` | `base` | faster-whisper model name or local path |
| `MDRA_MEDIA_WHISPER_DEVICE` | `auto` | `auto`, `cpu` or `cuda` |
| `MDRA_MEDIA_WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 compute type (`int8`, `float16`, `int8_float16`, ...) |
| `MDRA_MEDIA_LANGUAGE` | auto | ISO language for transcription and caption choice |
| `MDRA_MEDIA_API_BASE` | `http://127.0.0.1:8000/v1` | OpenAI-compatible transcription server (local, or OpenRouter) |
| `MDRA_MEDIA_API_MODEL` | `whisper-1` | model sent to that server; must end in `:free` on OpenRouter |
| `MDRA_MEDIA_API_KEY` | none | optional bearer token for that server (never logged) |
| `MDRA_MEDIA_PREFER_CAPTIONS` | `true` | YouTube captions before transcription |
| `MDRA_MEDIA_KEYFRAMES` | `6` | frames per video (0 = none) |
| `MDRA_MEDIA_YOUTUBE_FRAMES` | `false` | download low-res YouTube video for keyframes |
| `MDRA_MEDIA_WINDOW_SECONDS` | `60` | transcript seconds per cited chunk |
| `MDRA_MEDIA_MAX_DURATION_SECONDS` | `10800` | longer media is refused |
| `MDRA_MEDIA_MAX_DOWNLOAD_MB` | `200` | yt-dlp download cap |
| `MDRA_MEDIA_TIMEOUT_SECONDS` | `600` | transcription timeout (also each HTTP phase of the API call) |

Notes: PyAV is pinned `<16` because faster-whisper 1.2 calls `av.open(metadata_errors=...)`, which PyAV 16
removed. YouTube may answer "Sign in to confirm you're not a bot" from datacenter IPs; the error is shown as-is
(captions-only requests are lighter and usually still work). Links go through the same public-address SSRF check
as web pages, and so does every request yt-dlp makes (extractor pages, caption URLs from the video info, the
chosen media format URLs before download, and the final URL after redirects, checked before any body is read).
yt-dlp's native downloaders are forced so ffmpeg never fetches URLs. Known gap: an intermediate redirect hop is
requested before it can be inspected; its response body is never used.

**Timeouts.** On `MDRA_MEDIA_TIMEOUT_SECONDS` the run fails with `transcription_timeout` and the worker is told
to stop: faster-whisper decodes lazily, so it stops at the next segment; the API transcriber stops before its
request, and an in-flight request is capped by the same httpx timeout per phase. Transcription runs on a
dedicated 2-thread pool, so stragglers never pile up as unbounded threads. Not interruptible: a faster-whisper
model load/download and the current segment, which finish in the background (still within the 2 threads).

## 10. GraphRAG (Neo4j)

The knowledge graph (section 6) now also has **entity** nodes (acronyms and capitalised multi-word names,
extracted without a model; claim → entity `mentions`) and claim → claim `contradicts` edges from the report's
contradiction check. Each report is one `report` node (= one run); every node keeps the list of runs it
appears in.

| Variable | Default | Meaning |
|---|---|---|
| `MDRA_GRAPH_RAG` | `false` | expand the report chat's hybrid RAG hits through graph neighbours |
| `MDRA_GRAPH_AUTO_SYNC` | `false` | MERGE each finished run into Neo4j (control API runs and queue workers) |
| `MDRA_GRAPH_BACKEND` | `auto` | `auto` = Neo4j when configured and reachable, else local; `neo4j`; `local` |
| `MDRA_GRAPH_WEIGHT` | `0.5` | neighbour score = weight × seed hit score × relation factor |
| `MDRA_GRAPH_MAX_NEIGHBOURS` | `4` | neighbours added per question (0 = none) |
| `MDRA_GRAPH_TIMEOUT_SECONDS` | `5` | Neo4j connect timeout before falling back |

**Sync.** `deep-research graph-sync [--runs-dir DIR]` MERGEs every saved run (`:MdraNode {id}` with a uniqueness
constraint, `:REL {type}` relationships). It is idempotent, and a node's `runs` list is unioned, so syncing one
run never removes another run from a shared claim or source. With `MDRA_GRAPH_AUTO_SYNC=true` each finished run
is synced as it completes (and before a graph query when the chat uses Neo4j). The dashboard button still works.

**Retrieval.** With `MDRA_GRAPH_RAG=true` the chat takes the hybrid hits (RRF of BM25 and vectors, section 8) as
seeds: claim hits by their claim node, and every hit by the sources it cites. Neighbours are claims that
contradict a seed (factor 1.0), share a cited source (0.8) or mention the same entity (0.6). They are merged
into the ranking with `weight × seed score × factor` and shown as `graph:<relation>` passages. Only claims of the
**report being asked about** with at least one valid citation are added, so every `[n]` still points into that
report. A neighbour passes the evidence gate only if its seed did. The chat caption then says
`hybrid+graph retrieval` and `graph neo4j: +N` or `graph local: +N`.

**Fallback.** Without `MDRA_NEO4J_URI`, without the `neo4j` package, or when Neo4j is unreachable or rejects the
login, the same neighbours are read from the local in-process graph built from the report. Any graph error
leaves the plain hybrid answer unchanged. Errors are logged and shown by exception type only, never with the
URI or the password.

Local Neo4j: `docker compose --profile graph up -d` (5.26 community; the neighbour query needs 5.23+). Without
Docker, the community tarball works too (it needs Java 21).

## 11. Per-run trace timeline

`MDRA_TRACE_STORE=sqlite|jsonl` (default `off`, `tracing` extra) adds a local span processor that writes each
finished span of a run to `MDRA_TRACE_DIR` (default `data/traces`): one `spans.db` file, or one
`<run_id>.jsonl` per run. The control API passes its run id into the research run, so `mdra.run`, `mdra.agent`
and `mdra.tool` spans (including ones in child tasks and threads) are tagged `mdra.run_id`. Spans outside a run
are not stored. The store works on its own or together with `MDRA_OTEL_EXPORTER=console|otlp`; with both, the
collector still gets every span.

The report page's **⏱ Trace** tab draws a waterfall: one bar per span, indented by parent, positioned and sized
by its share of the run, with its duration. Errors are shown in red with the exception type. The attributes are
in a table below. Attributes are redacted a second time when written (the same `safe_attributes` redactor as
the exporter), so keys, tokens and URL credentials never reach the store; exception messages are never stored.
Runs recorded by a queue worker show up when the worker shares the same `MDRA_TRACE_DIR`.

## Testing

All features are unit-tested with fakes (fakeredis, an in-memory S3 fake, SQLite through SQLAlchemy,
in-memory span exporter, a fake Neo4j driver, node tests for the extension). Tests against real services are
skipped unless you point them at one:

```bash
MDRA_TEST_REDIS_URL=redis://localhost:6379/15 \
MDRA_TEST_DATABASE_URL=postgresql://mdra:mdra-local-dev@localhost:5433/mdra \
MDRA_TEST_S3_BUCKET=mdra-test MDRA_TEST_S3_ENDPOINT=http://localhost:9000 \
MDRA_TEST_S3_ACCESS_KEY=mdra-local MDRA_TEST_S3_SECRET_KEY=mdra-local-dev \
MDRA_TEST_NEO4J_URI=bolt://localhost:7687 MDRA_TEST_NEO4J_PASSWORD=mdra-local-dev \
pytest tests/unit/test_scale_*.py tests/unit/test_rag.py   # test_rag's pgvector test needs the extension
```
