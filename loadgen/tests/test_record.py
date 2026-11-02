"""The recorder (P2.2b): which trace lines a run's calls become, and when a recording stops.

The agent is httpx.MockTransport and the Redis read a plain function, so nothing here needs a
cluster; make record's done-when runs it against the real one. The schemas are the repo's own, so
these are also trace.v1's and run-request.v1's contract tests for loadgen (D12).
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from agent_runtime.config import load_config
from agent_runtime.documents import read_run_files
from agent_runtime.llm import LLMClient
from agent_runtime.loop import run_task
from agent_runtime.store import CallRecord, RedisStore, RunRecord
from agent_runtime.tools import offered_tools
from jsonschema import Draft202012Validator
from load_generator.record import (
    Experiment,
    RecordedRun,
    RecordError,
    Task,
    adapter_id,
    check_before_start,
    check_calls,
    check_line,
    experiment_hash,
    load_experiment,
    main,
    parse_calls,
    ready_pod_ip,
    record_run,
    redis_calls,
    run_request,
    summary,
    trace_lines,
    trace_path,
    trace_validator,
    write_trace,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "schemas"
DEMO = REPO_ROOT / "experiments" / "record-demo.yaml"
REAL_PROFILE = REPO_ROOT / "config" / "real.yaml"

RUN_ID = "run-0123456789ab"
TASK = Task(task_type="support", text="What is the status of order 1043?", seed=1, files=[])
# Step 2 of the P2.2a smoke run, as Redis holds it (experiments/log.md).
CALL: dict[str, Any] = {
    "step": 2,
    "ts_start": 1793606543527,
    "duration_ms": 4181,
    "gap_before_ms": 11,
    "expected_steps": 2,
    "prompt_tokens": 398,
    "completion_tokens": 10,
    "cached_tokens": 352,
    "pod": "vllm-5c9565db76-6bq8s",
}


def calls_of(steps: int) -> list[dict[str, Any]]:
    """Valid calls for steps 1..steps."""
    return [{**CALL, "step": step} for step in range(1, steps + 1)]


def run_body(status: str = "done", steps: int = 2, error: str | None = None) -> dict[str, Any]:
    """A GET /runs/{id} body; only its status, steps, error and answer matter here."""
    return {
        "run_id": RUN_ID,
        "status": status,
        "answer": "56" if status == "done" else None,
        "error": error,
        "steps": [{"step": step} for step in range(1, steps + 1)],
    }


class FakeAgent:
    """POST /runs answers RUN_ID, each GET /runs/{id} the next of states; the last one repeats."""

    def __init__(self, *states: dict[str, Any]) -> None:
        self._states = list(states)
        self.posted: list[dict[str, Any]] = []
        self.gets = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posted.append(json.loads(request.content))
            return httpx.Response(202, json={"run_id": RUN_ID})
        assert request.url.path == f"/runs/{RUN_ID}"
        self.gets += 1
        return httpx.Response(200, json=self._states[min(self.gets, len(self._states)) - 1])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), base_url="http://agent")


def record(
    agent: FakeAgent,
    read_calls: Callable[[str], list[dict[str, Any]]],
    run_timeout: float = 60,
) -> RecordedRun:
    return record_run(agent.client(), TASK, read_calls, trace_validator(), run_timeout, 0)


# The mapping from a run's calls to trace lines.


def test_a_call_becomes_a_line_with_its_run_and_task_type() -> None:
    assert trace_lines(RUN_ID, "contracts", [CALL]) == [
        {"schema_version": 1, "run_id": RUN_ID, "task_type": "contracts", **CALL}
    ]


def test_lines_keep_the_step_order_and_copy_each_gap() -> None:
    calls = [{**CALL, "step": 1, "gap_before_ms": 0}, {**CALL, "step": 2, "gap_before_ms": 11}]
    lines = trace_lines(RUN_ID, "csv", calls)
    assert [(line["step"], line["gap_before_ms"]) for line in lines] == [(1, 0), (2, 11)]


@pytest.mark.parametrize("absent", ["cached_tokens", "pod"])
def test_a_measurement_the_call_lacks_stays_out_of_the_line(absent: str) -> None:
    call = {name: value for name, value in CALL.items() if name != absent}
    [line] = trace_lines(RUN_ID, "csv", [call])
    assert absent not in line
    check_line(trace_validator(), line)


def test_a_record_as_the_agent_saves_it_makes_a_valid_line() -> None:
    record = CallRecord(
        step=1,
        ts_start=1793606515407,
        duration_ms=25708,
        gap_before_ms=0,
        expected_steps=2,
        prompt_tokens=358,
        completion_tokens=21,
        cached_tokens=0,
        pod="vllm-5c9565db76-6bq8s",
    )
    [line] = trace_lines(RUN_ID, "csv", [json.loads(json.dumps(record.fields()))])
    check_line(trace_validator(), line)
    assert line["cached_tokens"] == 0


# trace.v1's contract: a line that does not fit stops the recording.


def test_the_schemas_valid_example_passes() -> None:
    check_line(
        trace_validator(), json.loads((SCHEMAS / "examples/trace.v1.valid.json").read_text())
    )


def test_the_schemas_invalid_example_stops_the_recording() -> None:
    invalid = json.loads((SCHEMAS / "examples/trace.v1.invalid.json").read_text())
    with pytest.raises(RecordError, match=r"not a trace\.v1 line"):
        check_line(trace_validator(), invalid)


@pytest.mark.parametrize(
    "change",
    [
        {"prompt_tokens": None},
        {"completion_tokens": None},
        {"expected_steps": None},
        {"pod": ""},
        {"prompt_tokens": -1},
        {"cached_tokens": -1},
        {"duration_ms": "4181"},
        {"finish_reason": "stop"},
    ],
    ids=str,
)
def test_a_call_trace_v1_refuses_stops_the_recording(change: dict[str, Any]) -> None:
    """None removes the field: agent's CallRecord.fields leaves out what a reply lacked."""
    changed = {**CALL, **change}
    call = {name: value for name, value in changed.items() if value is not None}
    [line] = trace_lines(RUN_ID, "csv", [call])
    with pytest.raises(RecordError, match=f"{RUN_ID} step 2"):
        check_line(trace_validator(), line)


# A run's calls must be the steps GET /runs/{id} shows.


@pytest.mark.parametrize("steps", [1, 2, 5])
def test_calls_that_are_steps_1_to_n_pass(steps: int) -> None:
    check_calls(RUN_ID, calls_of(steps), steps)


@pytest.mark.parametrize(
    ("steps", "shown"),
    [
        pytest.param([2, 1], 2, id="out of order"),
        pytest.param([1, 3], 2, id="a step missing"),
        pytest.param([2, 3], 2, id="not from step 1"),
        pytest.param([1, 1], 2, id="a step twice"),
        pytest.param([1, 2], 3, id="fewer than the run shows"),
        pytest.param([1, 2], 1, id="more than the run shows"),
    ],
)
def test_calls_that_do_not_match_the_runs_steps_stop_the_recording(
    steps: list[int], shown: int
) -> None:
    calls = [{**CALL, "step": step} for step in steps]
    with pytest.raises(RecordError, match=RUN_ID):
        check_calls(RUN_ID, calls, shown)


# Reading the calls from Redis.


def test_the_calls_come_from_redis_clis_output() -> None:
    assert parse_calls(RUN_ID, json.dumps(calls_of(2)) + "\n") == calls_of(2)


def test_a_missing_key_stops_the_recording() -> None:
    """redis-cli prints an empty line for a key that does not exist, without a terminal."""
    with pytest.raises(RecordError, match=f"{RUN_ID}: no calls in Redis"):
        parse_calls(RUN_ID, "\n")


def test_the_calls_are_read_from_the_key_the_agent_saves_them_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake Redis takes the agent's own RedisStore.save, so a renamed key fails here."""
    saved: dict[str, str] = {}

    class Pipeline:
        def hset(self, key: str, mapping: dict[str, Any]) -> None: ...

        def expire(self, key: str, seconds: int) -> None: ...

        def set(self, key: str, value: str, ex: int) -> None:
            saved[key] = value

        async def execute(self) -> None: ...

        async def __aenter__(self) -> "Pipeline":
            return self

        async def __aexit__(self, *args: object) -> None: ...

    class Redis:
        def pipeline(self, transaction: bool) -> Pipeline:
            return Pipeline()

    def kubectl(*args: str) -> str:
        assert args[:5] == ("exec", "deployment/redis", "--", "redis-cli", "GET")
        return saved[args[5]] + "\n"

    monkeypatch.setattr("load_generator.record.kubectl", kubectl)
    run = RunRecord(
        run_id=RUN_ID,
        status="done",
        task="Q",
        step=1,
        expected_steps=2,
        invalid_calls=0,
        answer="56",
        error=None,
        created_ms=1,
        updated_ms=2,
    )
    call = CallRecord(
        step=1,
        ts_start=1,
        duration_ms=2,
        gap_before_ms=0,
        expected_steps=2,
        prompt_tokens=3,
        completion_tokens=4,
        cached_tokens=0,
        pod="vllm-0",
    )
    store = RedisStore(Redis(), 60)  # type: ignore[arg-type]
    asyncio.run(store.save(run, [], [call]))
    assert redis_calls(RUN_ID) == [call.fields()]


# The experiment file.


def test_the_hash_is_the_first_12_digits_of_the_files_sha256(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_bytes(b"abc")
    assert experiment_hash(path) == "ba7816bf8f01"


def test_another_byte_gives_another_hash(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_bytes(b"abc")
    before = experiment_hash(path)
    path.write_bytes(b"abd")
    assert experiment_hash(path) != before


def test_an_experiment_is_every_question_once_per_seed_in_file_order(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    (tmp_path / "notes.txt").write_text("Notes\n\n## One\nText.\n")
    path = tmp_path / "experiment.yaml"
    path.write_text(
        """
profile: real
seeds: [7, 8]
task_types:
  csv:
    files:
      - {id: data, name: data.csv, path: data.csv}
      - {id: notes, name: notes.txt, type: text, path: notes.txt}
    questions: [Q1, Q2]
  arithmetic:
    questions: [Q3]
"""
    )
    experiment = load_experiment(path)
    assert experiment.profile == "real"
    assert experiment.hash == experiment_hash(path)
    assert [(t.task_type, t.text, t.seed) for t in experiment.tasks] == [
        ("csv", "Q1", 7),
        ("csv", "Q1", 8),
        ("csv", "Q2", 7),
        ("csv", "Q2", 8),
        ("arithmetic", "Q3", 7),
        ("arithmetic", "Q3", 8),
    ]
    assert experiment.tasks[0].files == [
        {"id": "data", "name": "data.csv", "content": "a,b\n1,2\n"},
        {"id": "notes", "name": "notes.txt", "type": "text", "content": "Notes\n\n## One\nText.\n"},
    ]
    assert experiment.tasks[-1].files == []


def test_a_task_goes_out_as_its_run_request() -> None:
    files = [{"id": "data", "name": "data.csv", "content": "a\n1\n"}]
    task = Task(task_type="csv", text="Q", seed=7, files=files)
    assert run_request(task) == {"task": "Q", "seed": 7, "files": files}


# experiments/record-demo.yaml, the file the done-when records.


def test_the_demo_records_the_real_profile_and_the_four_task_types() -> None:
    experiment = load_experiment(DEMO)
    assert experiment.profile == "real"
    assert {task.task_type for task in experiment.tasks} == {
        "csv",
        "contracts",
        "support",
        "arithmetic",
    }


def test_the_demo_has_more_runs_than_the_replayers_done_when_needs() -> None:
    """P2.3's done-when replays 20 runs of more than one step; every task type here calls a tool."""
    assert len(load_experiment(DEMO).tasks) >= 20


def test_every_demo_task_is_a_valid_run_request() -> None:
    schema = json.loads((SCHEMAS / "run-request.v1.json").read_text())
    validator = Draft202012Validator(schema)
    for task in load_experiment(DEMO).tasks:
        assert list(validator.iter_errors(run_request(task))) == []


@pytest.mark.parametrize(
    ("task_type", "tool"),
    [
        ("csv", "csv_summary"),
        ("contracts", "diff_clauses"),
        ("support", "draft_reply"),
        ("arithmetic", "calculator"),
    ],
)
def test_a_demo_tasks_files_bring_its_types_tools(task_type: str, tool: str) -> None:
    """A contract without type: text would be read as a CSV and offer the wrong tools, and the trace
    would hold another scenario's traffic without saying so."""
    config = load_config(REAL_PROFILE)
    tasks = [task for task in load_experiment(DEMO).tasks if task.task_type == task_type]
    for task in tasks:
        tables, documents = read_run_files(task.files, config.max_files, config.max_file_bytes)
        assert tool in offered_tools(config.tool_choice, tables, documents)


# The trace's file.


def experiment_at(path: Path) -> Experiment:
    return Experiment(path=path, hash="aaaabbbbcccc", profile="real", tasks=[])


def test_the_trace_is_named_by_the_experiment_the_agent_and_the_adapter(tmp_path: Path) -> None:
    experiment = experiment_at(tmp_path / "record-demo.yaml")
    assert trace_path(experiment, "thesis/agent:0123456789ab", "5554fcf81b09") == (
        tmp_path / "results/traces/record-demo-aaaabbbbcccc-0123456789ab-5554fcf81b09.jsonl"
    )


def test_a_profile_without_an_adapter_leaves_it_out_of_the_name(tmp_path: Path) -> None:
    experiment = experiment_at(tmp_path / "record-demo.yaml")
    assert trace_path(experiment, "thesis/agent:0123456789ab", None) == (
        tmp_path / "results/traces/record-demo-aaaabbbbcccc-0123456789ab.jsonl"
    )


def test_the_adapter_is_the_start_of_its_sha256() -> None:
    assert adapter_id({"lora": {"sha256": "5554fcf81b093dd231e3"}}) == "5554fcf81b09"
    assert adapter_id({"name": "a model without an adapter"}) is None


def test_the_written_trace_has_one_json_line_per_line(tmp_path: Path) -> None:
    lines = trace_lines(RUN_ID, "csv", calls_of(2))
    path = tmp_path / "results/traces/a.jsonl"
    write_trace(path, lines)
    assert [json.loads(line) for line in path.read_text().splitlines()] == lines
    assert path.read_text().endswith("\n")


def test_a_trace_that_appeared_since_the_check_is_not_replaced(tmp_path: Path) -> None:
    """The check before the start is one recording old by then, and a second make record may have
    written."""
    path = tmp_path / "a.jsonl"
    path.write_text("the other recording\n")
    with pytest.raises(FileExistsError):
        write_trace(path, trace_lines(RUN_ID, "csv", calls_of(2)))
    assert path.read_text() == "the other recording\n"


def test_every_committed_trace_still_fits_trace_v1() -> None:
    """A later change to the schema must not leave a trace P2.3 replays invalid without saying so
    (D12)."""
    traces = sorted((REPO_ROOT / "experiments/results/traces").glob("*.jsonl"))
    assert traces
    validator = trace_validator()
    for trace in traces:
        for line in trace.read_text().splitlines():
            check_line(validator, json.loads(line))


# Before a recording starts.


def test_a_recording_starts_on_the_profiles_agent_with_no_trace_yet(tmp_path: Path) -> None:
    check_before_start("real", "the profile\n", "the profile\n", tmp_path / "a.jsonl")


def test_an_agent_on_another_config_stops_it_before_the_first_run(tmp_path: Path) -> None:
    with pytest.raises(RecordError, match="make up PROFILE=real"):
        check_before_start("real", "another profile\n", "the profile\n", tmp_path / "a.jsonl")


def test_an_existing_trace_is_never_written_over(tmp_path: Path) -> None:
    trace = tmp_path / "a.jsonl"
    trace.write_text("the earlier recording\n")
    with pytest.raises(RecordError, match="exists"):
        check_before_start("real", "the profile\n", "the profile\n", trace)
    assert trace.read_text() == "the earlier recording\n"


# One run.


def test_a_run_is_sent_as_its_run_request() -> None:
    agent = FakeAgent(run_body())
    record(agent, lambda run_id: calls_of(2))
    assert agent.posted == [run_request(TASK)]


def test_a_run_is_polled_until_it_ends() -> None:
    agent = FakeAgent(run_body("running", 0), run_body("running", 1), run_body())
    recorded = record(agent, lambda run_id: calls_of(2))
    assert agent.gets == 3
    assert [line["step"] for line in recorded.lines] == [1, 2]


def test_the_lines_carry_the_runs_id_and_the_tasks_type() -> None:
    recorded = record(FakeAgent(run_body()), lambda run_id: calls_of(2))
    assert {(line["run_id"], line["task_type"]) for line in recorded.lines} == {(RUN_ID, "support")}


def test_the_calls_are_read_for_the_run_the_agent_named() -> None:
    asked: list[str] = []

    def read_calls(run_id: str) -> list[dict[str, Any]]:
        asked.append(run_id)
        return calls_of(2)

    record(FakeAgent(run_body()), read_calls)
    assert asked == [RUN_ID]


def test_the_calls_are_read_only_after_the_run_ended() -> None:
    """Only the final save holds every call, written in one transaction with the status
    (agent_runtime.store.RedisStore), so a read before it would miss the last steps."""
    agent = FakeAgent(run_body("running", 0), run_body("running", 1), run_body())
    gets_at_read: list[int] = []

    def read_calls(run_id: str) -> list[dict[str, Any]]:
        gets_at_read.append(agent.gets)
        return calls_of(2)

    record(agent, read_calls)
    assert gets_at_read == [3]


@pytest.mark.parametrize(
    "error",
    [
        "3 invalid calls (agent.max_invalid_calls)",
        "10 calls without an answer (agent.max_steps)",
        "the reply was cut off at model.max_model_len (finish_reason length)",
    ],
)
def test_a_run_the_model_failed_stays_in_the_trace(error: str) -> None:
    """A real agent makes that traffic too (card 25)."""
    agent = FakeAgent(run_body("failed", 2, error))
    assert len(record(agent, lambda run_id: calls_of(2)).lines) == 2


GATEWAY = httpx.Request("POST", "http://gateway/v1/chat/completions")


def error_of_a_call_without_a_reply(failure: httpx.Response | httpx.HTTPError) -> str:
    """The error the agent's own loop ends a run with when its LLM call fails as failure says, so
    these tests follow the agent's wording instead of a copy of it."""

    def answer(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, httpx.HTTPError):
            raise failure
        return failure

    config = load_config(REAL_PROFILE)

    async def failed_run() -> str | None:
        transport = httpx.MockTransport(answer)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
            result = await run_task("Hi", RUN_ID, 1, config, LLMClient(http, config))
        return result.error

    error = asyncio.run(failed_run())
    assert error is not None
    return error


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(httpx.Response(503, request=GATEWAY), id="503"),
        pytest.param(httpx.Response(400, request=GATEWAY), id="400"),
        pytest.param(httpx.ConnectError("connection refused"), id="no connection"),
        pytest.param(httpx.ReadTimeout("timed out"), id="timeout"),
        pytest.param(httpx.Response(200, json={}, request=GATEWAY), id="no chat completion"),
    ],
)
def test_a_call_without_a_reply_stops_the_recording(
    failure: httpx.Response | httpx.HTTPError,
) -> None:
    """It got no record, so the trace would miss a call the run made."""
    error = error_of_a_call_without_a_reply(failure)
    agent = FakeAgent(run_body("failed", 1, error))
    with pytest.raises(RecordError, match=f"{RUN_ID}.*no reply"):
        record(agent, lambda run_id: calls_of(1))


def test_a_run_that_does_not_end_stops_the_recording() -> None:
    agent = FakeAgent(run_body("running", 0))
    with pytest.raises(RecordError, match=f"{RUN_ID} still running"):
        record(agent, lambda run_id: calls_of(1), run_timeout=0)


def test_calls_that_differ_from_the_runs_steps_stop_the_recording() -> None:
    with pytest.raises(RecordError, match=RUN_ID):
        record(FakeAgent(run_body(steps=3)), lambda run_id: calls_of(2))


def test_an_invalid_line_stops_the_recording() -> None:
    def read_calls(run_id: str) -> list[dict[str, Any]]:
        return [
            {name: value for name, value in call.items() if name != "prompt_tokens"}
            for call in calls_of(2)
        ]

    with pytest.raises(RecordError, match=r"not a trace\.v1 line"):
        record(FakeAgent(run_body()), read_calls)


def test_an_agent_refusing_the_run_stops_the_recording() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "not a run-request.v1"})

    client = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://agent")
    with pytest.raises(httpx.HTTPStatusError):
        record_run(client, TASK, lambda run_id: [], trace_validator(), 60, 0)


# Finding the agent.


def pod(ip: str, *, ready: bool = True, deleting: bool = False) -> dict[str, Any]:
    metadata = {"deletionTimestamp": "2026-11-02T10:00:00Z"} if deleting else {}
    condition = {"type": "Ready", "status": "True" if ready else "False"}
    return {"metadata": metadata, "status": {"podIP": ip, "conditions": [condition]}}


def test_the_agent_is_its_one_ready_pod() -> None:
    pods = {
        "items": [pod("10.0.0.1", ready=False), pod("10.0.0.2", deleting=True), pod("10.0.0.3")]
    }
    assert ready_pod_ip(pods) == "10.0.0.3"


@pytest.mark.parametrize(
    "items",
    [
        pytest.param([], id="no pod"),
        pytest.param([pod("10.0.0.1", ready=False)], id="none ready"),
        pytest.param([pod("10.0.0.1"), pod("10.0.0.2")], id="two ready"),
    ],
)
def test_anything_else_stops_the_recording(items: list[dict[str, Any]]) -> None:
    with pytest.raises(RecordError, match="one Ready agent pod"):
        ready_pod_ip({"items": items})


# What the recording prints at the end: the numbers experiments/log.md keeps.


def recorded(task_type: str, steps: int, status: str = "done", run_id: str = RUN_ID) -> RecordedRun:
    task = Task(task_type=task_type, text="Q", seed=1, files=[])
    lines = trace_lines(run_id, task_type, calls_of(steps))
    return RecordedRun(task=task, run=run_body(status, steps), lines=lines)


def test_the_summary_counts_runs_lines_and_steps_per_task_type() -> None:
    runs = [
        recorded("csv", 2),
        recorded("csv", 3),
        recorded("support", 1, "failed"),
        recorded("csv", 2),
    ]
    assert summary(runs) == [
        "csv: 3 runs, 0 failed, 7 lines, 2-3 steps per run",
        "support: 1 runs, 1 failed, 1 lines, 1-1 steps per run",
        "all: 4 runs, 3 with more than one step, 8 lines, 8 steps in GET /runs/{id}",
    ]


# A whole recording: main, with kubectl, Redis and the agent's HTTP faked. The guarantees decided on
# the card (never written over, no partial trace) hold here, not only in the helpers.

REAL_TEXT = REAL_PROFILE.read_text()
IMAGE = "thesis/agent:0123456789ab"


def experiment_file(directory: Path) -> Path:
    path = directory / "experiment.yaml"
    path.write_text(
        """
profile: real
seeds: [1]
task_types:
  arithmetic:
    questions: [Q1, Q2]
"""
    )
    return path


def trace_of(experiment: Path) -> Path:
    """Where main writes this experiment's trace."""
    model = yaml.safe_load(REAL_TEXT)["model"]
    return trace_path(load_experiment(experiment), IMAGE, adapter_id(model))


def fake_cluster(
    monkeypatch: pytest.MonkeyPatch,
    experiment: Path,
    agent: FakeAgent,
    read_calls: Callable[[str], list[dict[str, Any]]],
    running: str = REAL_TEXT,
) -> None:
    def kubectl(*args: str) -> str:
        answers = {
            "configmap": running,
            "deployment": IMAGE,
            "pods": json.dumps({"items": [pod("10.0.0.1")]}),
        }
        return answers[args[1]]

    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(agent.handle), **kwargs)

    monkeypatch.setattr("load_generator.record.kubectl", kubectl)
    monkeypatch.setattr("load_generator.record.redis_calls", read_calls)
    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr("sys.argv", ["record", str(experiment)])


def test_a_recording_writes_every_runs_lines_once_after_the_last_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment = experiment_file(tmp_path)
    trace = trace_of(experiment)
    agent = FakeAgent(run_body())
    exists_at_read: list[bool] = []

    def read_calls(run_id: str) -> list[dict[str, Any]]:
        exists_at_read.append(trace.exists())
        return calls_of(2)

    fake_cluster(monkeypatch, experiment, agent, read_calls)
    main()
    assert [body["task"] for body in agent.posted] == ["Q1", "Q2"]
    assert exists_at_read == [False, False]
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [line["step"] for line in lines] == [1, 2, 1, 2]


def test_a_recording_that_stops_midway_leaves_no_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment = experiment_file(tmp_path)
    answers = iter([calls_of(2), calls_of(1)])
    agent = FakeAgent(run_body())
    fake_cluster(monkeypatch, experiment, agent, lambda run_id: next(answers))
    with pytest.raises(RecordError):
        main()
    assert len(agent.posted) == 2
    assert not trace_of(experiment).exists()


def test_an_existing_trace_stops_the_recording_before_any_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment = experiment_file(tmp_path)
    trace = trace_of(experiment)
    trace.parent.mkdir(parents=True)
    trace.write_text("the earlier recording\n")
    agent = FakeAgent(run_body())
    fake_cluster(monkeypatch, experiment, agent, lambda run_id: calls_of(2))
    with pytest.raises(RecordError, match="exists"):
        main()
    assert agent.posted == []
    assert trace.read_text() == "the earlier recording\n"


def test_an_agent_on_another_config_stops_the_recording_before_any_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment = experiment_file(tmp_path)
    agent = FakeAgent(run_body())
    fake_cluster(monkeypatch, experiment, agent, lambda run_id: calls_of(2), "another profile\n")
    with pytest.raises(RecordError, match="make up PROFILE=real"):
        main()
    assert agent.posted == []
    assert not trace_of(experiment).exists()


def test_a_run_may_take_max_steps_calls_of_the_longest_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment = experiment_file(tmp_path)
    fake_cluster(monkeypatch, experiment, FakeAgent(run_body()), lambda run_id: calls_of(2))
    timeouts: list[float] = []

    def spy(
        client: httpx.Client,
        task: Task,
        read_calls: Callable[[str], list[dict[str, Any]]],
        validator: Draft202012Validator,
        run_timeout: float,
    ) -> RecordedRun:
        timeouts.append(run_timeout)
        return record_run(client, task, read_calls, validator, run_timeout, 0)

    monkeypatch.setattr("load_generator.record.record_run", spy)
    main()
    agent = yaml.safe_load(REAL_TEXT)["agent"]
    assert timeouts == [agent["max_steps"] * agent["llm_timeout_seconds"]] * 2
