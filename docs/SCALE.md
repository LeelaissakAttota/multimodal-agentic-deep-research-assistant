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
| 4 | Tracing (OpenTelemetry) | `MDRA_OTEL_EXPORTER=console\|otlp` | `tracing` | off |
| 5 | A/B experiments | `MDRA_AB_CONFIG` | – | default prompts |
| 6 | Knowledge graph sync (Neo4j) | `MDRA_NEO4J_URI` | `graph` | local graph only |
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
