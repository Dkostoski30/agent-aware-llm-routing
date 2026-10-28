"""Contract tests for the versioned schemas (D12).

For each schema in SCHEMA_NAMES, its <name>.valid.json example must be accepted and its
<name>.invalid.json example rejected, and each rule is checked by changing one field of the
valid example.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SCHEMAS_DIR = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = SCHEMAS_DIR / "examples"
SCHEMA_NAMES = ["headers.v1", "trace.v1", "run-request.v1", "run-events.v1"]
REMOVE = object()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def validator_for(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_json(SCHEMAS_DIR / f"{name}.json"))


def valid_example(name: str) -> dict[str, Any]:
    example: dict[str, Any] = load_json(EXAMPLES_DIR / f"{name}.valid.json")
    return example


def with_change(example: dict[str, Any], field: str, value: object) -> dict[str, Any]:
    changed = copy.deepcopy(example)
    if value is REMOVE:
        del changed[field]
    else:
        changed[field] = value
    return changed


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(load_json(SCHEMAS_DIR / f"{name}.json"))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_every_schema_has_a_valid_and_an_invalid_example(name: str) -> None:
    assert (EXAMPLES_DIR / f"{name}.valid.json").is_file()
    assert (EXAMPLES_DIR / f"{name}.invalid.json").is_file()


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_valid_example_is_accepted(name: str) -> None:
    assert validator_for(name).is_valid(valid_example(name))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_invalid_example_is_rejected(name: str) -> None:
    assert not validator_for(name).is_valid(load_json(EXAMPLES_DIR / f"{name}.invalid.json"))


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("x-parallel-group", REMOVE, True),
        ("x-step", "1", True),
        ("x-expected-steps", "12", True),
        ("x-run-id", REMOVE, False),
        ("x-step", REMOVE, False),
        ("x-expected-steps", REMOVE, False),
        ("x-run-id", "", False),
        ("x-parallel-group", "", False),
        ("x-step", "0", False),
        ("x-step", "-1", False),
        ("x-step", "01", False),
        ("x-step", "2.5", False),
        ("x-step", "two", False),
        ("x-step", 2, False),
        ("x-expected-steps", "0", False),
        ("x-max-steps", "20", False),
    ],
)
def test_header_rules(field: str, value: object, accepted: bool) -> None:
    headers = with_change(valid_example("headers.v1"), field, value)
    assert validator_for("headers.v1").is_valid(headers) is accepted


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("parallel_group", REMOVE, True),
        ("ts_start", REMOVE, True),
        ("ttft_ms", REMOVE, True),
        ("duration_ms", REMOVE, True),
        ("pod", REMOVE, True),
        ("gap_before_ms", 0, True),
        ("completion_tokens", 0, True),
        ("schema_version", REMOVE, False),
        ("run_id", REMOVE, False),
        ("task_type", REMOVE, False),
        ("step", REMOVE, False),
        ("expected_steps", REMOVE, False),
        ("gap_before_ms", REMOVE, False),
        ("prompt_tokens", REMOVE, False),
        ("completion_tokens", REMOVE, False),
        ("schema_version", 2, False),
        ("schema_version", "1", False),
        ("run_id", "", False),
        ("task_type", "", False),
        ("parallel_group", "", False),
        ("pod", "", False),
        ("step", 0, False),
        ("step", 1.5, False),
        ("step", "2", False),
        ("expected_steps", 0, False),
        ("gap_before_ms", -1, False),
        ("prompt_tokens", -1, False),
        ("completion_tokens", -1, False),
        ("ts_start", -1, False),
        ("ts_start", "2026-10-25T12:00:00Z", False),
        ("ttft_ms", -1, False),
        ("duration_ms", -1, False),
        ("latency_ms", 10, False),
    ],
)
def test_trace_rules(field: str, value: object, accepted: bool) -> None:
    line = with_change(valid_example("trace.v1"), field, value)
    assert validator_for("trace.v1").is_valid(line) is accepted


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("seed", REMOVE, True),
        ("seed", 0, True),
        ("files", REMOVE, True),
        ("files", [], True),
        ("task", REMOVE, False),
        ("task", "", False),
        ("task", 7, False),
        ("seed", "7", False),
        ("seed", 1.5, False),
        ("max_steps", 5, False),
        ("files", {}, False),
        ("files", "sales.csv", False),
    ],
)
def test_run_request_rules(field: str, value: object, accepted: bool) -> None:
    request = with_change(valid_example("run-request.v1"), field, value)
    assert validator_for("run-request.v1").is_valid(request) is accepted


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("id", "q3-sales-2025", True),
        ("id", "a" * 64, True),
        ("content", "", True),
        ("id", REMOVE, False),
        ("name", REMOVE, False),
        ("content", REMOVE, False),
        ("id", "", False),
        ("id", "Sales", False),
        ("id", "sales.csv", False),
        ("id", "sales_q3", False),
        ("id", "-sales", False),
        ("id", "sales-", False),
        ("id", "a--b", False),
        ("id", "a" * 65, False),
        ("name", "", False),
        ("content", 7, False),
        ("size", 10, False),
    ],
)
def test_run_request_file_rules(field: str, value: object, accepted: bool) -> None:
    request = valid_example("run-request.v1")
    request["files"] = [with_change(request["files"][0], field, value)]
    assert validator_for("run-request.v1").is_valid(request) is accepted


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("content", "56", True),
        ("tool_calls", [], True),
        ("error", "Error: answer with a call to one of the tools: calculator", True),
        ("run_id", REMOVE, False),
        ("step", REMOVE, False),
        ("expected_steps", REMOVE, False),
        ("content", REMOVE, False),
        ("tool_calls", REMOVE, False),
        ("error", REMOVE, False),
        ("run_id", "", False),
        ("step", 0, False),
        ("step", "1", False),
        ("expected_steps", 0, False),
        ("tool_calls", {}, False),
        ("pod", "vllm-0", False),
    ],
)
def test_step_event_rules(field: str, value: object, accepted: bool) -> None:
    event = with_change(valid_example("run-events.v1"), field, value)
    assert validator_for("run-events.v1").is_valid(event) is accepted


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("arguments", '{"op": "add"', True),
        ("result", None, True),
        ("result", {"error": "op must be one of add, subtract, multiply, divide"}, True),
        ("name", REMOVE, False),
        ("arguments", REMOVE, False),
        ("result", REMOVE, False),
        ("name", "", False),
        ("result", 56, False),
        ("id", "call-1", False),
    ],
)
def test_step_event_tool_call_rules(field: str, value: object, accepted: bool) -> None:
    event = valid_example("run-events.v1")
    event["tool_calls"] = [with_change(event["tool_calls"][0], field, value)]
    assert validator_for("run-events.v1").is_valid(event) is accepted


RUN_EVENT = {
    "run_id": "run-0a1b2c3d4e5f",
    "task": "What is 7 times 8?",
    "expected_steps": 2,
    "created_ms": 1793102400123,
}
END_EVENT = {
    "run_id": "run-0a1b2c3d4e5f",
    "status": "done",
    "answer": "56",
    "error": None,
    "steps": 2,
    "invalid_calls": 0,
    "elapsed_ms": 1840,
}


@pytest.mark.parametrize(
    ("event", "field", "value", "accepted"),
    [
        (RUN_EVENT, "task", "x", True),
        (RUN_EVENT, "task", "", False),
        (RUN_EVENT, "created_ms", REMOVE, False),
        (RUN_EVENT, "created_ms", -1, False),
        (RUN_EVENT, "expected_steps", 0, False),
        (END_EVENT, "status", "failed", True),
        (END_EVENT, "answer", None, True),
        (END_EVENT, "status", "running", False),
        (END_EVENT, "steps", 0, False),
        (END_EVENT, "invalid_calls", -1, False),
        (END_EVENT, "elapsed_ms", REMOVE, False),
        (END_EVENT, "elapsed_ms", 1.5, False),
        (END_EVENT, "task", "What is 7 times 8?", False),
    ],
)
def test_run_and_end_event_rules(
    event: dict[str, Any], field: str, value: object, accepted: bool
) -> None:
    changed = with_change(event, field, value)
    assert validator_for("run-events.v1").is_valid(changed) is accepted
