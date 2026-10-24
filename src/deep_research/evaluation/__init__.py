"""Offline evaluation suite: faithfulness / relevance / coverage scorecard for research reports."""

from deep_research.evaluation.offline_eval import EvalCase, Scorecard, run_suite, score_report, score_report_async

__all__ = ["EvalCase", "Scorecard", "run_suite", "score_report", "score_report_async"]
