"""The agent's tools and the check every tool call goes through before it runs (D10)."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from agent_runtime import csv_tools
from agent_runtime.config import ToolChoice
from agent_runtime.csv_tools import COMPARE, CSV_SUMMARY, GROUP_BY, Table, ToolError

CALCULATOR = "calculator"
FINAL_ANSWER = "final_answer"

# The tools whose schemas are the same in every run; the CSV tools' schemas are built per run.
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
    **csv_tools.DESCRIPTIONS,
}


class InvalidCallError(Exception):
    """A tool call the runtime refuses to run; the message goes back to the model."""


def offered_tools(tool_choice: ToolChoice, tables: Sequence[Table]) -> dict[str, dict[str, Any]]:
    """The tools a run offers, name -> JSON schema of the arguments, in the order the model sees
    them. Every run gets the calculator, and a run with files the CSV tools, until P4.8 adds the
    scenario step (D13).
    """
    tools = {CALCULATOR: PARAMETERS[CALCULATOR]}
    # Without files the file enum would be empty: the CSV tools could never be called, and in
    # required mode vLLM would have to build a grammar from an enum with no values (D10).
    if tables:
        tools.update(csv_tools.parameters(tables))
    # final_answer ends the run in required mode; in auto mode a plain text answer does (D10).
    if tool_choice == "required":
        tools[FINAL_ANSWER] = PARAMETERS[FINAL_ANSWER]
    return tools


def tool_definitions(tools: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The tools in the OpenAI format of /v1/chat/completions."""
    return [
        {
            "type": "function",
            "function": {"name": name, "description": DESCRIPTIONS[name], "parameters": schema},
        }
        for name, schema in tools.items()
    ]


def check_call(
    name: str, raw_arguments: str, tools: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Returns the parsed arguments of a call to one of tools, or raises InvalidCallError."""
    if name not in tools:
        raise InvalidCallError(f"unknown tool {name!r}; allowed tools: {', '.join(tools)}")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise InvalidCallError(f"{name}: arguments are not valid JSON: {error}") from error
    # For an enum, jsonschema's message lists the allowed values: what the model needs to fix it.
    violation = best_match(Draft202012Validator(tools[name]).iter_errors(arguments))
    if violation is not None:
        raise InvalidCallError(f"{name}: {violation.message}")
    checked: dict[str, Any] = arguments
    return checked


def run_tool(
    name: str,
    arguments: dict[str, Any],
    tables: Mapping[str, Table],
    max_result_rows: int,
    result_decimals: int,
) -> dict[str, Any]:
    """Runs a call that passed check_call, to any tool but final_answer. The file enum guarantees
    the file exists; a call the file's data cannot answer comes back as a tool error. The CSV
    tools' numbers are rounded to result_decimals; the calculator's are not, since its result
    feeds the next operation."""
    if name == CALCULATOR:
        return calculator(**arguments)
    table = tables[arguments["file"]]
    try:
        if name == CSV_SUMMARY:
            return csv_tools.csv_summary(table, max_result_rows, result_decimals)
        if name == GROUP_BY:
            return csv_tools.group_by(
                table,
                arguments["by"],
                arguments["value"],
                arguments["agg"],
                max_result_rows,
                result_decimals,
            )
        if name == COMPARE:
            return csv_tools.compare(
                table,
                arguments["by"],
                arguments["value"],
                arguments["agg"],
                arguments["left"],
                arguments["right"],
                max_result_rows,
                result_decimals,
            )
    except ToolError as error:
        return {"error": str(error)}
    raise ValueError(f"{name!r} is not a tool run_tool runs")


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
