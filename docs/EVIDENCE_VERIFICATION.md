# Evidence verification (Phase 4)

Every live report is post-processed by `DeterministicReportAgent` (see `deep_research/verification/`):

1. **Per-claim fact check** (`fact_check.py`) – each cited sentence is a claim, checked against the text read from
   the sources it cites. A free LLM judge (the same free-model-guarded gateway as the agents, `:free` models only)
   decides when an OpenRouter key is set; otherwise, and for any claim the judge cannot decide (rate limit, timeout,
   invalid reply, budget, more than 12 claims), an offline heuristic (term overlap, figures, negation) is used.
   Each verdict records its `method`. Output: `report["verification"]` and a *Claim verification* section.
2. **Contradictions** (`contradictions.py`) – cross-source disagreements (negation, antonyms, conflicting figures for
   the same unit) and claims contradicted by their own source. Output: `report["contradictions"]` and a
   *Contradictions and disagreements* section (always present, says "none detected" when clean).
3. **Charts** (`charts.py`) – verdict counts, citations per section and sources by domain, saved as JSON specs in
   `report["charts"]`, rendered as text charts in the Markdown report and as native bar charts in the dashboard viewer.

## Offline eval suite

```
deep-research-eval                     # bundled cases
deep-research-eval runs/*.json --json  # score saved runs (uses citation excerpts)
deep-research-eval --min-overall 0.6   # non-zero exit below the threshold (CI gate)
```

Scorecard metrics (0–1): **faithfulness** (claims backed by their cited text), **relevance** (claims on the
objective), **coverage** (expected key points, or sections with cited claims). Fully offline and deterministic.
