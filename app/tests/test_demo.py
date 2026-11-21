"""GET /demo, POST /demo/replay and POST /demo/kill (P4.6a): the cluster panel's demo buttons
passed on to the host's demo runner, as schemas/demo.v1.json, and what the UI gets when the
runner cannot answer or the profile has no buttons.

A fake runner answers each request the way a test sets it; what it was asked is pinned here.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from app_backend.app import create_app
from app_backend.cluster import ClusterReader
from app_backend.config import AppConfig, DemoPattern, ScryptParams
from app_backend.demo import RUNNER_DOWN, TOKEN_REFUSED, DemoControls, RunnerClient
from app_backend.runtime import RuntimeClient
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SCHEMA = json.loads((REPO_ROOT / "schemas" / "demo.v1.json").read_text())
POLL_SECONDS = 5
PASSWORD = "a long enough password"
TOKEN = "f" * 64
PATTERNS = (DemoPattern(name="steady", runs=20), DemoPattern(name="burst", runs=30))
POD = "vllm-568fc4b669-c9kb2"
RUNNING: dict[str, Any] = {
    "replay": {
        "pattern": "steady",
        "command": "make replay TRACE=t.jsonl PATTERN=steady PROFILE=real OUT=o.jsonl",
        "started_at": "2026-11-20T21:05:03Z",
        "state": "running",
        "runs_total": 20,
        "runs_finished": 7,
        "calls_failed": 0,
        "exit_code": None,
    },
    "last_kill": None,
}
KILLED: dict[str, Any] = {**RUNNING, "last_kill": {"pod": POD, "at": "2026-11-20T21:07:41Z"}}

Handler = Callable[[httpx.Request], httpx.Response]


class FakeRunner:
    """The runner's API; answer gives each request's response, and requests holds what came."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: Handler = lambda request: httpx.Response(200, json=RUNNING)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


def make_config() -> AppConfig:
    return AppConfig(
        session_ttl_seconds=3600,
        scrypt=ScryptParams(n=2, r=1, p=1),
        min_password_length=15,
        cookie_secure=True,
        max_files=5,
        runtime_timeout_seconds=5.0,
        cluster_poll_seconds=POLL_SECONDS,
        kv_cache_tokens=1000,
        pressure_warn=1.0,
    )


def refusing(name: str) -> httpx.MockTransport:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"/demo called {name}: {request.method} {request.url}")

    return httpx.MockTransport(refuse)


def build_client(tmp_path: Path, demo: DemoControls | None) -> TestClient:
    runtime = RuntimeClient(
        httpx.Client(transport=refusing("the runtime"), base_url="http://agent"),
        httpx.AsyncClient(transport=refusing("the runtime"), base_url="http://agent"),
    )
    prometheus = ClusterReader(
        httpx.Client(transport=refusing("Prometheus"), base_url="http://prometheus")
    )
    app = create_app(make_config(), tmp_path / "app.db", runtime, prometheus, lambda: 1000.0, demo)
    client = TestClient(app, base_url="https://testserver")
    credentials = {"email": "ana@example.com", "password": PASSWORD}
    assert client.post("/auth/register", json=credentials).status_code == 201
    return client


def log_in(client: TestClient) -> None:
    credentials = {"email": "ana@example.com", "password": PASSWORD}
    assert client.post("/auth/login", json=credentials).status_code == 200


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(tmp_path: Path, runner: FakeRunner) -> TestClient:
    http = httpx.Client(transport=httpx.MockTransport(runner.handle), base_url="http://demo-runner")
    client = build_client(tmp_path, DemoControls(PATTERNS, RunnerClient(http, TOKEN)))
    log_in(client)
    return client


@pytest.fixture
def disabled(tmp_path: Path) -> TestClient:
    client = build_client(tmp_path, None)
    log_in(client)
    return client


def assert_follows_schema(body: dict[str, Any]) -> None:
    """Contract test (D12): what the app sends is what schemas/demo.v1.json allows."""
    assert list(Draft202012Validator(DEMO_SCHEMA).iter_errors(body)) == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/demo", None),
        ("POST", "/demo/replay", {"pattern": "steady"}),
        ("POST", "/demo/kill", {"pod": POD}),
    ],
)
def test_nobody_logged_in_gets_401_and_the_runner_is_not_asked(
    tmp_path: Path, runner: FakeRunner, method: str, path: str, body: dict[str, str] | None
) -> None:
    http = httpx.Client(transport=httpx.MockTransport(runner.handle), base_url="http://demo-runner")
    client = build_client(tmp_path, DemoControls(PATTERNS, RunnerClient(http, TOKEN)))
    assert client.request(method, path, json=body).status_code == 401
    assert runner.requests == []


def test_get_demo_shows_the_patterns_and_the_runners_status(
    client: TestClient, runner: FakeRunner
) -> None:
    response = client.get("/demo")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "enabled": True,
        "poll_seconds": POLL_SECONDS,
        "patterns": [{"name": "steady", "runs": 20}, {"name": "burst", "runs": 30}],
        "runner": RUNNING,
        "runner_error": None,
    }
    assert_follows_schema(response.json())
    (request,) = runner.requests
    assert (request.method, request.url.path) == ("GET", "/status")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_profile_without_the_buttons_says_so_and_asks_no_runner(disabled: TestClient) -> None:
    response = disabled.get("/demo")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "poll_seconds": POLL_SECONDS,
        "patterns": [],
        "runner": None,
        "runner_error": None,
    }
    assert_follows_schema(response.json())


@pytest.mark.parametrize(
    ("path", "body"), [("/demo/replay", {"pattern": "steady"}), ("/demo/kill", {"pod": POD})]
)
def test_a_profile_without_the_buttons_refuses_them(
    disabled: TestClient, path: str, body: dict[str, str]
) -> None:
    assert disabled.post(path, json=body).status_code == 404


def connection_refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("All connection attempts failed", request=request)


def timed_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def token_refused(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"detail": "the token is missing or wrong"})


@pytest.mark.parametrize(
    ("answer", "sentence"),
    [(connection_refused, RUNNER_DOWN), (timed_out, RUNNER_DOWN), (token_refused, TOKEN_REFUSED)],
)
def test_a_runner_that_cannot_answer_leaves_runner_null_with_the_reason(
    client: TestClient, runner: FakeRunner, answer: Handler, sentence: str
) -> None:
    runner.answer = answer
    body = client.get("/demo").json()
    assert (body["runner"], body["runner_error"]) == (None, sentence)
    assert body["patterns"] == [{"name": "steady", "runs": 20}, {"name": "burst", "runs": 30}]
    assert_follows_schema(body)


@pytest.mark.parametrize(
    ("path", "body"), [("/demo/replay", {"pattern": "steady"}), ("/demo/kill", {"pod": POD})]
)
@pytest.mark.parametrize(
    ("answer", "sentence"),
    [(connection_refused, RUNNER_DOWN), (timed_out, RUNNER_DOWN), (token_refused, TOKEN_REFUSED)],
)
def test_a_press_the_runner_cannot_take_is_503_with_the_reason(
    client: TestClient,
    runner: FakeRunner,
    path: str,
    body: dict[str, str],
    answer: Handler,
    sentence: str,
) -> None:
    runner.answer = answer
    response = client.post(path, json=body)
    assert response.status_code == 503
    assert response.json() == {"detail": sentence}


def test_start_sends_the_pattern_and_answers_the_new_status(
    client: TestClient, runner: FakeRunner
) -> None:
    runner.answer = lambda request: httpx.Response(202, json=RUNNING)
    response = client.post("/demo/replay", json={"pattern": "steady"})
    assert response.status_code == 202
    assert response.json() == RUNNING
    (request,) = runner.requests
    assert (request.method, request.url.path) == ("POST", "/replay")
    assert json.loads(request.content) == {"pattern": "steady"}
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_kill_sends_the_pod_and_answers_the_new_status(
    client: TestClient, runner: FakeRunner
) -> None:
    runner.answer = lambda request: httpx.Response(202, json=KILLED)
    response = client.post("/demo/kill", json={"pod": POD})
    assert response.status_code == 202
    assert response.json() == KILLED
    (request,) = runner.requests
    assert (request.method, request.url.path) == ("POST", "/kill")
    assert json.loads(request.content) == {"pod": POD}


@pytest.mark.parametrize(
    ("path", "body", "status", "detail"),
    [
        ("/demo/replay", {"pattern": "steady"}, 409, "a replay is running: make replay ..."),
        ("/demo/replay", {"pattern": "tidal"}, 422, "tidal is not one of steady, burst"),
        ("/demo/kill", {"pod": "vllm-gone"}, 404, "no vLLM pod vllm-gone"),
        ("/demo/kill", {"pod": POD}, 502, "kubectl delete failed: Forbidden"),
    ],
)
def test_the_runners_refusals_are_passed_on(
    client: TestClient,
    runner: FakeRunner,
    path: str,
    body: dict[str, str],
    status: int,
    detail: str,
) -> None:
    runner.answer = lambda request: httpx.Response(status, json={"detail": detail})
    response = client.post(path, json=body)
    assert (response.status_code, response.json()) == (status, {"detail": detail})


@pytest.mark.parametrize(
    ("path", "body"),
    [("/demo/replay", {}), ("/demo/replay", {"pattern": 3}), ("/demo/kill", {"name": POD})],
)
def test_a_body_without_its_field_is_refused_before_the_runner(
    client: TestClient, runner: FakeRunner, path: str, body: dict[str, Any]
) -> None:
    assert client.post(path, json=body).status_code == 422
    assert runner.requests == []


def test_a_taken_press_whose_answer_is_not_a_status_is_not_passed_on(
    client: TestClient, runner: FakeRunner
) -> None:
    """What reaches the UI as the runner's new status is demo.v1's runner object, checked here,
    not whatever the runner sent."""
    runner.answer = lambda request: httpx.Response(202, json={"replay": "started"})
    with pytest.raises(ValidationError):
        client.post("/demo/replay", json={"pattern": "steady"})
