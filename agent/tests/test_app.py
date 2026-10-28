"""The run API (P1.3a-2): POST /runs, GET /runs/{id}, /healthz, history saved after every step.

The LLM is httpx.MockTransport with scripted replies; the store is an in-memory class behind the
RunStore interface, so these tests need no Redis. TestClient finishes a request's background task
before it returns, so a run is over when POST /runs answers.
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_runtime.app import create_app
from agent_runtime.config import AgentConfig, Sampling
from agent_runtime.store import RunRecord
from fastapi.testclient import TestClient

RUN_REQUEST_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "run-request.v1.json"
TASK = "What is 7 times 8 plus 3?"
EXPECTED_STEPS = 3
SALES_CSV = (Path(__file__).parent / "fixtures" / "sales.csv").read_text()
MAX_FILE_BYTES = len(SALES_CSV)
SALES_FILE = {"id": "sales", "name": "sales.csv", "content": SALES_CSV}


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
        result_decimals=2,
    )


def calc(op: str, a: float, b: float) -> dict[str, Any]:
    call = {"name": "calculator", "arguments": json.dumps({"op": op, "a": a, "b": b})}
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
        self.files: dict[str, list[dict[str, str]]] = {}

    async def save(self, record: RunRecord, messages: list[dict[str, Any]]) -> None:
        self.saves.append((record, list(messages)))

    async def save_files(self, run_id: str, files: list[dict[str, str]]) -> None:
        self.files[run_id] = files

    async def get(self, run_id: str) -> tuple[RunRecord, list[dict[str, Any]]] | None:
        found = [save for save in self.saves if save[0].run_id == run_id]
        return found[-1] if found else None


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
    service = Service([calc("multiply", 7, 8), calc("add", 56, 3), text("59")])
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
    ],
    ids=["too-many", "too-big", "duplicate-id", "bad-csv", "id-not-a-slug"],
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
    service = Service([calc("multiply", 7, 8), calc("add", 56, 3), text("59")])
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
        calc("multiply", 7, 8),
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
                    "arguments": {"op": "multiply", "a": 7, "b": 8},
                    "result": {"result": 56},
                }
            ],
            "error": None,
        }
    ]


def test_steps_show_invalid_calls_and_rejected_answers() -> None:
    broken = text('<tool_call>\n{"name": "calculator", "arguments": {"op": "add"\n</tool_call>')
    service = Service([calc("pow", 2, 3), broken, calc("multiply", 7, 8), text("56")])
    run_id = service.start({"task": TASK})
    run = service.client.get(f"/runs/{run_id}").json()
    assert (run["status"], run["invalid_calls"]) == ("done", 2)
    steps = run["steps"]
    assert [step["step"] for step in steps] == [1, 2, 3, 4]
    first = steps[0]["tool_calls"][0]
    assert first["arguments"] == {"op": "pow", "a": 2, "b": 3}
    assert "'multiply'" in first["result"]["error"]
    assert steps[0]["error"] is None
    assert (steps[1]["tool_calls"], steps[1]["content"]) == ([], broken["content"])
    assert "could not be parsed" in steps[1]["error"]
    assert steps[2]["tool_calls"][0]["result"] == {"result": 56}
    assert (steps[3]["content"], steps[3]["error"]) == ("56", None)
