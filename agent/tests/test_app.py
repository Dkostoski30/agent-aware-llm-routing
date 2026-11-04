"""The run API (P1.3a-2, P1.4b-1): POST /runs, GET /runs/{id}, its events over SSE, /healthz,
history saved after every step.

The LLM is httpx.MockTransport with scripted replies; the store is an in-memory class behind the
RunStore interface, so these tests need no Redis. TestClient finishes a request's background task
before it returns, so a run is over when POST /runs answers. The live part of the event stream is
tested on run_events with a store that returns a scripted snapshot per read, so a test decides what
each poll sees. Every event a test gets is validated against schemas/run-events.v1.json (D12).
"""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from agent_runtime.app import create_app, run_events
from agent_runtime.config import AgentConfig, Sampling
from agent_runtime.store import CallRecord, RunRecord, RunStatus, RunStore
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

RUN_REQUEST_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "run-request.v1.json"
TASK = "What is 7 times 8 plus 3?"
EXPECTED_STEPS = 3
SALES_CSV = (Path(__file__).parent / "fixtures" / "sales.csv").read_text()
MAX_FILE_BYTES = len(SALES_CSV)
SALES_FILE = {"id": "sales", "name": "sales.csv", "content": SALES_CSV}
TERMS_FILE = {
    "id": "terms",
    "name": "terms.txt",
    "type": "text",
    "content": "## Fees\nTen euros.\n",
}


def make_config() -> AgentConfig:
    return AgentConfig(
        model_name="test-model",
        tool_choice="auto",
        max_steps=5,
        max_invalid_calls=3,
        expected_steps_default=EXPECTED_STEPS,
        llm_timeout_seconds=5,
        history_ttl_seconds=60,
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20),
        system_prompt="You are a calculator agent.",
        max_file_bytes=MAX_FILE_BYTES,
        max_files=2,
        max_result_rows=20,
        max_result_chars=560,
        result_decimals=2,
        max_clause_chars=1600,
        max_diff_changes=20,
        diff_context_words=3,
        events_poll_seconds=0.01,
    )


def calc(expression: str) -> dict[str, Any]:
    call = {"name": "calculator", "arguments": json.dumps({"expression": expression})}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "function": call}],
    }


def text(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "tool_calls": []}


class MemoryStore:
    """Keeps every save, so a test can see the history after each step."""

    def __init__(self) -> None:
        self.saves: list[tuple[RunRecord, list[dict[str, Any]]]] = []
        # The calls each save held, in the order of saves (P2.2a).
        self.calls: list[list[CallRecord]] = []
        self.files: dict[str, list[dict[str, str]]] = {}

    async def save(
        self, record: RunRecord, messages: list[dict[str, Any]], calls: Sequence[CallRecord]
    ) -> None:
        self.saves.append((record, list(messages)))
        self.calls.append(list(calls))

    async def save_files(self, run_id: str, files: list[dict[str, str]]) -> None:
        self.files[run_id] = files

    async def get(self, run_id: str) -> tuple[RunRecord, list[dict[str, Any]]] | None:
        found = [save for save in self.saves if save[0].run_id == run_id]
        return found[-1] if found else None

    async def get_calls(self, run_id: str) -> list[dict[str, Any]]:
        saved = zip(self.saves, self.calls, strict=True)
        found = [calls for save, calls in saved if save[0].run_id == run_id]
        return [call.fields() for call in found[-1]] if found else []


class Service:
    """The app with a scripted LLM (or one failure for every call) and a MemoryStore."""

    def __init__(self, replies: list[dict[str, Any]], failure: httpx.Response | None = None):
        self.replies = list(replies)
        self.failure = failure
        self.requests: list[httpx.Request] = []
        self.store = MemoryStore()
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), base_url="http://gateway"
        )
        schema = json.loads(RUN_REQUEST_SCHEMA.read_text())
        self.client = TestClient(create_app(make_config(), self.store, http, schema))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            return self.failure
        message = self.replies.pop(0)
        choice = {
            "message": message,
            "finish_reason": "tool_calls" if message["tool_calls"] else "stop",
        }
        return httpx.Response(200, json={"choices": [choice]})

    def start(self, body: dict[str, Any]) -> str:
        response = self.client.post("/runs", json=body)
        assert response.status_code == 202
        run_id: str = response.json()["run_id"]
        return run_id


def test_post_starts_a_run_and_get_reads_it_back() -> None:
    service = Service([calc("7*8"), calc("56+3"), text("59")])
    run_id = service.start({"task": TASK, "seed": 7})
    assert re.fullmatch(r"run-[0-9a-f]{12}", run_id)
    response = service.client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    run = response.json()
    assert (run["run_id"], run["status"], run["task"]) == (run_id, "done", TASK)
    assert (run["answer"], run["error"], run["invalid_calls"]) == ("59", None, 0)
    assert len(run["steps"]) == 3
    assert [request.headers["x-run-id"] for request in service.requests] == [run_id] * 3
    assert json.loads(service.requests[0].content)["seed"] == 7


def test_two_runs_get_different_ids() -> None:
    service = Service([text("1"), text("2")])
    assert service.start({"task": TASK}) != service.start({"task": TASK})


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"task": ""},
        {"task": TASK, "seed": "7"},
        {"task": TASK, "max_steps": 5},
        "not json",
    ],
    ids=["no-task", "empty-task", "string-seed", "extra-field", "not-json"],
)
def test_invalid_request_is_rejected_before_a_run_starts(body: dict[str, Any] | str) -> None:
    service = Service([text("59")])
    if isinstance(body, str):
        response = service.client.post(
            "/runs", content=body, headers={"content-type": "application/json"}
        )
    else:
        response = service.client.post("/runs", json=body)
    assert response.status_code == 422
    assert service.store.saves == []
    assert service.requests == []


@pytest.mark.parametrize(
    ("files", "detail_parts"),
    [
        ([SALES_FILE, {**SALES_FILE, "id": "b"}, {**SALES_FILE, "id": "c"}], ["agent.max_files"]),
        ([{**SALES_FILE, "content": SALES_CSV + "x"}], ["agent.max_file_bytes"]),
        ([SALES_FILE, SALES_FILE], ["duplicate file id"]),
        ([{**SALES_FILE, "content": "a,a\n1,2\n"}], ["duplicate column"]),
        ([{**SALES_FILE, "id": "Sales.csv"}], ["not a run-request.v1"]),
        ([{**TERMS_FILE, "content": "Fees: ten euros.\n"}], ["terms:", "no '## ' heading"]),
        ([{**TERMS_FILE, "type": "pdf"}], ["not a run-request.v1"]),
    ],
    ids=[
        "too-many",
        "too-big",
        "duplicate-id",
        "bad-csv",
        "id-not-a-slug",
        "text-without-heading",
        "unknown-type",
    ],
)
def test_file_the_run_cannot_use_is_rejected_before_it_starts(
    files: list[dict[str, str]], detail_parts: list[str]
) -> None:
    service = Service([text("59")])
    response = service.client.post("/runs", json={"task": TASK, "files": files})
    assert response.status_code == 422
    for part in detail_parts:
        assert part in response.json()["detail"]
    assert (service.store.saves, service.store.files, service.requests) == ([], {}, [])


def test_files_are_saved_with_the_run_and_reach_the_tools() -> None:
    arguments = {"file": "sales", "by": "region", "value": "revenue", "agg": "sum"}
    call = {"name": "group_by", "arguments": json.dumps(arguments)}
    group = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "function": call}],
    }
    service = Service([group, text("South")])
    run_id = service.start({"task": TASK, "files": [SALES_FILE]})
    assert service.store.files == {run_id: [SALES_FILE]}
    run = service.client.get(f"/runs/{run_id}").json()
    assert (run["status"], run["answer"]) == ("done", "South")
    result = run["steps"][0]["tool_calls"][0]["result"]
    assert result["rows"][0] == {"group": "South", "sum": 380.0}


def test_text_documents_are_saved_with_the_run_and_reach_the_document_tools() -> None:
    call = {"name": "read_document", "arguments": json.dumps({"file": "terms"})}
    read = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "function": call}],
    }
    service = Service([read, text("One clause: fees.")])
    run_id = service.start({"task": TASK, "files": [SALES_FILE, TERMS_FILE]})
    assert service.store.files == {run_id: [SALES_FILE, TERMS_FILE]}
    tools = [tool["function"]["name"] for tool in json.loads(service.requests[0].content)["tools"]]
    # No calculator on a run with files (P2.1a-3).
    assert tools == [
        "csv_summary",
        "group_by",
        "compare",
        "read_document",
        "extract_clauses",
        # Every text file also brings get_policy (card 21, P2.1a-2), until P4.8's scenario step.
        "get_policy",
    ]
    run = service.client.get(f"/runs/{run_id}").json()
    assert (run["status"], run["answer"]) == ("done", "One clause: fees.")
    result = run["steps"][0]["tool_calls"][0]["result"]
    assert result["clauses"] == [{"clause": "fees", "title": "Fees", "word_count": 2}]


def test_a_run_without_files_saves_an_empty_file_list() -> None:
    service = Service([text("59")])
    run_id = service.start({"task": TASK})
    assert service.store.files == {run_id: []}


def test_unknown_run_is_404() -> None:
    assert Service([]).client.get("/runs/run-000000000000").status_code == 404


def test_healthz() -> None:
    response = Service([]).client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_history_is_saved_after_every_step() -> None:
    service = Service([calc("7*8"), calc("56+3"), text("59")])
    run_id = service.start({"task": TASK})
    saved = [
        (record.status, record.step, record.invalid_calls, record.answer, len(messages))
        for record, messages in service.store.saves
    ]
    assert saved == [
        ("running", 0, 0, None, 2),
        ("running", 1, 0, None, 4),
        ("running", 2, 0, None, 6),
        ("done", 3, 0, "59", 7),
    ]
    records = [record for record, _ in service.store.saves]
    assert {(r.run_id, r.task, r.expected_steps) for r in records} == {
        (run_id, TASK, EXPECTED_STEPS)
    }
    assert len({record.created_ms for record in records}) == 1
    updated = [record.updated_ms for record in records]
    assert updated == sorted(updated)
    final_messages = service.store.saves[-1][1]
    assert final_messages[-1] == {"role": "assistant", "content": "59"}


def test_calls_are_saved_with_every_step() -> None:
    """Each save holds the calls of the steps it saves, none before the first call (P2.2a)."""
    service = Service([calc("7*8"), calc("56+3"), text("59")])
    service.start({"task": TASK})
    saved = [[call.step for call in calls] for calls in service.store.calls]
    assert saved == [[], [1], [1, 2], [1, 2, 3]]


def test_failed_run_is_saved_with_its_error() -> None:
    service = Service([], failure=httpx.Response(500, json={"error": "scripted failure"}))
    run_id = service.start({"task": TASK})
    record, _ = service.store.saves[-1]
    assert (record.status, record.step, record.answer) == ("failed", 1, None)
    assert record.error is not None
    assert "500" in record.error
    run = service.client.get(f"/runs/{run_id}").json()
    assert (run["status"], run["error"]) == ("failed", record.error)


def test_running_run_shows_the_steps_so_far() -> None:
    service = Service([])
    messages = [
        {"role": "system", "content": "You are a calculator agent."},
        {"role": "user", "content": TASK},
        calc("7*8"),
        {"role": "tool", "tool_call_id": "call-1", "content": json.dumps({"result": 56})},
    ]
    record = RunRecord(
        run_id="run-0123456789ab",
        status="running",
        task=TASK,
        step=1,
        expected_steps=EXPECTED_STEPS,
        invalid_calls=0,
        answer=None,
        error=None,
        created_ms=1,
        updated_ms=2,
    )
    service.store.saves.append((record, messages))
    run = service.client.get("/runs/run-0123456789ab").json()
    assert (run["status"], run["answer"]) == ("running", None)
    assert run["steps"] == [
        {
            "step": 1,
            "content": None,
            "tool_calls": [
                {
                    "name": "calculator",
                    "arguments": {"expression": "7*8"},
                    "result": {"result": 56},
                }
            ],
            "error": None,
        }
    ]


def test_steps_show_invalid_calls_and_rejected_answers() -> None:
    broken = text('<tool_call>\n{"name": "calculator", "arguments": {"op": "add"\n</tool_call>')
    service = Service([calc("2^3"), broken, calc("7*8"), text("56")])
    run_id = service.start({"task": TASK})
    run = service.client.get(f"/runs/{run_id}").json()
    assert (run["status"], run["invalid_calls"]) == ("done", 2)
    steps = run["steps"]
    assert [step["step"] for step in steps] == [1, 2, 3, 4]
    first = steps[0]["tool_calls"][0]
    assert first["arguments"] == {"expression": "2^3"}
    assert "does not match" in first["result"]["error"]
    assert steps[0]["error"] is None
    assert (steps[1]["tool_calls"], steps[1]["content"]) == ([], broken["content"])
    assert "could not be parsed" in steps[1]["error"]
    assert steps[2]["tool_calls"][0]["result"] == {"result": 56}
    assert (steps[3]["content"], steps[3]["error"]) == ("56", None)


EVENTS_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "run-events.v1.json").read_text()
)
VALIDATOR = Draft202012Validator(EVENTS_SCHEMA)
RUN_ID = "run-0123456789ab"
CREATED_MS = 1_000

Snapshot = tuple[RunRecord, list[dict[str, Any]]] | None


def checked(events: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """Each event's data is a run-events.v1 of the kind its SSE event name says: every field of a
    kind is required and no other is allowed, so the fields tell the kind."""
    for name, data in events:
        VALIDATOR.validate(data)
        assert set(data) == set(EVENTS_SCHEMA["$defs"][name]["required"])
    return events


def saved_record(step: int, status: RunStatus = "running", answer: str | None = None) -> RunRecord:
    """The run's record after step; every step takes 100 ms."""
    return RunRecord(
        run_id=RUN_ID,
        status=status,
        task=TASK,
        step=step,
        expected_steps=3,
        invalid_calls=0,
        answer=answer,
        error=None,
        created_ms=CREATED_MS,
        updated_ms=CREATED_MS + 100 * step,
    )


def tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": "call-1", "content": json.dumps(result)}


# The saves of a run that multiplies, adds and answers, as the agent writes them.
STEP_0 = [
    {"role": "system", "content": "You are a calculator agent."},
    {"role": "user", "content": TASK},
]
STEP_1 = [*STEP_0, calc("7*8"), tool_result({"result": 56})]
STEP_2 = [*STEP_1, calc("56+3"), tool_result({"result": 59})]
DONE = [*STEP_2, {"role": "assistant", "content": "59"}]


class ScriptedStore:
    """Returns the next snapshot on every read and the last one once the script runs out."""

    def __init__(self, snapshots: list[Snapshot]) -> None:
        self.snapshots = snapshots
        self.reads = 0

    async def get(self, run_id: str) -> Snapshot:
        assert run_id == RUN_ID
        snapshot = self.snapshots[min(self.reads, len(self.snapshots) - 1)]
        self.reads += 1
        return snapshot


async def collect(store: ScriptedStore) -> list[tuple[str, dict[str, Any]]]:
    reader = run_events(cast(RunStore, store), RUN_ID, 0)
    events = [(event.event or "", event.data) async for event in reader]
    return checked(events)


def kinds(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [f"{name} {data['step']}" if name == "step" else name for name, data in events]


@pytest.mark.anyio
async def test_saved_steps_then_live_steps_then_end() -> None:
    store = ScriptedStore(
        [
            (saved_record(0), STEP_0),
            (saved_record(1), STEP_1),
            (saved_record(1), STEP_1),
            (saved_record(2), STEP_2),
            (saved_record(3, "done", "59"), DONE),
        ]
    )
    events = await collect(store)
    assert kinds(events) == ["run", "step 1", "step 2", "step 3", "end"]
    assert store.reads == 5
    assert events[0][1] == {
        "run_id": RUN_ID,
        "task": TASK,
        "expected_steps": 3,
        "created_ms": CREATED_MS,
    }
    assert events[1][1] == {
        "run_id": RUN_ID,
        "step": 1,
        "expected_steps": 3,
        "content": None,
        "tool_calls": [
            {
                "name": "calculator",
                "arguments": {"expression": "7*8"},
                "result": {"result": 56},
            }
        ],
        "error": None,
    }
    assert (events[3][1]["content"], events[3][1]["tool_calls"]) == ("59", [])
    assert events[4][1] == {
        "run_id": RUN_ID,
        "status": "done",
        "answer": "59",
        "error": None,
        "steps": 3,
        "invalid_calls": 0,
        "elapsed_ms": 300,
    }


@pytest.mark.anyio
async def test_a_stream_opened_mid_run_gets_the_earlier_steps_again() -> None:
    # The first stream loses its connection after step 1; here the run expiring ends it.
    first = await collect(ScriptedStore([(saved_record(1), STEP_1), None]))
    reconnect = await collect(
        ScriptedStore([(saved_record(2), STEP_2), (saved_record(3, "done", "59"), DONE)])
    )
    assert kinds(first) == ["run", "step 1"]
    assert kinds(reconnect) == ["run", "step 1", "step 2", "step 3", "end"]
    assert reconnect[1] == first[1]


@pytest.mark.anyio
async def test_a_run_that_expires_mid_stream_closes_without_an_end() -> None:
    store = ScriptedStore([(saved_record(1), STEP_1), None])
    assert kinds(await collect(store)) == ["run", "step 1"]
    assert store.reads == 2


@pytest.mark.anyio
async def test_final_answer_call_has_no_result() -> None:
    final_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call-2", "function": {"name": "final_answer", "arguments": '{"answer": "56"}'}}
        ],
    }
    store = ScriptedStore([(saved_record(2, "done", "56"), [*STEP_1, final_call])])
    events = await collect(store)
    assert kinds(events) == ["run", "step 1", "step 2", "end"]
    assert events[2][1]["tool_calls"] == [
        {"name": "final_answer", "arguments": {"answer": "56"}, "result": None}
    ]


def stream(service: Service, run_id: str) -> list[tuple[str, dict[str, Any]]]:
    response = service.client.get(f"/runs/{run_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for block in response.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return checked(events)


def test_finished_run_streams_its_steps_and_answer_then_closes() -> None:
    service = Service([calc("7*8"), calc("56+3"), text("59")])
    run_id = service.start({"task": TASK})
    events = stream(service, run_id)
    assert kinds(events) == ["run", "step 1", "step 2", "step 3", "end"]
    assert {data["run_id"] for _, data in events} == {run_id}
    end = events[-1][1]
    assert (end["status"], end["answer"], end["steps"]) == ("done", "59", 3)
    assert stream(service, run_id) == events


def test_invalid_calls_reach_the_stream() -> None:
    broken = text('<tool_call>\n{"name": "calculator", "arguments": {"op": "add"\n</tool_call>')
    service = Service([calc("2^3"), broken, calc("7*8"), text("56")])
    events = stream(service, service.start({"task": TASK}))
    steps = [data for name, data in events if name == "step"]
    assert "does not match" in steps[0]["tool_calls"][0]["result"]["error"]
    assert "could not be parsed" in steps[1]["error"]
    assert (events[-1][1]["answer"], events[-1][1]["invalid_calls"]) == ("56", 2)


def test_arguments_that_are_not_json_reach_the_stream_as_text() -> None:
    call = {"name": "calculator", "arguments": '{"op": "add"'}
    broken = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "function": call}],
    }
    service = Service([broken, text("3")])
    events = stream(service, service.start({"task": TASK}))
    [first] = events[1][1]["tool_calls"]
    assert first["arguments"] == '{"op": "add"'
    assert "error" in first["result"]


def test_failed_run_ends_with_its_error() -> None:
    service = Service([], failure=httpx.Response(500, json={"error": "scripted failure"}))
    events = stream(service, service.start({"task": TASK}))
    assert kinds(events) == ["run", "end"]
    end = events[-1][1]
    assert (end["status"], end["answer"], end["steps"]) == ("failed", None, 1)
    assert "500" in end["error"]


def test_events_of_an_unknown_run_is_404() -> None:
    response = Service([]).client.get("/runs/run-000000000000/events")
    assert response.status_code == 404


CALLS_VALIDATOR = Draft202012Validator(
    json.loads((Path(__file__).resolve().parents[2] / "schemas" / "run-calls.v1.json").read_text())
)
POD = "vllm-5c9565db76-6bq8s"


class ServedService(Service):
    """Every reply has the usage and the fingerprint a vLLM pod sends (P2.2a)."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        reply = super().handle(request)
        served = {
            "usage": {
                "prompt_tokens": 398,
                "completion_tokens": 9,
                "prompt_tokens_details": {"cached_tokens": 352},
            },
            "system_fingerprint": POD,
        }
        return httpx.Response(reply.status_code, json={**reply.json(), **served})


def read_calls(service: Service, run_id: str) -> list[dict[str, Any]]:
    response = service.client.get(f"/runs/{run_id}/calls")
    assert response.status_code == 200
    body = response.json()
    CALLS_VALIDATOR.validate(body)
    assert body["run_id"] == run_id
    calls: list[dict[str, Any]] = body["calls"]
    return calls


def test_calls_are_the_records_the_run_saved_in_step_order() -> None:
    service = ServedService([calc("7*8"), text("56")])
    run_id = service.start({"task": TASK})
    [first, second] = read_calls(service, run_id)
    assert [first["step"], second["step"]] == [1, 2]
    assert (first["prompt_tokens"], first["completion_tokens"], first["cached_tokens"]) == (
        398,
        9,
        352,
    )
    assert (first["pod"], first["expected_steps"], first["gap_before_ms"]) == (
        POD,
        EXPECTED_STEPS,
        0,
    )
    assert [first, second] == [call.fields() for call in service.store.calls[-1]]


def test_calls_leave_out_a_measurement_the_reply_did_not_have() -> None:
    service = Service([text("56")])
    [call] = read_calls(service, service.start({"task": TASK}))
    assert set(call) == {"step", "ts_start", "duration_ms", "gap_before_ms", "expected_steps"}


def test_calls_of_a_run_whose_first_call_got_no_reply_are_empty() -> None:
    service = Service([], failure=httpx.Response(500, json={"error": "scripted failure"}))
    assert read_calls(service, service.start({"task": TASK})) == []


def test_calls_of_an_unknown_run_is_404() -> None:
    response = Service([]).client.get("/runs/run-000000000000/calls")
    assert response.status_code == 404
