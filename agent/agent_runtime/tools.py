"""The agent's tools and the check every tool call goes through before it runs (D10)."""

import ast
import json
import operator
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from agent_runtime import csv_tools
from agent_runtime.config import ToolChoice
from agent_runtime.csv_tools import COMPARE, CSV_SUMMARY, GROUP_BY, Table, ToolError

CALCULATOR = "calculator"
FINAL_ANSWER = "final_answer"

# Longer than the arithmetic of any question a user types, and short enough that the deepest tree
# it can parse to (200 nested unary minuses, ~200 levels) stays far below Python's recursion limit
# of 1000; a 100,000-term sum raises RecursionError while Python parses it (P1.6d).
MAX_EXPRESSION_LENGTH = 200
# x, X and the multiplication sign multiply, the division sign divides: the model copies them from
# the question.
_SIGNS = str.maketrans({"x": "*", "X": "*", "\u00d7": "*", "\u00f7": "/"})
_OPERATORS: dict[type[ast.operator], Callable[[Fraction, Fraction], Fraction]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

# The tools whose schemas are the same in every run; the CSV tools' schemas are built per run.
PARAMETERS: dict[str, dict[str, Any]] = {
    CALCULATOR: {
        "type": "object",
        "properties": {
            # The whole arithmetic of the question, so the model never chains dependent calls
            # (P1.6d). Only these characters; the evaluator accepts numbers and + - * / only.
            "expression": {
                "type": "string",
                "pattern": "^[0-9.+\\-*/()xX\u00d7\u00f7 ]+$",
                "maxLength": MAX_EXPRESSION_LENGTH,
            },
        },
        "required": ["expression"],
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
    CALCULATOR: (
        "Evaluates one arithmetic expression with + - * / and parentheses, "
        "for example 7*8+3 or (2+3)*4."
    ),
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
    tools' numbers are rounded to result_decimals; the calculator's are not: its arithmetic is
    exact."""
    if name == CALCULATOR:
        return calculator(arguments["expression"])
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


class _NotArithmeticError(Exception):
    """A parsed expression with something in it other than numbers and + - * /."""


def calculator(expression: str) -> dict[str, float | str]:
    """Evaluates an expression that passed check_call; one without a value comes back as an
    error, which the model reads like any tool result. The schema's pattern and maxLength keep out
    names and anything long enough to exhaust the parser; ast is walked, never eval'd.

    The arithmetic is exact (fractions) and every number is read as written: a whole number comes
    back as an exact int, so the model reads 8, not 8.0, and every digit of 123456789*987654321;
    any other value as the nearest float, so 0.1*3 is 0.3. Within maxLength no value comes near a
    float's limits.
    """
    try:
        # strip(): Python reads a leading space as an indent and refuses it.
        source = expression.translate(_SIGNS).strip()
        value = _evaluate(ast.parse(source, mode="eval").body, source)
    except (SyntaxError, _NotArithmeticError):
        return {"error": f"not an arithmetic expression: {expression}"}
    except ZeroDivisionError:
        return {"error": "division by zero"}
    return {"result": int(value) if value.denominator == 1 else float(value)}


def _evaluate(node: ast.expr, source: str) -> Fraction:
    """The exact value of a parsed expression. A number is read from its text in source, not from
    the float Python parsed it to, so "0.1" is 1/10 and a decimal keeps digits past a float's 17."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        # ast positions count UTF-8 bytes; they index source because it is ASCII: the pattern's
        # only other characters, the multiplication and division signs, are translated to * and /.
        return Fraction(source[node.col_offset : node.end_col_offset])
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        value = _evaluate(node.operand, source)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](
            _evaluate(node.left, source), _evaluate(node.right, source)
        )
    raise _NotArithmeticError(ast.dump(node))
