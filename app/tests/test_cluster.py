"""GET /cluster (P3.11): Prometheus's answers mapped to schemas/cluster.v1.json, and what the
panel gets when Prometheus cannot answer.

A fake Prometheus answers each instant query the endpoint sends with the vector a test gives it;
a query it has no answer for fails the test, so the queries themselves are pinned here.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from app_backend.app import create_app
from app_backend.cluster import (
    ACTIVE_QUERY,
    DELETING_QUERY,
    PHASE_QUERY,
    READY_QUERY,
    REMAINING_QUERY,
    RUNNING_QUERY,
    WAITING_QUERY,
    ClusterReader,
    pod_state,
)
from app_backend.config import AppConfig, ScryptParams
from app_backend.runtime import RuntimeClient
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

CLUSTER_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "cluster.v1.json"
POLL_SECONDS = 5
PASSWORD = "a long enough password"
POD_A = "vllm-568fc4b669-c9kb2"
POD_B = "vllm-568fc4b669-fgdgm"

Vector = list[tuple[dict[str, str], float]]


def sample(labels: dict[str, str], value: float) -> dict[str, Any]:
    return {"metric": labels, "value": [1795006690.887, str(value)]}


class FakePrometheus:
    """GET /api/v1/query answered from answers, keyed by the query; down answers 503."""

    def __init__(self) -> None:
        self.answers: dict[str, Vector] = {}
        self.down = False
        self.queries: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, text="Service Unavailable")
        assert request.url.path == "/api/v1/query"
        query = request.url.params["query"]
        self.queries.append(query)
        vector = self.answers[query]
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [sample(labels, value) for labels, value in vector],
                },
            },
        )


def make_config() -> AppConfig:
    return AppConfig(
        session_ttl_seconds=3600,
        scrypt=ScryptParams(n=2, r=1, p=1),
        min_password_length=15,
        cookie_secure=True,
        max_files=5,
        runtime_timeout_seconds=5.0,
        cluster_poll_seconds=POLL_SECONDS,
    )


def no_runtime() -> RuntimeClient:
    """The cluster endpoint never calls the agent runtime; a call would fail the test."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"/cluster called the runtime: {request.method} {request.url}")

    return RuntimeClient(
        httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://agent"),
        httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://agent"),
    )


@pytest.fixture
def prometheus() -> FakePrometheus:
    return FakePrometheus()


@pytest.fixture
def client(tmp_path: Path, prometheus: FakePrometheus) -> TestClient:
    reader = ClusterReader(
        httpx.Client(transport=httpx.MockTransport(prometheus.handle), base_url="http://prometheus")
    )
    app = create_app(make_config(), tmp_path / "app.db", no_runtime(), reader, lambda: 1000.0)
    return TestClient(app, base_url="https://testserver")


def log_in(client: TestClient) -> None:
    credentials = {"email": "ana@example.com", "password": PASSWORD}
    assert client.post("/auth/register", json=credentials).status_code == 201
    assert client.post("/auth/login", json=credentials).status_code == 200


def answer_two_pods(prometheus: FakePrometheus) -> None:
    """POD_A ready with calls, POD_B started but not ready yet and without vLLM metrics."""
    prometheus.answers = {
        PHASE_QUERY: [
            ({"pod": POD_A, "phase": "Running"}, 1),
            ({"pod": POD_B, "phase": "Running"}, 1),
        ],
        READY_QUERY: [({"pod": POD_A}, 1)],
        DELETING_QUERY: [],
        WAITING_QUERY: [({"pod": POD_A}, 3)],
        RUNNING_QUERY: [({"pod": POD_A}, 2)],
        REMAINING_QUERY: [({}, 7)],
        ACTIVE_QUERY: [({}, 4)],
    }


def test_the_cluster_is_mapped_from_prometheus(
    client: TestClient, prometheus: FakePrometheus
) -> None:
    log_in(client)
    answer_two_pods(prometheus)
    response = client.get("/cluster")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "remaining_work": 7,
        "active_runs": 4,
        "poll_seconds": POLL_SECONDS,
        "pods": [
            {"name": POD_A, "state": "ready", "queue": 3, "running": 2},
            {"name": POD_B, "state": "starting", "queue": None, "running": None},
        ],
    }


def test_the_body_follows_the_schema(client: TestClient, prometheus: FakePrometheus) -> None:
    """Contract test (D12): what the app sends is what schemas/cluster.v1.json allows."""
    log_in(client)
    answer_two_pods(prometheus)
    body = client.get("/cluster").json()
    schema = json.loads(CLUSTER_SCHEMA.read_text())
    assert list(Draft202012Validator(schema).iter_errors(body)) == []


def test_pods_come_sorted_by_name(client: TestClient, prometheus: FakePrometheus) -> None:
    log_in(client)
    answer_two_pods(prometheus)
    prometheus.answers[PHASE_QUERY].reverse()
    names = [pod["name"] for pod in client.get("/cluster").json()["pods"]]
    assert names == [POD_A, POD_B]


def test_no_active_runs_reads_as_zero(client: TestClient, prometheus: FakePrometheus) -> None:
    """With no run active the replayer exports no series (P3.8), so the sums are empty vectors."""
    log_in(client)
    answer_two_pods(prometheus)
    prometheus.answers[REMAINING_QUERY] = []
    prometheus.answers[ACTIVE_QUERY] = []
    body = client.get("/cluster").json()
    assert (body["remaining_work"], body["active_runs"]) == (0, 0)


def test_a_pod_being_deleted_is_draining(client: TestClient, prometheus: FakePrometheus) -> None:
    log_in(client)
    answer_two_pods(prometheus)
    prometheus.answers[DELETING_QUERY] = [({"pod": POD_A}, 1795006600)]
    states = {pod["name"]: pod["state"] for pod in client.get("/cluster").json()["pods"]}
    assert states == {POD_A: "draining", POD_B: "starting"}


def test_vllm_metrics_of_a_pod_kubernetes_no_longer_lists_are_left_out(
    client: TestClient, prometheus: FakePrometheus
) -> None:
    """A pod's vLLM series outlive it by one scrape; the pod list comes from Kubernetes."""
    log_in(client)
    answer_two_pods(prometheus)
    prometheus.answers[WAITING_QUERY].append(({"pod": "vllm-568fc4b669-gone1"}, 1))
    names = [pod["name"] for pod in client.get("/cluster").json()["pods"]]
    assert names == [POD_A, POD_B]


def test_prometheus_down_is_503_with_the_poll_interval(
    client: TestClient, prometheus: FakePrometheus
) -> None:
    """The UI says the numbers are missing instead of showing zeros, and still knows when to
    ask again."""
    log_in(client)
    prometheus.down = True
    response = client.get("/cluster")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Prometheus did not answer",
        "poll_seconds": POLL_SECONDS,
    }


def test_prometheus_unreachable_is_503(tmp_path: Path) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    reader = ClusterReader(
        httpx.Client(transport=httpx.MockTransport(unreachable), base_url="http://prometheus")
    )
    app = create_app(make_config(), tmp_path / "app.db", no_runtime(), reader, lambda: 1000.0)
    client = TestClient(app, base_url="https://testserver")
    log_in(client)
    assert client.get("/cluster").status_code == 503


def test_logged_out_is_401_and_asks_prometheus_nothing(
    client: TestClient, prometheus: FakePrometheus
) -> None:
    assert client.get("/cluster").status_code == 401
    assert prometheus.queries == []


@pytest.mark.parametrize(
    ("phase", "ready", "deleting", "state"),
    [
        ("Running", True, False, "ready"),
        ("Running", False, False, "starting"),
        ("Pending", False, False, "starting"),
        ("Failed", False, False, "failed"),
        ("Unknown", False, False, "failed"),
        ("Running", True, True, "draining"),
        ("Failed", False, True, "draining"),
    ],
)
def test_pod_state(phase: str, ready: bool, deleting: bool, state: str) -> None:
    assert pod_state(phase, ready=ready, deleting=deleting) == state
