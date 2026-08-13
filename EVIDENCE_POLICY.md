# Evidence Policy

## Definition of Evidence
Evidence is information obtained from a source that has been:
1. **Collected** via a research tool (web search, document retrieval, etc.)
2. **Extracted** in a machine-readable format (text, structured data, etc.)
3. **Normalized** to a common internal format for analysis
4. **Attributed** to its original source with metadata (URL, timestamp, author, etc.)

## What Is Not Evidence
- Raw, unprocessed tool outputs that have not been extracted or normalized.
- Information generated solely by the language model without grounding in collected sources.
- Claims or assertions that lack traceable support from collected evidence.
- Opinions, inferences, or syntheses that have not been validated against source material.

## Evidence Requirements
All evidence used in the research process must:
- Have a clear **source provenance** (where it came from).
- Be **verifiable** (in principle, accessible to others).
- Be **relevant** to the research question or sub-question.
- Be **timely** (within acceptable recency bounds, unless historical context is explicitly required).
- Be **credible** (assessed via basic heuristics: domain reputation, cross-referencing, etc.; deep credibility assessment is part of evaluation).

## Evidence Handling
- Evidence is immutable once collected; any processing creates derivative evidence with clear provenance.
- Phase 5 persistence enforces immutability for previously stored evidence, sources, and claims and rejects updates that remove or rewrite those historical objects.
- The system maintains an evidence trace showing how raw inputs were transformed into analyzed evidence.
- Evidence versioning is not required in V1; each research session starts with fresh evidence collection.

## Evidence Sources
The system may collect evidence from:
- Public web pages (HTML, text)
- Documents (PDF, DOCX, TXT, etc.)
- Structured data (JSON, CSV, API responses)
- Multimedia (images, video/audio transcripts)
- Other sources as tools are developed.

## Evidence Limitations
- Evidence collection is subject to tool capabilities (e.g., web search tool may be rate-limited or blocked by some sites).
- The system does not bypass paywalls or access controls without explicit permission.
- Evidence from user-provided files is trusted as provided; the system does not validate the truthfulness of user-uploaded content beyond basic sanity checks.
