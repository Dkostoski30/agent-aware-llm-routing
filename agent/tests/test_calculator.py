"""Calculator tool and the checks every tool call goes through (P1.3a-1, D10)."""

import json

import pytest
from agent_runtime.tools import (
    CALCULATOR,
    FINAL_ANSWER,
    PARAMETERS,
    InvalidCallError,
    calculator,
    check_call,
)

BOTH_TOOLS = {name: PARAMETERS[name] for name in (CALCULATOR, FINAL_ANSWER)}


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("add", 7, 8, {"result": 15}),
        ("subtract", 7, 8, {"result": -1}),
        ("multiply", 7, 8, {"result": 56}),
        ("divide", 7, 2, {"result": 3.5}),
        ("multiply", 0.5, 4, {"result": 2.0}),
        ("divide", 1, 0, {"error": "division by zero"}),
    ],
)
def test_calculator(op: str, a: float, b: float, expected: dict[str, object]) -> None:
    assert calculator(op, a, b) == expected


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (CALCULATOR, {"op": "add", "a": 1, "b": 2.5}, {"op": "add", "a": 1, "b": 2.5}),
        (FINAL_ANSWER, {"answer": "59"}, {"answer": "59"}),
    ],
)
def test_valid_call_returns_its_arguments(
    name: str, arguments: dict[str, object], expected: dict[str, object]
) -> None:
    assert check_call(name, json.dumps(arguments), BOTH_TOOLS) == expected


@pytest.mark.parametrize(
    ("name", "raw_arguments", "tools", "error_parts"),
    [
        (
            CALCULATOR,
            '{"op": "pow", "a": 2, "b": 3}',
            BOTH_TOOLS,
            ["'pow'", "'add'", "'subtract'", "'multiply'", "'divide'"],
        ),
        (CALCULATOR, '{"op": "add", "a": 2}', BOTH_TOOLS, ["'b' is a required property"]),
        (CALCULATOR, '{"op": "add", "a": "seven", "b": 3}', BOTH_TOOLS, ["'seven'", "'number'"]),
        (CALCULATOR, '{"op": "add", "a": 2, "b": 3, "c": 4}', BOTH_TOOLS, ["'c'"]),
        (CALCULATOR, "[2, 3]", BOTH_TOOLS, ["'object'"]),
        (CALCULATOR, '{"op": "add", "a": 2,', BOTH_TOOLS, ["not valid JSON"]),
        (FINAL_ANSWER, '{"answer": ""}', BOTH_TOOLS, ["non-empty"]),
        ("power", "{}", BOTH_TOOLS, ["unknown tool 'power'", "calculator, final_answer"]),
        # In auto mode final_answer is not offered, so calling it is calling an unknown tool (D10).
        (
            FINAL_ANSWER,
            '{"answer": "59"}',
            {CALCULATOR: PARAMETERS[CALCULATOR]},
            ["unknown tool 'final_answer'"],
        ),
    ],
)
def test_invalid_call_names_what_is_wrong(
    name: str, raw_arguments: str, tools: dict[str, dict[str, object]], error_parts: list[str]
) -> None:
    with pytest.raises(InvalidCallError) as raised:
        check_call(name, raw_arguments, tools)
    for part in error_parts:
        assert part in str(raised.value)
