"""Calculator tool and the checks every tool call goes through (P1.3a-1, P1.6d, D10)."""

import json

import pytest
from agent_runtime.tools import (
    CALCULATOR,
    FINAL_ANSWER,
    MAX_EXPRESSION_LENGTH,
    PARAMETERS,
    InvalidCallError,
    calculator,
    check_call,
)

BOTH_TOOLS = {name: PARAMETERS[name] for name in (CALCULATOR, FINAL_ANSWER)}
NOT_AN_EXPRESSION = "not an arithmetic expression"


@pytest.mark.parametrize(
    ("expression", "result"),
    [
        ("7*8", 56),
        ("7*8+3", 59),
        ("1+2*3", 7),
        ("(2+3)*4", 20),
        ("10-3-2", 5),
        ("100/5/2", 10),
        ("7/2", 3.5),
        ("-3+5", 2),
        ("2*-3", -6),
        ("+4", 4),
        ("0.5*4", 2),
        # Exact arithmetic: decimals are read as written, so no float noise reaches the model.
        ("0.1+0.2", 0.3),
        ("0.1*3", 0.3),
        ("1/3", 1 / 3),
        # Whole numbers stay exact past a float's 53 bits.
        ("123456789*987654321", 121932631112635269),
        ("9007199254740993", 9007199254740993),
        ("1" * MAX_EXPRESSION_LENGTH, int("1" * MAX_EXPRESSION_LENGTH)),
        ("1.5", 1.5),
        ("5.*2", 10),
        (".5*4", 2),
        # A decimal is read as written, past a float's 17 significant digits: as a float,
        # 123456789012345678.5 is 123456789012345680.
        ("123456789012345678.5*2", 246913578024691357),
        ("0.1234567890123456789*10000000000000000000", 1234567890123456789),
        (" ( ( 1 + 2 ) ) ", 3),
        # The model copies the question's signs: x, X and the multiplication sign multiply, the
        # division sign divides.
        ("2x2x2", 8),
        ("7 X 8", 56),
        ("2 \u00d7 3", 6),
        ("12 \u00f7 4", 3),
    ],
)
def test_calculator_evaluates_the_expression(expression: str, result: float) -> None:
    assert calculator(expression) == {"result": result}


@pytest.mark.parametrize(
    ("expression", "kind"),
    [("2x2x2", int), ("7/2", float), ("6/2", int), ("123456789012345678.5*2", int)],
)
def test_a_whole_number_comes_back_as_an_int(expression: str, kind: type) -> None:
    assert type(calculator(expression)["result"]) is kind


@pytest.mark.parametrize(
    ("expression", "error"),
    [
        ("1/0", "division by zero"),
        ("5/(2-2)", "division by zero"),
        ("2*", NOT_AN_EXPRESSION),
        ("1 2", NOT_AN_EXPRESSION),
        ("()", NOT_AN_EXPRESSION),
        ("...", NOT_AN_EXPRESSION),
        ("007", NOT_AN_EXPRESSION),
        ("1..2", NOT_AN_EXPRESSION),
        ("x", NOT_AN_EXPRESSION),
        # The pattern allows *, so ** gets through the schema; the evaluator refuses the power.
        ("2**8", NOT_AN_EXPRESSION),
    ],
)
def test_an_expression_without_a_value_comes_back_as_an_error(expression: str, error: str) -> None:
    result = calculator(expression)
    assert list(result) == ["error"]
    assert error in str(result["error"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (CALCULATOR, {"expression": "7*8+3"}, {"expression": "7*8+3"}),
        (CALCULATOR, {"expression": "2x2x2"}, {"expression": "2x2x2"}),
        (CALCULATOR, {"expression": "7 X 8"}, {"expression": "7 X 8"}),
        (
            CALCULATOR,
            {"expression": "12 \u00f7 (4 \u00d7 1.5)"},
            {"expression": "12 \u00f7 (4 \u00d7 1.5)"},
        ),
        (
            CALCULATOR,
            {"expression": "1+" * ((MAX_EXPRESSION_LENGTH - 1) // 2) + "1"},
            {"expression": "1+" * ((MAX_EXPRESSION_LENGTH - 1) // 2) + "1"},
        ),
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
        (CALCULATOR, '{"expression": "7 times 8"}', BOTH_TOOLS, ["'7 times 8'", "does not match"]),
        (CALCULATOR, '{"expression": "__import__"}', BOTH_TOOLS, ["does not match"]),
        (CALCULATOR, '{"expression": ""}', BOTH_TOOLS, ["''"]),
        (CALCULATOR, '{"expression": 56}', BOTH_TOOLS, ["56", "'string'"]),
        (
            CALCULATOR,
            json.dumps({"expression": "1+" * MAX_EXPRESSION_LENGTH}),
            BOTH_TOOLS,
            ["too long"],
        ),
        # The earlier calculator's arguments are refused: the error names the missing field.
        (CALCULATOR, '{"op": "add", "a": 2, "b": 3}', BOTH_TOOLS, ["'expression'"]),
        (CALCULATOR, '{"expression": "1+1", "op": "add"}', BOTH_TOOLS, ["'op'"]),
        (CALCULATOR, "[2, 3]", BOTH_TOOLS, ["'object'"]),
        (CALCULATOR, '{"expression": "2*', BOTH_TOOLS, ["not valid JSON"]),
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
