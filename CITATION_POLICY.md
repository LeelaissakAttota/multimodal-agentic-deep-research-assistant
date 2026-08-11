# Citation Policy

## Purpose
Citations provide traceability from claims in the final report back to the specific evidence that supports them.

## Citation Requirements
All factual claims in the final report **must** be accompanied by a citation that:
1. Points to a specific piece of evidence in the system's evidence base.
2. Includes sufficient information to locate the original source (e.g., URL, document ID, timestamp).
3. Is formatted consistently (style to be defined, e.g., IEEE, APA, or numeric bracketed).

## What Requires a Citation
- Statements of fact (e.g., "The Eiffel Tower is 330 meters tall.")
- Statistical data (e.g., "In 2023, global renewable energy capacity reached 3,870 GW.")
- Historical events (e.g., "The Berlin Wall fell in November 1989.")
- Quotations from sources.
- Any information not considered common knowledge in the target audience's context.

## What Does Not Require a Citation
- Common knowledge (e.g., "Water boils at 100°C at sea level." - but note: context matters; in a physics paper, this might need citation).
- Logical deductions explicitly shown in the report's reasoning (if the premises are cited, the deduction may not need its own citation).
- Methodological descriptions (e.g., "We performed a web search for recent studies on X.").
- The research process itself (e.g., "We consulted three sources.").

## Citation Generation
- Citations are generated automatically by the Report Agent based on evidence used to support each claim.
- The system maintains a mapping from claims to evidence IDs during analysis and synthesis.
- Evidence IDs are traceable to source metadata.

## Citation Format (V1 Placeholder)
- Citations will be numeric in brackets, e.g., `[1]`, `[2]`.
- A bibliography or references section will list full source details.
- Example:
  ```
  The Eiffel Tower is 330 meters tall [1].
  References:
  [1] Gustave Eiffel, "Eiffel Tower Specifications," 1889, https://example.com/eiffel-tower-specs.
  ```

## Source Provenance
Each citation must include at minimum:
- **Identifier**: A unique reference within the report (e.g., number).
- **Source Locator**: URL, DOI, file path, or other means to access the source.
- **Access Timestamp**: When the evidence was collected (important for dynamic web content).
- **Optional**: Author, title, publication name, etc.

## Handling Conflicting Evidence
- If evidence conflicts, the Report Agent should note the discrepancy and cite multiple sources.
- The Evaluation Agent will flag conflicting evidence for user review in later phases.

## Prohibited Practices
- **Fabricating citations** or evidence to support a claim.
- **Citing sources that were not actually consulted** during the research process.
- **Omitting citations** for factual claims that require them.
- **Using citations** to misrepresent the strength or consensus of evidence.
