"""make failover-live (P4.4): the experiment file, the agent's log lines counted, and the verdict
on each run and each case, from known runs. The live part is what the check's done-when proves."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from failover_live import (
    CaseVerdict,
    LiveError,
    RunOutcome,
    case_lines,
    check_profile,
    load_live,
    log_count,
    outcome_of,
    run_problems,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STEP_1: dict[str, Any] = {"step": 1, "content": None, "tool_calls": [], "error": None}
STEP_2: dict[str, Any] = {"step": 2, "content": "56", "tool_calls": [], "error": None}


def experiment_file(tmp_path: Path, **changes: Any) -> Path:
    spec: dict[str, Any] = {
        "profile": "real",
        "routing": {"plugin": True, "affinity": True},
        "go_no_go": str(REPO_ROOT / "experiments" / "real-go-no-go.yaml"),
        "seeds": [1],
        "kill_after_seconds": 10,
        "wait_for_calls_seconds": 30,
        "cases": ["vllm", "agent"],
    }
    spec.update(changes)
    path = tmp_path / "failover-live.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_the_experiment_file_is_read_whole(tmp_path: Path) -> None:
    experiment = load_live(experiment_file(tmp_path))
    assert experiment.name == "failover-live"
    assert experiment.profile == "real"
    assert experiment.routing == {"plugin": True, "affinity": True}
    assert experiment.questions.file["id"] == "sales"
    assert [q.id for q in experiment.questions.questions]
    assert experiment.seeds == [1]
    assert (experiment.kill_after_seconds, experiment.wait_for_calls_seconds) == (10, 30)
    assert experiment.cases == ["vllm", "agent"]
    assert len(experiment.hash) == 12


@pytest.mark.parametrize(
    "changes",
    [
        {"cases": []},
        {"cases": ["vllm", "vllm"]},
        {"cases": ["redis"]},
        {"kill_after_seconds": 0},
        {"wait_for_calls_seconds": -1},
        {"seeds": []},
        {"routing": {"plugin": True, "affinity": False}},
        {"profile": "demo"},
    ],
)
def test_an_experiment_file_that_could_not_show_a_failover_is_refused(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(LiveError):
        load_live(experiment_file(tmp_path, **changes))


def line(message: str, run: str) -> str:
    return json.dumps({"message": message, "run-id": run, "level": "WARNING"})


def test_log_count_counts_the_lines_of_one_message_for_the_given_runs() -> None:
    log = "\n".join(
        [
            line("llm call retried", "run-a"),
            "INFO:     10.0.0.1 - GET /runs/run-a",
            line("llm call retried", "run-a"),
            line("llm call retried", "run-elsewhere"),
            line("llm call", "run-a"),
            "{not json",
        ]
    )
    assert log_count(log, "llm call retried", {"run-a", "run-b"}) == 2


def outcome(**changes: Any) -> RunOutcome:
    fields: dict[str, Any] = {
        "run_id": "run-a",
        "question": "summary",
        "seed": 1,
        "status": "done",
        "error": None,
        "answer": "56",
        "correct": True,
        "steps_before": [STEP_1],
        "steps_after": [STEP_1, STEP_2],
        "call_steps": [1, 2],
    }
    fields.update(changes)
    return RunOutcome(**fields)


def test_a_run_that_went_on_from_its_saved_steps_has_no_problem() -> None:
    assert run_problems(outcome()) == []


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"status": "failed", "error": "LLM call failed: ConnectError"}, "failed"),
        ({"steps_after": [STEP_2, STEP_2]}, "changed"),
        ({"steps_after": []}, "changed"),
        ({"call_steps": [1, 2, 2]}, "each once"),
        ({"call_steps": [2]}, "each once"),
    ],
)
def test_a_run_that_did_not_go_on_from_its_saved_steps_is_named(
    changes: dict[str, Any], says: str
) -> None:
    (problem,) = run_problems(outcome(**changes))
    assert problem.startswith("run-a")
    assert says in problem


def test_a_wrong_answer_is_counted_not_a_problem() -> None:
    assert run_problems(outcome(correct=False)) == []


def test_an_outcome_is_read_from_the_runs_api() -> None:
    run = {"status": "done", "error": None, "answer": "56", "steps": [STEP_1, STEP_2]}
    calls = {"run_id": "run-a", "calls": [{"step": 1}, {"step": 2}]}
    result = outcome_of("run-a", "summary", 1, [STEP_1], run, calls, correct=True)
    assert result == outcome()


def test_a_case_passes_with_its_signal_and_no_problem() -> None:
    verdict = CaseVerdict("vllm", signals=1, outcomes=[outcome()])
    assert verdict.passed and not verdict.void
    assert case_lines(verdict)[-1] == "vllm: passed, 1 of 1 runs done, 1 correct"


def test_a_case_without_its_signal_is_void() -> None:
    verdict = CaseVerdict("agent", signals=0, outcomes=[outcome()])
    assert verdict.void and not verdict.passed
    assert case_lines(verdict)[-1] == "agent: void, no run resumed"


def test_a_case_with_a_problem_fails_and_names_it() -> None:
    verdict = CaseVerdict("vllm", signals=2, outcomes=[outcome(call_steps=[1, 1, 2])])
    assert not verdict.passed
    lines = case_lines(verdict)
    assert lines[-1] == "vllm: failed, 1 of 1 runs done, 1 correct"
    assert any("each once" in text for text in lines)


def test_the_cluster_must_run_the_files_profile() -> None:
    """make up (real) puts the profile's text in the agent's ConfigMap."""
    check_profile("real", "a: 1\n", "a: 1\n")
    with pytest.raises(LiveError, match="make up PROFILE=real"):
        check_profile("real", "a: 2\n", "a: 1\n")
