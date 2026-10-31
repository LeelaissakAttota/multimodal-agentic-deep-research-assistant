"""A/B experiments: config validation, deterministic assignment, recording and evaluation comparison."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from deep_research.control.runtime import RunStatus
from deep_research.core.agents.llm_agents import PLAN_STYLES, LlmPlanningAgent
from deep_research.core.config import Settings
from deep_research.evaluation.__main__ import main as eval_main
from deep_research.evaluation.offline_eval import EvalCase, case_from_run, compare_variants, run_suite, variant_table
from deep_research.scale import backend_status
from deep_research.scale.experiments import active_experiment, apply_variant, load_experiment, parse_experiment
from deep_research.scale.jobs import run_research_job
from tests.unit.test_agentic_agents import ScriptedLLM, request, state_for
from tests.unit.test_scale_queue import _control, _wait

CONFIG = {"name": "planner-style", "variants": [
    {"name": "control", "weight": 1},
    {"name": "broad", "weight": 1, "overrides": {"agent_plan_style": "broad", "agent_max_plan_tasks": "5"}}]}


def _row() -> dict[str, Any]:
    return next(r for r in backend_status() if r["name"] == "A/B experiments")


def test_off_by_default() -> None:
    assert active_experiment() is None
    settings = Settings(_env_file=None)
    assert apply_variant(settings, "r1", None) == (settings, None)
    assert settings.agent_plan_style == "default" and _row()["state"] == "off"


def test_assignment_is_deterministic_and_weighted() -> None:
    exp = parse_experiment(CONFIG)
    assert all(exp.assign(f"run-{i}") == exp.assign(f"run-{i}") for i in range(50))
    counts = Counter(exp.assign(f"run-{i}").name for i in range(2000))
    assert 850 < counts["control"] < 1150
    skewed = parse_experiment({"name": "s", "variants": [{"name": "a", "weight": 9}, {"name": "b", "weight": 1}]})
    assert Counter(skewed.assign(str(i)).name for i in range(2000))["a"] > 1600


@pytest.mark.parametrize("bad, message", [
    ([], "JSON object"), ({"variants": []}, "name"), ({"name": "x", "variants": [{"name": "a"}]}, "two variants"),
    ({"name": "x", "variants": [{"name": "a"}, {"name": "a"}]}, "unique"),
    ({"name": "x", "variants": [{"name": "a", "weight": 0}, {"name": "b"}]}, "weight"),
    ({"name": "x", "variants": [{"name": "a"}, {"name": "b", "overrides": {"openrouter_api_key": "sk-x"}}]}, "not allowed"),
    ({"name": "x", "variants": [{"name": "a"}, {"name": "b", "overrides": {"agent_max_plan_tasks": 99}}]}, "bad value"),
])
def test_invalid_config(bad: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_experiment(bad)


def test_load_from_file_and_apply(tmp_path: Path) -> None:
    path = tmp_path / "ab.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    exp = load_experiment(str(path))
    run_id = next(f"r{i}" for i in range(100) if exp.assign(f"r{i}").name == "broad")
    settings, record = apply_variant(Settings(_env_file=None), run_id, exp)
    assert settings.agent_plan_style == "broad" and settings.agent_max_plan_tasks == 5
    assert record == {"experiment": "planner-style", "variant": "broad",
                      "overrides": {"agent_plan_style": "broad", "agent_max_plan_tasks": 5}}
    with pytest.raises(ValueError, match="not found"):
        load_experiment(str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="valid JSON"):
        load_experiment("{nope")


def test_planner_prompt_variant() -> None:
    req = request()
    state = state_for(req)
    base = LlmPlanningAgent(ScriptedLLM())._prompt(req, state, [])
    broad = LlmPlanningAgent(ScriptedLLM(), style="broad")._prompt(req, state, [])
    assert broad == base + PLAN_STYLES["broad"] and PLAN_STYLES["default"] == ""
    assert LlmPlanningAgent(ScriptedLLM(), style="weird").style == "default"


def test_status_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_AB_CONFIG", json.dumps(CONFIG))
    assert _row()["state"] == "configured" and "control (1), broad (1)" in _row()["detail"]
    monkeypatch.setenv("MDRA_AB_CONFIG", '{"name": "x"}')
    assert _row()["state"] == "invalid"


def test_control_run_records_variant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_AB_CONFIG", json.dumps(CONFIG))
    run = _control(tmp_path).manager.create({"objective": "Benefits of green tea", "mode": "offline"})
    _wait(run)
    assert run.status == RunStatus.COMPLETED
    expected = parse_experiment(CONFIG).assign(run.run_id).name
    assert run.result["experiment"]["variant"] == expected
    assert any(f"variant {expected}" in line["message"] for line in run.logs)
    case = case_from_run({"run_id": run.run_id, "params": run.params, "result": run.result})
    assert case is not None and case.variant == expected


def test_queue_job_uses_same_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_AB_CONFIG", json.dumps(CONFIG))
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", "nonexistent.env")
    result = run_research_job({"objective": "Benefits of green tea", "mode": "offline", "run_id": "abc"})
    assert result["experiment"]["variant"] == parse_experiment(CONFIG).assign("abc").name
    assert "experiment" not in run_research_job({"objective": "Benefits of green tea", "mode": "offline"})


def test_compare_variants_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from deep_research.evaluation.cases import CASES

    cases = [EvalCase(**{**c.__dict__, "variant": "a" if i % 2 else "b"}) for i, c in enumerate(CASES)]
    comparison = compare_variants(run_suite(cases))
    assert set(comparison) == {"a", "b"} and sum(m["runs"] for m in comparison.values()) == len(CASES)
    assert "variant" in variant_table(comparison)
    assert compare_variants(run_suite(CASES[:1])).keys() == {"(none)"}
    for i, variant in enumerate(("a", "b")):
        c = CASES[i]
        run = {"run_id": f"r{i}", "params": {"objective": c.objective}, "result": {
            "experiment": {"experiment": "e", "variant": variant},
            "report": {"content": c.report, "citations": [{"number": n, "excerpt": t} for n, t in c.excerpts.items()]}}}
        (tmp_path / f"r{i}.json").write_text(json.dumps(run), encoding="utf-8")
    assert eval_main([str(tmp_path / "r0.json"), str(tmp_path / "r1.json"), "--by-variant", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"a", "b"} and out["a"]["runs"] == 1
