"""The P1.7 data generator: its CSVs, questions and trajectories, checked against the agent's own
tools and the go/no-go scorer, independently of how the generator built them."""

import ast
import asyncio
import csv
import io
import itertools
import json
import operator
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_runtime.config import AgentConfig, ToolChoice, load_config
from agent_runtime.csv_tools import parse_file
from agent_runtime.tools import FINAL_ANSWER, offered_tools, run_tool, tool_definitions
from finetune_data import KINDS, Planned, generate, plan, record
from go_no_go import Question, expected_value, is_correct

EXPERIMENTS = Path(__file__).resolve().parents[1]
SPEC_PATH = EXPERIMENTS / "finetune-data.yaml"
SPEC: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
CONFIG = load_config(EXPERIMENTS.parent / "config" / f"{SPEC['profile']}.yaml")
TOLERANCE: float = yaml.safe_load(
    (EXPERIMENTS.parent / "config" / f"{SPEC['profile']}.yaml").read_text()
)["agent"]["answer_tolerance"]
SALES = EXPERIMENTS / "data" / "sales.csv"
# A small version of the experiment file, so the tests run in seconds; the real sizes only change
# how many questions come out, not how each is built.
SMALL = {**SPEC, "csvs": {"train": 6, "heldout": 4}, "questions": {"train": 12, "heldout": 8}}

OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}


def small_plan(split: str) -> list[Planned]:
    return plan(SMALL, split, TOLERANCE, CONFIG.max_result_rows, CONFIG.result_decimals)


def rows_of(planned: Planned) -> list[dict[str, str]]:
    return (
        [] if planned.file is None else list(csv.DictReader(io.StringIO(planned.file["content"])))
    )


def tool_results(planned: Planned) -> list[dict[str, Any]]:
    """What the agent's run_tool returns for each planned call, on the question's file."""
    tables = {} if planned.file is None else {planned.file["id"]: parse_table(planned.file)}
    return [
        run_tool(name, arguments, tables, CONFIG.max_result_rows, CONFIG.result_decimals)
        for name, arguments in planned.calls
        if name != FINAL_ANSWER
    ]


def parse_table(file: dict[str, str]) -> Any:
    return parse_file(file["id"], file["name"], file["content"])


def numbers_of(value: Any) -> list[float]:
    """Every number in a tool result, at any depth."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [float(value)]
    if isinstance(value, dict):
        return [n for item in value.values() for n in numbers_of(item)]
    if isinstance(value, list):
        return [n for item in value for n in numbers_of(item)]
    return []


def arithmetic_value(text: str) -> float:
    """The value of the expression in an arithmetic question with the usual precedence, read
    with Python's own parser: the words become operators, then ast evaluates the tree."""
    words = {"times": "*", "x": "*", "multiplied by": "*", "divided by": "/", "plus": "+"}
    words |= {"minus": "-"}
    expression = text.lower().rstrip("?.").split(":")[-1].strip()
    for prefix in ("what is", "calculate", "work out", "compute"):
        expression = expression.removeprefix(prefix)
    for word, symbol in sorted(words.items(), key=lambda item: -len(item[0])):
        expression = re.sub(rf"(?<=[\d\s]){re.escape(word)}(?=[\s\d])", f" {symbol} ", expression)

    def value(node: ast.expr) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return value(node.left) / value(node.right)
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            return float(OPERATORS[type(node.op)](value(node.left), value(node.right)))
        raise ValueError(f"not an arithmetic expression: {ast.dump(node)}")

    return value(ast.parse(expression.strip(), mode="eval").body)


def as_run_body(planned: Planned, answer: str) -> dict[str, Any]:
    """The GET /runs/{id} body of a run that made exactly the planned calls, as go_no_go reads it:
    every call has its tool result, final_answer none."""
    results = iter(tool_results(planned))
    steps = [
        {
            "tool_calls": [
                {
                    "name": name,
                    "arguments": arguments,
                    "result": None if name == FINAL_ANSWER else next(results),
                }
            ]
        }
        for name, arguments in planned.calls
    ]
    return {"status": "done", "answer": answer, "steps": steps}


def test_the_same_seed_gives_the_same_data() -> None:
    first = generate(SMALL, CONFIG, TOLERANCE)
    second = generate(SMALL, CONFIG, TOLERANCE)
    assert json.dumps(first) == json.dumps(second)


def test_a_different_seed_gives_different_data() -> None:
    other = {**SMALL, "seeds": {"train": 7, "heldout": 8}}
    assert json.dumps(generate(SMALL, CONFIG, TOLERANCE)) != json.dumps(
        generate(other, CONFIG, TOLERANCE)
    )


@pytest.mark.parametrize("split", ["train", "heldout"])
def test_every_kind_has_the_configured_number_of_questions(split: str) -> None:
    counts = Counter(planned.kind for planned in small_plan(split))
    assert counts == dict.fromkeys(KINDS, SMALL["questions"][split])


def test_every_training_question_has_a_trajectory_in_every_mode() -> None:
    examples, _ = generate(SMALL, CONFIG, TOLERANCE)
    trajectories = {(example["question"], example["mode"]) for example in examples}
    questions = {planned.id for planned in small_plan("train")}
    assert trajectories == {(q, mode) for q in questions for mode in SMALL["modes"]}


def test_held_out_lines_are_the_held_out_questions_for_the_scorer() -> None:
    _, heldout = generate(SMALL, CONFIG, TOLERANCE)
    assert [line["id"] for line in heldout] == [planned.id for planned in small_plan("heldout")]
    for line in heldout:
        Question(
            id=line["id"],
            text=line["text"],
            expected_calls=line["expected_calls"],
            expected=line["expected"],
            wrong_if_matches=line["wrong_if_matches"],
        )


def test_the_pools_share_no_name_with_each_other_or_with_sales_csv() -> None:
    def names(pool: dict[str, Any]) -> set[str]:
        found = set(pool["files"])
        for column_kind in ("text_columns", "priced_columns"):
            for column, values in pool[column_kind].items():
                found |= {column, *values}
        found |= {name for pair in pool["number_columns"] for name in pair}
        found |= {value for values in pool["off_topic"].values() for value in values}
        return found

    sales_rows = list(csv.DictReader(SALES.open()))
    sales = {"sales", *sales_rows[0]} | {row[c] for row in sales_rows for c in ("month", "region")}
    sales |= {row["product"] for row in sales_rows}
    train, heldout = names(SPEC["pools"]["train"]), names(SPEC["pools"]["heldout"])
    assert not train & heldout
    assert not (train | heldout) & sales


def test_held_out_files_use_only_held_out_names() -> None:
    pool = SPEC["pools"]["train"]
    train_names = set(pool["files"]) | set(pool["text_columns"]) | set(pool["priced_columns"])
    train_names |= {name for pair in pool["number_columns"] for name in pair}
    for planned in small_plan("heldout"):
        if planned.file is not None:
            header = planned.file["content"].splitlines()[0].split(",")
            assert not (set(header) | {planned.file["id"]}) & train_names


@pytest.mark.parametrize("split", ["train", "heldout"])
def test_every_answer_scores_correct_with_the_go_no_go_scorer(split: str) -> None:
    for planned in small_plan(split):
        spec = planned.expected
        expected = None if spec["kind"] == "fixed_reply" else expected_value(rows_of(planned), spec)
        question = Question(
            id=planned.id,
            text=planned.text,
            expected_calls=planned.expected_calls,
            expected=spec,
            wrong_if_matches=planned.wrong_if_matches,
        )
        assert is_correct(as_run_body(planned, planned.answer), question, expected, TOLERANCE), (
            planned
        )


@pytest.mark.parametrize("split", ["train", "heldout"])
def test_no_other_number_the_model_sees_passes_for_the_answer(split: str) -> None:
    """Another group's value, the sum over all groups, an operand: every number in the tool
    results or the question that is not the answer is more than the tolerance from it, so copying
    the wrong one scores wrong."""
    for planned in small_plan(split):
        if planned.kind == "off-topic":
            continue
        rows = rows_of(planned)
        expected = expected_value(rows, planned.expected)
        seen = [n for result in tool_results(planned) for n in numbers_of(result)]
        if planned.kind == "arithmetic":
            seen += [float(n) for n in re.findall(r"\d+", planned.text)]
        else:
            value = planned.expected["value"]
            seen.append(sum(float(row[value]) for row in rows))
        # The answer itself is in the last result, rounded to result_decimals.
        rounding = 10**-CONFIG.result_decimals
        others = [n for n in seen if abs(n - expected) > rounding]
        assert all(abs(n - expected) > TOLERANCE * abs(expected) for n in others), planned


@pytest.mark.parametrize("split", ["train", "heldout"])
def test_two_step_needs_both_calls(split: str) -> None:
    """The group that ranks first by the rank column is not the one with the most of the asked
    value, so a single group_by cannot give the answer by luck; no tie for first place."""
    for planned in small_plan(split):
        if planned.kind != "two-step":
            continue
        rank, asked = tool_results(planned)
        assert rank["rows"][0]["group"] != asked["rows"][0]["group"], planned
        assert rank["rows"][0][rank["agg"]] != rank["rows"][1][rank["agg"]], planned


@pytest.mark.parametrize("split", ["train", "heldout"])
def test_arithmetic_questions_mean_what_their_calls_compute(split: str) -> None:
    for planned in small_plan(split):
        if planned.kind != "arithmetic":
            continue
        assert arithmetic_value(planned.text) == pytest.approx(
            expected_value([], planned.expected)
        ), planned
        low, high = SPEC["arithmetic"]["operations"]
        assert low <= len(planned.expected["steps"]) <= high


def test_held_out_arithmetic_and_off_topic_questions_are_not_in_training() -> None:
    train = {planned.text for planned in small_plan("train")}
    for planned in small_plan("heldout"):
        if planned.kind in ("arithmetic", "off-topic"):
            assert planned.text not in train


@pytest.mark.parametrize("split", ["train", "heldout"])
def test_off_topic_questions_get_the_sentence_and_call_no_tool(split: str) -> None:
    for planned in small_plan(split):
        if planned.kind == "off-topic":
            assert planned.answer == SPEC["off_topic_reply"]
            assert planned.calls == []
            assert planned.expected_calls == []


@pytest.mark.parametrize("mode", ["required", "auto"])
def test_a_trajectory_is_the_runtime_s_own_run(mode: ToolChoice) -> None:
    """Each recorded example is what run_task sent the model at that step, and the run ends done
    with the planned answer, no invalid call, and every tool result equal to run_tool's."""
    config = replace(CONFIG, tool_choice=mode)
    for planned in small_plan("train"):
        examples, result = asyncio.run(record(planned, config))
        assert result.status == "done", (planned, result.error)
        assert result.answer == planned.answer
        assert result.invalid_calls == 0
        tables = [] if planned.file is None else [parse_table(planned.file)]
        tools = tool_definitions(offered_tools(mode, tables))
        assert all(example["tools"] == tools for example in examples)
        assert examples[0]["messages"] == [
            {"role": "system", "content": CONFIG.system_prompt},
            {"role": "user", "content": planned.text},
        ]
        # Every example's history is the previous one's plus its reply and the tool results.
        for before, after in itertools.pairwise(examples):
            assert after["messages"][: len(before["messages"]) + 1] == [
                *before["messages"],
                before["reply"],
            ]
        recorded = [
            json.loads(message["content"])
            for message in result.messages
            if message["role"] == "tool"
        ]
        assert recorded == tool_results(planned)


def test_required_mode_ends_with_final_answer_and_auto_mode_with_text() -> None:
    planned = small_plan("train")[0]
    required, _ = asyncio.run(record(planned, replace(CONFIG, tool_choice="required")))
    auto, _ = asyncio.run(record(planned, replace(CONFIG, tool_choice="auto")))
    last_call = required[-1]["reply"]["tool_calls"][0]["function"]
    assert last_call["name"] == FINAL_ANSWER
    assert json.loads(last_call["arguments"]) == {"answer": planned.answer}
    assert auto[-1]["reply"] == {"role": "assistant", "content": planned.answer}
    assert len(required) == len(auto)


def test_tool_call_arguments_are_json_in_the_schema_s_order() -> None:
    """As vLLM returns them: a JSON string, keys in the order the tool's schema lists them."""
    config: AgentConfig = replace(CONFIG, tool_choice="auto")
    for planned in small_plan("train"):
        examples, _ = asyncio.run(record(planned, config))
        for example in examples:
            for call in example["reply"].get("tool_calls", []):
                function = call["function"]
                schema = {d["function"]["name"]: d for d in example["tools"]}[function["name"]]
                order = list(schema["function"]["parameters"]["properties"])
                assert list(json.loads(function["arguments"])) == order
                assert function["arguments"] == json.dumps(json.loads(function["arguments"]))


def test_an_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a D10 mode"):
        generate({**SMALL, "modes": ["required", "none"]}, CONFIG, TOLERANCE)
