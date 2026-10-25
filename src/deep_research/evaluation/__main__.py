"""``python -m deep_research.evaluation`` / ``deep-research-eval``: print the offline scorecard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deep_research.evaluation.offline_eval import case_from_run, compare_variants, run_suite, scorecard_table, variant_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deep-research-eval", description=__doc__)
    parser.add_argument("runs", nargs="*", type=Path, help="saved run JSON files to score (default: bundled cases)")
    parser.add_argument("--json", action="store_true", help="print the scorecard as JSON")
    parser.add_argument("--min-overall", type=float, default=None, help="exit 1 if the mean overall score is below")
    parser.add_argument("--by-variant", action="store_true", help="compare mean scores per A/B variant")
    args = parser.parse_args(argv)
    cases = None
    if args.runs:
        cases = []
        for path in args.runs:
            case = case_from_run(json.loads(path.read_text(encoding="utf-8")), name=path.stem)
            if case is None:
                print(f"skip {path.name}: no report", file=sys.stderr)
            else:
                cases.append(case)
    cards = run_suite(cases)
    if args.by_variant:
        comparison = compare_variants(cards)
        print(json.dumps(comparison, indent=2) if args.json else variant_table(comparison))
    elif args.json:
        print(json.dumps([c.as_dict() for c in cards], indent=2))
    else:
        print(scorecard_table(cards))
    mean = sum(c.overall for c in cards) / len(cards) if cards else 0.0
    return 1 if args.min_overall is not None and mean < args.min_overall else 0


if __name__ == "__main__":
    raise SystemExit(main())
