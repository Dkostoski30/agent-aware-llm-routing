"""Chats, messages and attachments (P3.12a, D7, D13), against a mocked agent runtime.

The runtime is an httpx.MockTransport behind the app's real RuntimeClient, so the tests see the
exact body the app posts to POST /runs and can check it against schemas/run-request.v1.json (D12).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from app_backend.app import create_app
from app_backend.chats import RUN_EXPIRED, RUNTIME_UNREACHABLE
from app_backend.config import AppConfig, ScryptParams
from app_backend.runtime import RuntimeClient
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

RUN_REQUEST_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "run-request.v1.json"
TTL = 3600
MAX_FILES = 2
START = 1_000_000.0
PASSWORD = "a long enough password"


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeRuntime:
    """The agent runtime's POST /runs and GET /runs/{id}. mode: up, refuse (422), error (500) or
    down (the connection fails)."""

    mode: str = "up"
    posted: list[dict[str, Any]] = field(default_factory=list)
    # run_id -> what GET /runs/{id} answers; a run missing here is a 404, as after Redis's TTL.
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    gets: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if self.mode == "error":
            return httpx.Response(500, json={"detail": "internal error"})
        if request.method == "POST" and request.url.path == "/runs":
            if self.mode == "refuse":
                return httpx.Response(422, json={"detail": "files: sales has no header row"})
            body = json.loads(request.content)
            self.posted.append(body)
            run_id = f"run-{len(self.posted)}"
            self.runs[run_id] = {
                "run_id": run_id,
                "status": "running",
                "answer": None,
                "error": None,
            }
            return httpx.Response(202, json={"run_id": run_id})
        if request.method == "GET" and request.url.path.startswith("/runs/"):
            self.gets += 1
            run_id = request.url.path.removeprefix("/runs/")
            if run_id not in self.runs:
                return httpx.Response(404, json={"detail": f"unknown run {run_id}"})
            return httpx.Response(200, json=self.runs[run_id])
        return httpx.Response(404, json={"detail": "no such route"})

    def finish(self, run_id: str, answer: str) -> None:
        self.runs[run_id] |= {"status": "done", "answer": answer}

    def fail(self, run_id: str, error: str) -> None:
        self.runs[run_id] |= {"status": "failed", "error": error}


def make_config() -> AppConfig:
    return AppConfig(
        session_ttl_seconds=TTL,
        scrypt=ScryptParams(n=2, r=1, p=1),
        min_password_length=15,
        cookie_secure=True,
        max_files=MAX_FILES,
        runtime_timeout_seconds=5.0,
    )


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app_factory(tmp_path: Path, runtime: FakeRuntime, clock: FakeClock) -> Any:
    client = RuntimeClient(
        httpx.Client(transport=httpx.MockTransport(runtime.handle), base_url="http://agent")
    )
    return create_app(make_config(), tmp_path / "app.db", client, clock)


def logged_in(app: Any, email: str) -> TestClient:
    client = TestClient(app, base_url="https://testserver")
    credentials = {"email": email, "password": PASSWORD}
    assert client.post("/auth/register", json=credentials).status_code == 201
    assert client.post("/auth/login", json=credentials).status_code == 200
    return client


@pytest.fixture
def ana(app_factory: Any) -> TestClient:
    return logged_in(app_factory, "ana@example.com")


@pytest.fixture
def bob(app_factory: Any) -> TestClient:
    return logged_in(app_factory, "bob@example.com")


def new_chat(client: TestClient, title: str = "Sales") -> int:
    response = client.post("/chats", json={"title": title})
    assert response.status_code == 201, response.text
    chat_id: int = response.json()["id"]
    return chat_id


def a_file(file_id: str, content: str = "region,amount\neast,1\n") -> dict[str, str]:
    return {"id": file_id, "name": f"{file_id}.csv", "type": "csv", "content": content}


def send(
    client: TestClient, chat_id: int, task: str, files: list[dict[str, str]] | None = None
) -> Any:
    body: dict[str, Any] = {"task": task}
    if files is not None:
        body["files"] = files
    return client.post(f"/chats/{chat_id}/messages", json=body)


def sent_ids(body: dict[str, Any]) -> list[str]:
    return [file["id"] for file in body.get("files", [])]


def messages(client: TestClient, chat_id: int) -> list[dict[str, Any]]:
    response = client.get(f"/chats/{chat_id}")
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()["messages"]
    return found


# Chats


def test_a_new_chat_is_listed_newest_first(ana: TestClient, clock: FakeClock) -> None:
    first = new_chat(ana, "first")
    clock.now += 1
    second = new_chat(ana, "second")
    listed = ana.get("/chats").json()
    assert [chat["id"] for chat in listed] == [second, first]
    assert [chat["title"] for chat in listed] == ["second", "first"]


def test_a_chat_needs_a_title(ana: TestClient) -> None:
    assert ana.post("/chats", json={"title": ""}).status_code == 422


def test_a_new_chat_has_no_messages(ana: TestClient) -> None:
    chat_id = new_chat(ana)
    chat = ana.get(f"/chats/{chat_id}").json()
    assert chat["title"] == "Sales"
    assert chat["messages"] == []


def test_another_users_chats_are_not_listed(ana: TestClient, bob: TestClient) -> None:
    new_chat(ana)
    assert bob.get("/chats").json() == []


def test_another_users_chat_is_404_like_a_missing_one(
    ana: TestClient, bob: TestClient, runtime: FakeRuntime
) -> None:
    chat_id = new_chat(ana)
    missing = chat_id + 1000
    assert bob.get(f"/chats/{chat_id}").status_code == 404
    assert bob.get(f"/chats/{missing}").status_code == 404
    assert bob.get(f"/chats/{chat_id}").json() == bob.get(f"/chats/{missing}").json()
    assert send(bob, chat_id, "total by region?").status_code == 404
    assert runtime.posted == []


def test_chats_need_a_session(app_factory: Any) -> None:
    anonymous = TestClient(app_factory, base_url="https://testserver")
    assert anonymous.get("/chats").status_code == 401
    assert anonymous.post("/chats", json={"title": "x"}).status_code == 401


def test_an_expired_session_is_401(ana: TestClient, clock: FakeClock) -> None:
    chat_id = new_chat(ana)
    clock.now = START + TTL
    assert ana.get("/chats").status_code == 401
    assert ana.get(f"/chats/{chat_id}").status_code == 401
    assert send(ana, chat_id, "total by region?").status_code == 401


# Messages


def test_a_message_starts_one_run_and_saves_both_rows(
    ana: TestClient, runtime: FakeRuntime
) -> None:
    chat_id = new_chat(ana)
    response = send(ana, chat_id, "total by region?", [a_file("sales")])
    assert response.status_code == 202, response.text
    ids = response.json()
    assert ids["run_id"] == "run-1"
    assert runtime.posted == [{"task": "total by region?", "files": [a_file("sales")]}]
    user, assistant = messages(ana, chat_id)
    assert (user["id"], user["role"], user["text"], user["status"]) == (
        ids["user_message_id"],
        "user",
        "total by region?",
        "done",
    )
    assert user["run_id"] is None
    assert user["files"] == [
        {"id": "sales", "name": "sales.csv", "type": "csv", "size": len(a_file("sales")["content"])}
    ]
    assert (assistant["id"], assistant["role"], assistant["status"], assistant["run_id"]) == (
        ids["assistant_message_id"],
        "assistant",
        "running",
        "run-1",
    )
    assert assistant["files"] == []


def test_what_the_app_sends_is_a_run_request_v1(ana: TestClient, runtime: FakeRuntime) -> None:
    validator = Draft202012Validator(json.loads(RUN_REQUEST_SCHEMA.read_text()))
    chat_id = new_chat(ana)
    send(ana, chat_id, "no files")
    send(ana, chat_id, "one file", [a_file("sales")])
    terms = {"id": "terms", "name": "terms.md", "type": "text", "content": "## A\nx"}
    send(ana, chat_id, "a text file", [terms])
    assert len(runtime.posted) == 3
    for body in runtime.posted:
        validator.validate(body)


def test_a_message_without_files_sends_none(ana: TestClient, runtime: FakeRuntime) -> None:
    send(ana, new_chat(ana), "2 + 2?")
    assert runtime.posted == [{"task": "2 + 2?"}]


def test_the_size_is_the_contents_utf8_bytes(ana: TestClient) -> None:
    chat_id = new_chat(ana)
    # Skopje in Cyrillic: 6 letters, 12 bytes.
    content = "grad,iznos\n\u0421\u043a\u043e\u043f\u0458\u0435,1\n"
    send(ana, chat_id, "total?", [a_file("sales", content)])
    assert messages(ana, chat_id)[0]["files"][0]["size"] == len(content.encode())


def test_a_later_message_gets_the_chats_earlier_files(
    ana: TestClient, runtime: FakeRuntime
) -> None:
    chat_id = new_chat(ana)
    send(ana, chat_id, "total?", [a_file("sales")])
    send(ana, chat_id, "and the average?")
    assert runtime.posted[1] == {"task": "and the average?", "files": [a_file("sales")]}


def test_a_run_gets_the_newest_files_up_to_max_files(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    send(ana, chat_id, "one", [a_file("a")])
    send(ana, chat_id, "two", [a_file("b")])
    send(ana, chat_id, "three", [a_file("c")])
    send(ana, chat_id, "four")
    assert [sent_ids(body) for body in runtime.posted] == [
        ["a"],
        ["b", "a"],
        ["c", "b"],
        ["c", "b"],
    ]


def test_a_messages_own_files_come_first_in_its_order(
    ana: TestClient, runtime: FakeRuntime
) -> None:
    chat_id = new_chat(ana)
    send(ana, chat_id, "one", [a_file("a")])
    send(ana, chat_id, "two", [a_file("b"), a_file("c")])
    assert sent_ids(runtime.posted[1]) == ["b", "c"]


def test_more_files_than_max_files_in_one_message_is_422_and_nothing_is_sent(
    ana: TestClient, runtime: FakeRuntime
) -> None:
    chat_id = new_chat(ana)
    response = send(ana, chat_id, "too many", [a_file(f"f{n}") for n in range(MAX_FILES + 1)])
    assert response.status_code == 422
    assert runtime.posted == []
    assert messages(ana, chat_id) == []


def test_a_run_never_gets_another_chats_files(
    ana: TestClient, bob: TestClient, runtime: FakeRuntime
) -> None:
    send(ana, new_chat(ana, "first"), "total?", [a_file("sales")])
    send(ana, new_chat(ana, "second"), "anything attached?")
    send(bob, new_chat(bob), "anything attached?")
    assert [sent_ids(body) for body in runtime.posted] == [["sales"], [], []]


def test_a_repeated_file_id_in_a_chat_gets_a_number(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    send(ana, chat_id, "one", [a_file("sales")])
    send(ana, chat_id, "two", [a_file("sales")])
    send(ana, chat_id, "three", [a_file("sales"), a_file("sales")])
    assert [sent_ids(body) for body in runtime.posted] == [
        ["sales"],
        ["sales-2", "sales"],
        ["sales-3", "sales-4"],
    ]
    names = [file["name"] for message in messages(ana, chat_id) for file in message["files"]]
    assert names == ["sales.csv"] * 4


def test_the_same_file_id_in_another_chat_keeps_its_id(
    ana: TestClient, runtime: FakeRuntime
) -> None:
    send(ana, new_chat(ana, "first"), "one", [a_file("sales")])
    send(ana, new_chat(ana, "second"), "two", [a_file("sales")])
    assert [sent_ids(body) for body in runtime.posted] == [["sales"], ["sales"]]


def test_a_numbered_id_stays_within_64_characters(ana: TestClient, runtime: FakeRuntime) -> None:
    long_id = "a" * 61 + "-bc"
    chat_id = new_chat(ana)
    send(ana, chat_id, "one", [a_file(long_id)])
    send(ana, chat_id, "two", [a_file(long_id)])
    renamed = sent_ids(runtime.posted[1])[0]
    # The id cut to make room for "-2" would end in a dash, which the slug rule forbids.
    assert renamed == "a" * 61 + "-2"


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "Sales.csv", "name": "Sales.csv", "type": "csv", "content": "a\n1\n"},
        {"id": "sales", "name": "sales.pdf", "type": "pdf", "content": "a\n1\n"},
        {"id": "sales", "name": "", "type": "csv", "content": "a\n1\n"},
        {"id": "sales", "name": "sales.csv", "type": "csv"},
        {"id": "sales", "name": "sales.csv", "type": "csv", "content": "a\n1\n", "extra": 1},
    ],
    ids=["id-not-a-slug", "unknown-type", "empty-name", "no-content", "unknown-field"],
)
def test_a_file_the_run_request_would_not_allow_is_422(
    ana: TestClient, runtime: FakeRuntime, bad: dict[str, Any]
) -> None:
    chat_id = new_chat(ana)
    assert send(ana, chat_id, "total?", [bad]).status_code == 422
    assert runtime.posted == []


def test_an_empty_task_is_422(ana: TestClient, runtime: FakeRuntime) -> None:
    assert send(ana, new_chat(ana), "").status_code == 422
    assert runtime.posted == []


def test_a_run_the_runtime_refuses_saves_nothing(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    runtime.mode = "refuse"
    response = send(ana, chat_id, "total?", [a_file("sales")])
    assert response.status_code == 422
    assert response.json()["detail"] == "files: sales has no header row"
    assert messages(ana, chat_id) == []
    runtime.mode = "up"
    send(ana, chat_id, "and now?")
    assert sent_ids(runtime.posted[0]) == []


@pytest.mark.parametrize("mode", ["down", "error"])
def test_an_unreachable_runtime_is_502_with_a_failed_assistant_row(
    ana: TestClient, runtime: FakeRuntime, mode: str
) -> None:
    chat_id = new_chat(ana)
    runtime.mode = mode
    assert send(ana, chat_id, "total?", [a_file("sales")]).status_code == 502
    user, assistant = messages(ana, chat_id)
    assert (user["text"], user["status"]) == ("total?", "done")
    assert [file["id"] for file in user["files"]] == ["sales"]
    assert (assistant["status"], assistant["text"], assistant["run_id"]) == (
        "failed",
        RUNTIME_UNREACHABLE,
        None,
    )


# Unfinished rows, finished on read


def test_a_done_run_fills_in_its_answer(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    run_id = send(ana, chat_id, "total?").json()["run_id"]
    runtime.finish(run_id, "east: 1")
    assistant = messages(ana, chat_id)[1]
    assert (assistant["status"], assistant["text"]) == ("done", "east: 1")


def test_a_failed_run_fills_in_its_error(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    run_id = send(ana, chat_id, "total?").json()["run_id"]
    runtime.fail(run_id, "the model made 3 invalid calls")
    assistant = messages(ana, chat_id)[1]
    assert (assistant["status"], assistant["text"]) == ("failed", "the model made 3 invalid calls")


def test_a_run_the_runtime_no_longer_has_is_failed(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    run_id = send(ana, chat_id, "total?").json()["run_id"]
    del runtime.runs[run_id]
    assistant = messages(ana, chat_id)[1]
    assert (assistant["status"], assistant["text"]) == ("failed", RUN_EXPIRED)


def test_a_running_run_stays_running(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    send(ana, chat_id, "total?")
    assistant = messages(ana, chat_id)[1]
    assert (assistant["status"], assistant["text"]) == ("running", "")


def test_an_unreachable_runtime_on_read_leaves_rows_running(
    ana: TestClient, runtime: FakeRuntime
) -> None:
    chat_id = new_chat(ana)
    send(ana, chat_id, "total?")
    runtime.mode = "down"
    assert messages(ana, chat_id)[1]["status"] == "running"


def test_a_finished_row_is_saved_and_not_asked_for_again(
    ana: TestClient, runtime: FakeRuntime
) -> None:
    chat_id = new_chat(ana)
    run_id = send(ana, chat_id, "total?").json()["run_id"]
    runtime.finish(run_id, "east: 1")
    messages(ana, chat_id)
    asked = runtime.gets
    del runtime.runs[run_id]
    assistant = messages(ana, chat_id)[1]
    assert runtime.gets == asked
    assert (assistant["status"], assistant["text"]) == ("done", "east: 1")


def test_only_unfinished_rows_are_asked_for(ana: TestClient, runtime: FakeRuntime) -> None:
    chat_id = new_chat(ana)
    first = send(ana, chat_id, "one").json()["run_id"]
    send(ana, chat_id, "two")
    runtime.finish(first, "1")
    messages(ana, chat_id)
    asked = runtime.gets
    messages(ana, chat_id)
    assert runtime.gets - asked == 1
