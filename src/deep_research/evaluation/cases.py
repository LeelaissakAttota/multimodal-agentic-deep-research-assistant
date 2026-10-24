"""Bundled offline evaluation cases (hand-written, no network). Each exercises one failure mode."""

from __future__ import annotations

from deep_research.evaluation.offline_eval import EvalCase

_SMR = {
    1: "Small modular reactors produce up to 300 MW of electricity per unit. Factory fabrication of modules "
       "shortens construction schedules and lowers upfront capital cost.",
    2: "Critics note small modular reactors still generate radioactive waste and face high first-of-a-kind costs. "
       "Regulatory licensing of new reactor designs takes several years.",
}

CASES: list[EvalCase] = [
    EvalCase(
        name="grounded_complete",
        objective="What are the benefits and risks of small modular reactors?",
        report=("## Benefits\n\nSmall modular reactors produce up to 300 MW of electricity per unit [1]. "
                "Factory fabrication lowers upfront capital cost of small modular reactors [1].\n\n"
                "## Risks\n\nSmall modular reactors still generate radioactive waste [2]. "
                "Licensing of new reactor designs takes several years [2].\n\n## Sources\n\n[1] A - https://a.org"),
        excerpts=_SMR,
        expected_points=["small modular reactors capital cost", "radioactive waste", "reactor licensing years"],
        expect="high on all three metrics",
    ),
    EvalCase(
        name="hallucinated_figures",
        objective="What are the benefits and risks of small modular reactors?",
        report=("## Benefits\n\nSmall modular reactors produce up to 900 MW of electricity per unit [1]. "
                "Small modular reactors eliminate radioactive waste entirely [2].\n\n"
                "## Risks\n\nSmall modular reactors have no regulatory hurdles anywhere in the world [2]."),
        excerpts=_SMR,
        expected_points=["small modular reactors capital cost", "radioactive waste", "reactor licensing years"],
        expect="low faithfulness: wrong figure and unsupported claims",
    ),
    EvalCase(
        name="off_topic",
        objective="What are the benefits and risks of small modular reactors?",
        report=("## Overview\n\nOffshore wind farms reduce coastal fishing yields [1]. "
                "Solar panels degrade roughly half a percent per year [2]."),
        excerpts={1: "Offshore wind farms can reduce coastal fishing yields.",
                  2: "Solar panels degrade roughly half a percent per year."},
        expected_points=["small modular reactors capital cost", "radioactive waste"],
        expect="faithful but irrelevant and no coverage",
    ),
    EvalCase(
        name="partial_coverage",
        objective="What are the benefits and risks of small modular reactors?",
        report="## Benefits\n\nFactory fabrication of small modular reactor modules shortens construction schedules [1].",
        excerpts=_SMR,
        expected_points=["small modular reactors capital cost", "radioactive waste", "reactor licensing years"],
        expect="faithful and relevant but misses the risks",
    ),
]
