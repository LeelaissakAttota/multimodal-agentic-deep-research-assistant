# Assistant features (Phase 5)

Local features that work on finished reports. All state is in one SQLite file,
`<memory_dir>/assistant.db` (default `data/assistant.db`, git-ignored; override with
`DEEP_RESEARCH_ASSISTANT_DB`). Nothing is sent to external services except the optional
free-model call for chat.

## Report follow-up chat (`assistant/followup.py`)

- The report is split into passages: body sentences (the `## Sources` list is excluded),
  scored claims, and each citation's title and excerpt. Each passage carries the `[n]` citations
  that exist in the report.
- Retrieval scores passages by the share of question terms they contain. When no cited passage
  reaches the threshold, the answer is a refusal (`supported=false`): *"The report does not contain
  evidence that answers this question…"*. The model is not called in that case.
- With an OpenRouter key, a free model (gateway built through the free-model guard) phrases the
  answer from the retrieved passages only. Its reply must be JSON with citations. If it uses a
  citation that is not in the retrieved passages, gives no citation, or fails (rate limit, timeout,
  bad JSON), the offline extractive answer is used instead and the reason is recorded. If the model
  says the passages do not answer the question, the reply is a refusal.
- Both turns are saved per run (`chat` table) and shown in the dashboard with a grounded/unsupported
  label.

Limits: retrieval is lexical (no embeddings), so paraphrased questions can be refused even when
the report covers them. The answer is only as good as the report itself.

## Profile and preferences (`assistant/profile.py`)

| Field | Effect on a new run |
|---|---|
| `saved_topics` | When a topic shares a meaningful term with the question (stop words ignored, simple plural matching) and adds a new term, the planner adds **one** extra web-search task toward the best-matching topic (section "Related to <topic>"). Unrelated or empty topics leave the plan unchanged. Applies to the deterministic planner and the LLM planner's fallback. |
| `preferred_sources` | The planner always adds one task for the first preferred, enabled source tool that keyword routing did not pick. |
| `blocked_sources` | Removed from `live_source_tools`, so neither the planner nor the LLM planner can use them. |
| `blocked_domains` | Web search and source-tool results from these hosts and their subdomains are dropped. Entries are normalized: scheme, path, query, port, userinfo, trailing dot and a leading `www.`/`*.` are removed and the host is lowercased, so `https://Spam.net/x`, `spam.net:8080` and `spam.net.` all mean `spam.net`. Result hosts are normalized the same way (`spam.net.` is blocked). |
| `default_depth` | `quick` = 1 iteration, 3 results, 1 page fetched. `standard` = 3 / 5 / 2. `deep` = 5 / 8 / 4. |

The control service applies the profile in `settings_with_keys` for every run, reading
the store without creating it. With no profile saved, behaviour is unchanged.

## Notes and reminders (`assistant/notes.py`)

- Notes can be added, edited and deleted on each report.
- Reminders have a due time stored in UTC. The dashboard enters and shows times in the display
  zone (IST by default). Reminders can be marked done or deleted.
- The Dashboard lists open reminders, with overdue ones first.
- There are no email, push or desktop notifications.

## Static HTML and share links (`assistant/share.py`)

- `render_html(report)` produces one self-contained document:
  - inline CSS only; no scripts, fonts, images or external stylesheets
  - all report text HTML-escaped
  - `[n]` markers linked to the source list
  - only `http(s)` source URLs turned into links
- A share stores an HTML snapshot taken at creation time, plus the SHA-256 hash of a random token.
  The token is returned once, inside the link.
- Viewing a share (`GET /share/{id}?token=`) compares the token in constant time. Revoked shares and
  bad tokens return 404.
- Links expire after 7 days by default (`DEEP_RESEARCH_SHARE_TTL_DAYS`, 0 = never, max 365). A
  share can override this on create (`expires_in_days`, 0 = never). `expires_at` is stored in UTC;
  expired links return the same 404 as revoked ones and are listed with status `expired`.
  Databases created before expiry existed are migrated on open; their old shares never expire. See [CONTROL_API.md](CONTROL_API.md) for the routes and headers.
- The control API binds to 127.0.0.1, so a link only works on that machine unless you deliberately
  expose the port.
