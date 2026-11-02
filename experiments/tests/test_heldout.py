"""Scoring the P1.7 held-out set: reading the generated questions, their expected values, the score
per mode and kind, and the result file's name."""

import asyncio
import csv
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from agent_runtime.config import load_config
from agent_runtime.llm import ChatReply, LLMClient
from agent_runtime.loop import run_task
from finetune_data import generate
from go_no_go import Question
from heldout import (
    COLUMNS,
    Heldout,
    check_baseline,
    cluster_failed,
    earlier_rows,
    expected_of,
    load_heldout,
    mcnemar_p,
    paired,
    remaining,
    result_csv,
    result_path,
    run_mode,
    score,
)

EXPERIMENTS = Path(__file__).resolve().parents[1]
SPEC: dict[str, Any] = yaml.safe_load((EXPERIMENTS / "finetune-data.yaml").read_text())
PROFILE = EXPERIMENTS.parent / "config" / f"{SPEC['profile']}.yaml"
TOLERANCE: float = yaml.safe_load(PROFILE.read_text())["agent"]["answer_tolerance"]
SMALL = {**SPEC, "csvs": {"train": 3, "heldout": 3}, "questions": {"train": 2, "heldout": 4}}


def heldout_file(tmp_path: Path) -> Path:
    _, lines = generate(SMALL, load_config(PROFILE), TOLERANCE)
    path = tmp_path / "heldout.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def test_load_heldout_reads_every_generated_question(tmp_path: Path) -> None:
    path = heldout_file(tmp_path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    questions = load_heldout(path)
    assert [q.question.id for q in questions] == [line["id"] for line in lines]
    assert [q.files for q in questions] == [line["files"] for line in lines]
    assert [q.seed for q in questions] == [line["seed"] for line in lines]


def test_expected_of_computes_every_kind_but_the_fixed_reply(tmp_path: Path) -> None:
    for heldout in load_heldout(heldout_file(tmp_path)):
        expected = expected_of(heldout)
        if heldout.question.expected["kind"] in ("fixed_reply", "contains"):
            assert expected is None
        else:
            assert expected is not None
            assert expected > 0, heldout.question.id


def test_score_counts_correct_runs_per_mode_and_kind() -> None:
    rows = [
        {"mode": "auto", "kind": "compare", "correct": True},
        {"mode": "auto", "kind": "compare", "correct": False},
        {"mode": "auto", "kind": "two-step", "correct": False},
        {"mode": "required", "kind": "compare", "correct": True},
    ]
    assert score(rows) == {
        ("auto", "compare"): (1, 2),
        ("auto", "two-step"): (0, 1),
        ("required", "compare"): (1, 1),
    }


@pytest.mark.parametrize(
    ("adapter", "name"),
    [
        (None, "finetune-heldout-0123456789ab-c61bb199c383.csv"),
        ("fae2e213e325", "finetune-heldout-0123456789ab-c61bb199c383-fae2e213e325.csv"),
    ],
)
def test_result_path_carries_the_data_hash_the_agent_and_the_adapter(
    adapter: str | None, name: str
) -> None:
    path = result_path(EXPERIMENTS, "0123456789ab", "thesis/agent:c61bb199c383", adapter)
    assert path == EXPERIMENTS / "results" / name


def test_result_csv_has_every_saved_row_before_it_is_closed(tmp_path: Path) -> None:
    """A held-out run takes hours: a row is on disk as soon as its run ends, so a crash or Ctrl+C
    keeps every finished run."""
    path = tmp_path / "results" / "heldout.csv"
    row = dict.fromkeys(COLUMNS, "x")
    with result_csv(path) as save:
        save(row)
        with path.open() as written:
            assert list(csv.DictReader(written)) == [row]


def test_run_mode_saves_each_row_before_the_next_run() -> None:
    """The second run's request fails; the first run's row was already saved."""
    reply = SPEC["off_topic_reply"]
    questions = [
        Heldout(
            Question(
                id=f"q{i}",
                text="Hi",
                expected_calls=[],
                expected={"kind": "fixed_reply", "reply": reply},
            ),
            "off-topic",
            [],
            i,
        )
        for i in (1, 2)
    ]
    posts = 0

    def agent(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if posts > 1:
                return httpx.Response(500)
            return httpx.Response(201, json={"run_id": "run-1"})
        body = {"run_id": "run-1", "status": "done", "answer": reply, "steps": []}
        return httpx.Response(200, json={**body, "invalid_calls": 0, "error": None})

    saved: list[dict[str, Any]] = []
    client = httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent))
    expected = {"q1": None, "q2": None}
    with pytest.raises(httpx.HTTPStatusError):
        run_mode(client, questions, "auto", expected, TOLERANCE, 60, saved.append)
    assert [(row["question"], row["correct"]) for row in saved] == [("q1", True)]


def off_topic(count: int) -> list[Heldout]:
    expected = {"kind": "fixed_reply", "reply": "x"}
    return [
        Heldout(Question(f"q{i}", "Hi", expected_calls=[], expected=expected), "off-topic", [], i)
        for i in range(1, count + 1)
    ]


def test_result_csv_keeps_the_rows_of_a_run_cut_short(tmp_path: Path) -> None:
    """A second run reads the first run's rows back and adds its own after them, under one
    header: a stop (sleep, crash, Ctrl+C) costs only the run that was going."""
    path = tmp_path / "results" / "heldout.csv"
    first = {**dict.fromkeys(COLUMNS, "x"), "question": "q1", "correct": True}
    second = {**first, "question": "q2", "correct": False}
    assert earlier_rows(path) == []
    with result_csv(path) as save:
        save(first)
    assert earlier_rows(path) == [first]
    with result_csv(path) as save:
        save(second)
    assert earlier_rows(path) == [first, second]


def test_result_csv_has_the_header_on_disk_before_the_first_row(tmp_path: Path) -> None:
    """A run killed before its first row (closed terminal, WSL shutdown) leaves a file that the
    next run reads as no rows, not one it refuses for having no header."""
    path = tmp_path / "results" / "heldout.csv"
    with result_csv(path):
        assert earlier_rows(path) == []


def test_earlier_rows_refuses_a_file_with_other_columns(tmp_path: Path) -> None:
    path = tmp_path / "heldout.csv"
    path.write_text("mode,question,correct\nauto,q1,True\n")
    with pytest.raises(ValueError, match="delete it"):
        earlier_rows(path)


def test_remaining_skips_the_questions_already_scored_in_that_mode() -> None:
    questions = off_topic(3)
    rows = [{"mode": "required", "question": "q1"}, {"mode": "auto", "question": "q2"}]
    assert [q.question.id for q in remaining(questions, rows, "required")] == ["q2", "q3"]
    assert [q.question.id for q in remaining(questions, rows, "auto")] == ["q1", "q3"]


class FailingLLM(LLMClient):
    """An LLM whose every call fails as failure says: an exception is raised as it is, a response
    raises its status error (one with a success status is a mistake in the test). It skips
    LLMClient's __init__ (no HTTP client)."""

    def __init__(self, failure: httpx.Response | httpx.HTTPError) -> None:
        self._failure = failure

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        headers: dict[str, str],
        seed: int | None,
    ) -> ChatReply:
        if isinstance(self._failure, httpx.HTTPError):
            raise self._failure
        self._failure.raise_for_status()
        raise AssertionError(f"{self._failure} has a success status, not a failure")


def runtime_error(failure: httpx.Response | httpx.HTTPError) -> str:
    """The error the agent runtime ends a run with when its LLM call fails as failure says."""
    result = asyncio.run(run_task("Hi", "run-1", 1, load_config(PROFILE), FailingLLM(failure)))
    assert result.status == "failed"
    assert result.error is not None
    return result.error


GATEWAY = httpx.Request("POST", "http://gateway/v1/chat/completions")


@pytest.mark.parametrize(
    ("failure", "cluster"),
    [
        # No vLLM pod ready: the gateway answers 503.
        (httpx.Response(503, request=GATEWAY), True),
        (httpx.ConnectError("connection refused"), True),
        # The model looped until its history was longer than model.max_model_len (P1.7,
        # heldout-compare-0322).
        (httpx.Response(400, request=GATEWAY), False),
        (httpx.ReadTimeout("timed out"), False),
    ],
)
def test_only_a_5xx_or_no_connection_is_the_cluster_failing(
    failure: httpx.Response | httpx.HTTPError, cluster: bool
) -> None:
    assert cluster_failed(runtime_error(failure)) is cluster


def failed_run_agent(error: str) -> httpx.Client:
    """An agent whose every run fails with error."""

    def agent(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"run_id": "run-1"})
        body: dict[str, Any] = {"run_id": "run-1", "status": "failed", "answer": None, "steps": []}
        return httpx.Response(200, json={**body, "invalid_calls": 0, "error": error})

    return httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent))


def test_run_mode_stops_on_a_cluster_failure_without_saving_it() -> None:
    """A gateway 503 is the cluster failing, not the model: the run stops, and the question has
    no row, so the next run asks it again."""
    client = failed_run_agent(runtime_error(httpx.Response(503, request=GATEWAY)))
    saved: list[dict[str, Any]] = []
    with pytest.raises(RuntimeError, match="503"):
        run_mode(client, off_topic(1), "auto", {"q1": None}, TOLERANCE, 60, saved.append)
    assert saved == []


def test_run_mode_saves_a_model_failure_as_a_wrong_run() -> None:
    """A 400 (a history over the context length) follows from what the model wrote: it is a
    wrong run, and the next question is asked."""
    client = failed_run_agent(runtime_error(httpx.Response(400, request=GATEWAY)))
    saved: list[dict[str, Any]] = []
    run_mode(client, off_topic(2), "auto", {"q1": None, "q2": None}, TOLERANCE, 60, saved.append)
    assert [(row["question"], row["correct"]) for row in saved] == [("q1", False), ("q2", False)]


def row(
    mode: str, kind: str, question: str, correct: bool, expected: str = "4.0"
) -> dict[str, Any]:
    return {
        "mode": mode,
        "kind": kind,
        "question": question,
        "correct": correct,
        "expected": expected,
    }


def heldout(question_id: str, kind: str = "arithmetic") -> Heldout:
    question = Question(
        id=question_id, text="What is 2x2?", expected_calls=[], expected={}, wrong_if_matches=None
    )
    return Heldout(question, kind, [], 1)


def test_check_baseline_accepts_one_row_per_mode_and_question_with_the_same_expected() -> None:
    questions = [heldout("q1"), heldout("q2", "off-topic")]
    baseline = [
        row(mode, h.kind, h.question.id, False, "" if h.kind == "off-topic" else "4.0")
        for mode in ("required", "auto")
        for h in questions
    ]
    check_baseline(baseline, questions, {"q1": 4.0, "q2": None}, ["required", "auto"])


def test_check_baseline_refuses_a_baseline_of_other_questions() -> None:
    """Another data hash's held-out set: the pairs would compare different questions."""
    baseline = [row("auto", "arithmetic", "q9", False)]
    with pytest.raises(ValueError, match="q1"):
        check_baseline(baseline, [heldout("q1")], {"q1": 4.0}, ["auto"])


def test_check_baseline_refuses_a_question_with_another_expected_answer() -> None:
    baseline = [row("auto", "arithmetic", "q1", False, "5.0")]
    with pytest.raises(ValueError, match="q1"):
        check_baseline(baseline, [heldout("q1")], {"q1": 4.0}, ["auto"])


def test_check_baseline_refuses_a_baseline_missing_a_mode() -> None:
    baseline = [row("auto", "arithmetic", "q1", False)]
    with pytest.raises(ValueError, match="required"):
        check_baseline(baseline, [heldout("q1")], {"q1": 4.0}, ["required", "auto"])


def test_paired_counts_each_question_by_who_got_it_right() -> None:
    """(both, baseline only, adapter only, neither) in one mode, over the given kinds."""
    baseline = [
        row("auto", "arithmetic", "q1", True),
        row("auto", "arithmetic", "q2", True),
        row("auto", "arithmetic", "q3", False),
        row("auto", "off-topic", "q4", False),
        row("required", "arithmetic", "q1", False),
    ]
    adapter = [
        row("auto", "arithmetic", "q1", True),
        row("auto", "arithmetic", "q2", False),
        row("auto", "arithmetic", "q3", True),
        row("auto", "off-topic", "q4", True),
        row("required", "arithmetic", "q1", True),
    ]
    assert paired(baseline, adapter, "auto", ["arithmetic", "off-topic"]) == (1, 1, 2, 0)
    assert paired(baseline, adapter, "auto", ["arithmetic"]) == (1, 1, 1, 0)


@pytest.mark.parametrize(
    ("only_baseline", "only_adapter", "p"),
    [
        # Exact binomial test on the discordant pairs: 2 x P(X <= 5), X ~ B(17, 1/2).
        (5, 12, 0.143463134765625),
        (12, 5, 0.143463134765625),
        (0, 10, 0.001953125),
        (3, 3, 1.0),
        (0, 0, 1.0),
    ],
)
def test_mcnemar_p_is_the_exact_two_sided_p_value(
    only_baseline: int, only_adapter: int, p: float
) -> None:
    assert mcnemar_p(only_baseline, only_adapter) == pytest.approx(p)


# P2.1b: questions with several files and text answers.
ORDERS = {"id": "orders", "name": "orders.csv", "type": "csv"}
ORDERS["content"] = "order_id,status\n1041,shipped\n"
POLICIES = {"id": "policies", "name": "policies.txt", "type": "text"}
POLICIES["content"] = "## Returns\nIn 30 days."
STATUS = Question(
    id="q1",
    text="What is the status of order 1041?",
    expected_calls=[{"tool": "get_order", "arguments": {"order_id": "1041"}}],
    expected={"kind": "contains", "texts": ["shipped", "1041"]},
)


def status_agent(posted: list[dict[str, Any]]) -> httpx.Client:
    """An agent that keeps every POST /runs body and answers each run with get_order's row."""

    def agent(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={"run_id": "run-1"})
        call = {"name": "get_order", "arguments": {"file": "orders", "order_id": "1041"}}
        steps = [{"step": 1, "tool_calls": [{**call, "result": {"order": {"status": "shipped"}}}]}]
        answer = "Order 1041 is shipped."
        body = {"run_id": "run-1", "status": "done", "answer": answer, "steps": steps}
        return httpx.Response(200, json={**body, "invalid_calls": 0, "error": None})

    return httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent))


def test_run_mode_attaches_every_file_of_a_question() -> None:
    posted: list[dict[str, Any]] = []
    question = Heldout(STATUS, "order-status", [ORDERS, POLICIES], 5)
    rows = run_mode(status_agent(posted), [question], "auto", {"q1": None}, TOLERANCE, 60, print)
    assert [body["files"] for body in posted] == [[ORDERS, POLICIES]]
    assert posted[0]["seed"] == 5
    assert rows[0]["correct"] is True


def test_a_text_question_s_expected_column_holds_its_texts() -> None:
    """A text answer has no expected value; the CSV shows what it had to contain, and a baseline
    is checked against that."""
    saved: list[dict[str, Any]] = []
    question = Heldout(STATUS, "order-status", [ORDERS, POLICIES], 5)
    run_mode(status_agent([]), [question], "auto", {"q1": None}, TOLERANCE, 60, saved.append)
    assert saved[0]["expected"] == "shipped | 1041"
    baseline = [row("auto", "order-status", "q1", False, "shipped | 1041")]
    check_baseline(baseline, [question], {"q1": None}, ["auto"])
    other = [row("auto", "order-status", "q1", False, "")]
    with pytest.raises(ValueError, match="q1"):
        check_baseline(other, [question], {"q1": None}, ["auto"])
