"""The P4.7 task suite (docs/task-cards/71-P4.7-task-suite.md): its questions, checked against the
agent's own tools, the chat path as the runner walks it, and the table. The app backend is
httpx.MockTransport, so nothing here needs a cluster."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from agent_runtime.config import load_config
from agent_runtime.documents import read_run_files
from agent_runtime.tools import run_call
from go_no_go import CONTAINS, load_experiment
from task_suite import (
    AppClient,
    RunTimedOut,
    Suite,
    expected_values,
    load_suite,
    result_path,
    run_suite,
    table,
)

EXPERIMENTS = Path(__file__).resolve().parents[1]
SUITE_PATH = EXPERIMENTS / "task-suite.yaml"
SUITE = load_suite(SUITE_PATH)
CONFIG = load_config(EXPERIMENTS.parent / "config" / f"{SUITE.profile}.yaml")
TOLERANCE: float = yaml.safe_load(
    (EXPERIMENTS.parent / "config" / f"{SUITE.profile}.yaml").read_text()
)["agent"]["answer_tolerance"]

# The full call that answers each text question on its scenario's files, to run through the agent's
# own tools: what the expected texts must be found in.
ANSWERING_CALLS: dict[tuple[str, str], tuple[str, dict[str, str]]] = {
    ("contracts", "termination-diff"): (
        "diff_clauses",
        {"left": "contract-2025", "right": "contract-2026", "clause": "termination"},
    ),
    ("contracts", "fees-diff"): (
        "diff_clauses",
        {"left": "contract-2025", "right": "contract-2026", "clause": "fees"},
    ),
    ("contracts", "confidentiality-text"): (
        "extract_clauses",
        {"file": "contract-2026", "clause": "confidentiality"},
    ),
    ("contracts", "services-text"): (
        "extract_clauses",
        {"file": "contract-2025", "clause": "services"},
    ),
    ("contracts", "termination-text"): (
        "extract_clauses",
        {"file": "contract-2026", "clause": "termination"},
    ),
    ("support", "returns-reply"): (
        "draft_reply",
        {"orders": "orders", "order_id": "1041", "policies": "policies", "topic": "returns"},
    ),
    ("support", "damaged-reply"): (
        "draft_reply",
        {"orders": "orders", "order_id": "1042", "policies": "policies", "topic": "damaged items"},
    ),
    ("support", "status-1043"): ("get_order", {"file": "orders", "order_id": "1043"}),
    ("support", "status-1041"): ("get_order", {"file": "orders", "order_id": "1041"}),
    ("support", "refunds-policy"): ("get_policy", {"file": "policies", "topic": "refunds"}),
}


def test_the_suite_is_the_three_demo_scenarios_ten_runs_each() -> None:
    assert [s.name for s in SUITE.scenarios] == ["csv", "contracts", "support"]
    assert all(len(s.questions) * len(SUITE.seeds) == 10 for s in SUITE.scenarios)
    assert (SUITE.profile, SUITE.threshold) == ("demo", 9)


def test_the_csv_questions_are_the_go_no_go_s() -> None:
    go_no_go = load_experiment(EXPERIMENTS / "demo-go-no-go.yaml")
    [csv] = [s for s in SUITE.scenarios if s.name == "csv"]
    assert csv.questions == go_no_go.questions
    assert csv.files == [go_no_go.file]


def test_every_number_comes_from_the_csv_and_every_text_question_has_none() -> None:
    expected = expected_values(SUITE)
    for scenario in SUITE.scenarios:
        for question in scenario.questions:
            value = expected[(scenario.name, question.id)]
            if question.expected["kind"] == CONTAINS:
                assert value is None
            else:
                assert isinstance(value, float)


def test_every_expected_text_is_in_the_result_of_the_call_that_answers_it() -> None:
    """No question asks for what the tools cannot give, as the generator checks its own
    (finetune_data._contains); the answering call is one the question's expected calls allow."""
    texts = 0
    for scenario in SUITE.scenarios:
        tables, documents = read_run_files(scenario.files, CONFIG.max_files, CONFIG.max_file_bytes)
        for question in scenario.questions:
            if question.expected["kind"] != CONTAINS:
                continue
            name, arguments = ANSWERING_CALLS[(scenario.name, question.id)]
            [expected_call] = question.expected_calls
            assert expected_call["tool"] == name
            assert expected_call.get("arguments", {}).items() <= arguments.items()
            result = run_call(
                name,
                arguments,
                {t.id: t for t in tables},
                {d.id: d for d in documents},
                CONFIG,
            )
            assert "error" not in result, (question.id, result)
            said = json.dumps(result, ensure_ascii=False).casefold()
            for text in question.expected["texts"]:
                assert text.casefold() in said, (question.id, text, result)
                texts += 1
    assert texts == 14


def test_a_text_file_is_sent_as_text() -> None:
    [contracts] = [s for s in SUITE.scenarios if s.name == "contracts"]
    assert {file.get("type") for file in contracts.files} == {"text"}
    [csv] = [s for s in SUITE.scenarios if s.name == "csv"]
    assert "type" not in csv.files[0]


# The chat path, on a mocked app backend.

SESSION = "token-abc"


class FakeApp:
    """POST /auth/register and /auth/login (a Secure session cookie), POST /chats, POST
    /chats/{id}/messages, GET /chats/{id} (running for running_reads reads, then done) and GET
    /runs/{id}. Records every request."""

    def __init__(self, run: dict[str, Any], running_reads: int = 1) -> None:
        self.run = run
        self.running_reads = running_reads
        self.requests: list[httpx.Request] = []
        self.chats = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/auth/register":
            return httpx.Response(201, json={"id": 1, "email": "x"})
        if path == "/auth/login":
            cookie = f"session={SESSION}; HttpOnly; Path=/; SameSite=lax; Secure"
            return httpx.Response(200, json={"id": 1}, headers={"set-cookie": cookie})
        if request.headers.get("cookie") != f"session={SESSION}":
            return httpx.Response(401, json={"detail": "not logged in"})
        if path == "/chats" and request.method == "POST":
            self.chats += 1
            return httpx.Response(201, json={"id": self.chats, "title": "t", "created": 1.0})
        if path.endswith("/messages"):
            return httpx.Response(
                202,
                json={"user_message_id": 1, "assistant_message_id": 2, "run_id": "run-1"},
            )
        if path.startswith("/chats/"):
            status = "running" if self.running_reads > 0 else self.run["status"]
            self.running_reads -= 1
            reply = {"id": 2, "role": "assistant", "status": status, "run_id": "run-1"}
            return httpx.Response(200, json={"id": 1, "messages": [{"id": 1}, reply]})
        if path == "/runs/run-1":
            return httpx.Response(200, json=self.run)
        return httpx.Response(404)


def a_run(
    answer: str, calls: Sequence[tuple[str, dict[str, Any], Any]], pick: str
) -> dict[str, Any]:
    steps = [{"step": 1, "content": pick, "tool_calls": [], "error": None}]
    for n, (name, arguments, result) in enumerate(calls, 2):
        call = {"name": name, "arguments": arguments, "result": result}
        steps.append({"step": n, "content": None, "tool_calls": [call], "error": None})
    return {
        "run_id": "run-1",
        "status": "done",
        "answer": answer,
        "error": None,
        "invalid_calls": 0,
        "steps": steps,
    }


def client_on(app: FakeApp) -> AppClient:
    http = httpx.Client(transport=httpx.MockTransport(app.handle), base_url="http://app")
    return AppClient(http, poll_seconds=0)


def test_signing_up_registers_and_keeps_the_session_for_later_requests() -> None:
    app = FakeApp(a_run("56", [], "arithmetic"))
    client = client_on(app)
    client.sign_up("suite@task-suite.invalid", "p" * 16)
    run, _ = client.ask("What is 7 times 8?", [], 1, run_timeout=60)
    assert run["answer"] == "56"
    paths = [r.url.path for r in app.requests]
    assert paths[:2] == ["/auth/register", "/auth/login"]
    assert all(r.headers["cookie"] == f"session={SESSION}" for r in app.requests[2:])


def test_a_question_goes_as_one_message_of_a_new_chat_with_its_files_and_seed() -> None:
    app = FakeApp(a_run("56", [], "arithmetic"), running_reads=2)
    client = client_on(app)
    client.sign_up("suite@task-suite.invalid", "p" * 16)
    files = [{"id": "sales", "name": "sales.csv", "content": "a\n1\n"}]
    client.ask("Total?", files, 2, run_timeout=60)
    client.ask("Again?", files, 1, run_timeout=60)
    assert app.chats == 2
    [first, second] = [
        json.loads(r.content) for r in app.requests if r.url.path.endswith("/messages")
    ]
    assert first == {"task": "Total?", "files": files, "seed": 2}
    assert second["task"] == "Again?"
    # Read until the answer was no longer running: two running reads, then done.
    reads = [r for r in app.requests if r.method == "GET" and r.url.path == "/chats/1"]
    assert len(reads) == 3


def test_a_run_still_going_after_the_timeout_raises() -> None:
    app = FakeApp(a_run("56", [], "arithmetic"), running_reads=10**6)
    client = client_on(app)
    client.sign_up("suite@task-suite.invalid", "p" * 16)
    with pytest.raises(RunTimedOut, match="run-1"):
        client.ask("Total?", [], 1, run_timeout=0)


class ScriptedClient:
    """Answers every question with the run its text maps to; records each question asked."""

    def __init__(self, runs: Mapping[str, dict[str, Any]]) -> None:
        self.runs = runs
        self.asked: list[tuple[str, list[dict[str, str]], int]] = []

    def ask(
        self, task: str, files: list[dict[str, str]], seed: int, run_timeout: float
    ) -> tuple[dict[str, Any], float]:
        self.asked.append((task, files, seed))
        return self.runs[task], 1.5


def suite_of(*names: str) -> Suite:
    scenarios = [s for s in SUITE.scenarios if s.name in names]
    return Suite(SUITE.path, SUITE.hash, SUITE.profile, SUITE.seeds, SUITE.threshold, scenarios)


def test_every_question_runs_once_per_seed_with_its_scenario_s_files() -> None:
    suite = suite_of("support")
    [support] = suite.scenarios
    runs = {q.text: a_run("x", [], "support") for q in support.questions}
    client = ScriptedClient(runs)
    rows = run_suite(client, suite, expected_values(suite), TOLERANCE, 60)
    assert len(rows) == len(client.asked) == 10
    assert [seed for _, _, seed in client.asked] == [1, 2] * 5
    assert all(files == support.files for _, files, _ in client.asked)


def test_a_row_scores_the_run_and_says_what_step_1_picked() -> None:
    suite = suite_of("support")
    [support] = suite.scenarios
    status = next(q for q in support.questions if q.id == "status-1043")
    order = {"order_id": "1043", "status": "shipped"}
    right = a_run(
        "Order 1043 has shipped.", [("get_order", {"order_id": "1043"}, order)], "support"
    )
    wrong = a_run("I can only do arithmetic.", [], "out_of_scope")
    runs = {q.text: (right if q is status else wrong) for q in support.questions}
    rows = run_suite(ScriptedClient(runs), suite, expected_values(suite), TOLERANCE, 60)
    by_question = {(row["question"], row["seed"]): row for row in rows}
    row = by_question[("status-1043", 1)]
    assert (row["scenario"], row["correct"], row["picked"], row["steps"]) == (
        "support",
        True,
        "support",
        2,
    )
    assert row["expected"] == "shipped"
    assert by_question[("refunds-policy", 2)]["correct"] is False
    assert by_question[("refunds-policy", 2)]["picked"] == "out_of_scope"


def test_the_table_counts_each_scenario_and_names_the_ones_below_the_threshold() -> None:
    rows = (
        [{"scenario": "csv", "correct": i < 8, "picked": "csv"} for i in range(10)]
        + [{"scenario": "contracts", "correct": True, "picked": "contracts"} for _ in range(10)]
        + [{"scenario": "support", "correct": i < 9, "picked": "support"} for i in range(9)]
        + [{"scenario": "support", "correct": False, "picked": "csv"}]
    )
    lines = table(rows, SUITE)
    assert lines == [
        "csv: 8/10 correct, picked csv 10",
        "contracts: 10/10 correct, picked contracts 10",
        "support: 9/10 correct, picked support 9, csv 1",
        "no-go: csv below 9/10",
    ]
    passing = [row | {"correct": True} for row in rows]
    assert table(passing, SUITE)[-1] == "go: every scenario 9/10 or more"


def test_results_are_named_by_the_suite_s_hash_and_the_agent_image() -> None:
    path = result_path(SUITE, "thesis/agent:10ff2bb380fa")
    assert path == EXPERIMENTS / "results" / f"task-suite-{SUITE.hash}-10ff2bb380fa.csv"
