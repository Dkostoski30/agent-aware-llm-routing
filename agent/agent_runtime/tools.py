"""The agent's tools and the check every tool call goes through before it runs (D10)."""

import json
from collections.abc import Sequence
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

CALCULATOR = "calculator"
FINAL_ANSWER = "final_answer"

PARAMETERS: dict[str, dict[str, Any]] = {
    CALCULATOR: {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
            "a": {"type": "number"},
            "b": {"type": "number"},
        },
        "required": ["op", "a", "b"],
        "additionalProperties": False,
    },
    FINAL_ANSWER: {
        "type": "object",
        "properties": {"answer": {"type": "string", "minLength": 1}},
        "required": ["answer"],
        "additionalProperties": False,
    },
}

DESCRIPTIONS = {
    CALCULATOR: "Applies one arithmetic operation to two numbers: a op b.",
    FINAL_ANSWER: "Ends the task with the answer for the user.",
}


class InvalidCallError(Exception):
    """A tool call the runtime refuses to run; the message goes back to the model."""


def tool_definitions(tool_names: Sequence[str]) -> list[dict[str, Any]]:
    """The tools in the OpenAI format of /v1/chat/completions."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": DESCRIPTIONS[name],
                "parameters": PARAMETERS[name],
            },
        }
        for name in tool_names
    ]


def check_call(name: str, raw_arguments: str, tool_names: Sequence[str]) -> dict[str, Any]:
    """Returns the parsed arguments of a call to one of tool_names, or raises InvalidCallError."""
    if name not in tool_names:
        raise InvalidCallError(f"unknown tool {name!r}; allowed tools: {', '.join(tool_names)}")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise InvalidCallError(f"{name}: arguments are not valid JSON: {error}") from error
    # For an enum, jsonschema's message lists the allowed values: what the model needs to fix it.
    violation = best_match(Draft202012Validator(PARAMETERS[name]).iter_errors(arguments))
    if violation is not None:
        raise InvalidCallError(f"{name}: {violation.message}")
    checked: dict[str, Any] = arguments
    return checked


def calculator(op: str, a: float, b: float) -> dict[str, float | str]:
    """op is one of the enum values (check_call). Division by zero is an answer, not a raise."""
    if op == "add":
        return {"result": a + b}
    if op == "subtract":
        return {"result": a - b}
    if op == "multiply":
        return {"result": a * b}
    if b == 0:
        return {"error": "division by zero"}
    return {"result": a / b}
