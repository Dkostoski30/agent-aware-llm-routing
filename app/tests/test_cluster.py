"""GET /cluster (P3.11): Prometheus's and Kubernetes's answers mapped to schemas/cluster.v1.json,
and what the panel gets when either cannot answer. Cache pressure since P4.11; the pod list from
the Kubernetes API since card 80.

A fake Prometheus answers each instant query the endpoint sends with the vector a test gives it;
a query it has no answer for fails the test, so the queries themselves are pinned here. A fake
Kubernetes API lists the pods a test gives it and keeps what it was asked.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from app_backend.app import create_app
from app_backend.cluster import (
    ACTIVE_QUERY,
    CONTEXT_QUERY,
    REMAINING_QUERY,
    RUNNING_QUERY,
    WAITING_QUERY,
    ClusterReader,
    PodLister,
    pod_state,
)
from app_backend.config import AppConfig, ScryptParams
from app_backend.runtime import RuntimeClient
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CLUSTER_SCHEMA = REPO_ROOT / "schemas" / "cluster.v1.json"
DASHBOARDS = [
    REPO_ROOT / "infra" / "charts" / chart / "dashboards" / "vllm.json" for chart in ("vllm", "sim")
]
POLL_SECONDS = 5
# One pod's KV cache in tokens and the warning value; round numbers so the pressures below read
# at a glance.
KV_CACHE_TOKENS = 1000
PRESSURE_WARN = 1.25
PASSWORD = "a long enough password"
POD_A = "vllm-568fc4b669-c9kb2"
POD_B = "vllm-568fc4b669-fgdgm"
TOKEN = "service-account-token-1"
# The pod rule Grafana's pressure panel keeps on kube-state-metrics, the one the app keeps on the
# Kubernetes API: Ready and not being deleted.
VLLM_PODS = 'namespace="default",pod=~"vllm-[a-z0-9]+-[a-z0-9]+"'
READY_QUERY = f'kube_pod_status_ready{{{VLLM_PODS},condition="true"}} == 1'
DELETING_QUERY = f"kube_pod_deletion_timestamp{{{VLLM_PODS}}}"

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


def k8s_pod(
    name: str, phase: str = "Running", ready: bool = True, deleting: bool = False
) -> dict[str, Any]:
    """A pod as the Kubernetes API lists it, with the fields the app reads."""
    metadata: dict[str, Any] = {"name": name, "namespace": "default"}
    if deleting:
        metadata["deletionTimestamp"] = "2026-11-27T12:00:00Z"
    # A scheduled pod's other conditions are True whether or not it is Ready.
    conditions = [
        {"type": "PodScheduled", "status": "True"},
        {"type": "Ready", "status": "True" if ready else "False"},
    ]
    return {"metadata": metadata, "status": {"phase": phase, "conditions": conditions}}


class FakeKubernetes:
    """GET of the default namespace's pods answered with pods; down answers 503."""

    def __init__(self) -> None:
        self.pods: list[dict[str, Any]] = []
        self.down = False
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(503, text="Service Unavailable")
        assert request.url.path == "/api/v1/namespaces/default/pods"
        assert request.url.params["labelSelector"] == "app.kubernetes.io/name=vllm"
        return httpx.Response(200, json={"kind": "PodList", "items": self.pods})


def make_config() -> AppConfig:
    return AppConfig(
        session_ttl_seconds=3600,
        scrypt=ScryptParams(n=2, r=1, p=1),
        min_password_length=15,
        cookie_secure=True,
        max_files=5,
        runtime_timeout_seconds=5.0,
        cluster_poll_seconds=POLL_SECONDS,
        kv_cache_tokens=KV_CACHE_TOKENS,
        pressure_warn=PRESSURE_WARN,
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
def kubernetes() -> FakeKubernetes:
    return FakeKubernetes()


@pytest.fixture
def token() -> list[str]:
    """The ServiceAccount token in its file; a test may replace it as the kubelet does."""
    return [TOKEN]


def make_reader(
    prometheus: httpx.MockTransport, kubernetes: httpx.MockTransport, token: list[str]
) -> ClusterReader:
    return ClusterReader(
        httpx.Client(transport=prometheus, base_url="http://prometheus"),
        PodLister(
            httpx.Client(transport=kubernetes, base_url="https://kubernetes.default.svc"),
            lambda: token[0],
        ),
    )


@pytest.fixture
def client(
    tmp_path: Path, prometheus: FakePrometheus, kubernetes: FakeKubernetes, token: list[str]
) -> TestClient:
    reader = make_reader(
        httpx.MockTransport(prometheus.handle), httpx.MockTransport(kubernetes.handle), token
    )
    app = create_app(make_config(), tmp_path / "app.db", no_runtime(), reader, lambda: 1000.0)
    return TestClient(app, base_url="https://testserver")


def log_in(client: TestClient) -> None:
    credentials = {"email": "ana@example.com", "password": PASSWORD}
    assert client.post("/auth/register", json=credentials).status_code == 201
    assert client.post("/auth/login", json=credentials).status_code == 200


def answer_two_pods(prometheus: FakePrometheus, kubernetes: FakeKubernetes) -> None:
    """POD_A ready with calls, POD_B started but not ready yet and without vLLM metrics; the
    active runs hold 1500 tokens of context."""
    kubernetes.pods = [k8s_pod(POD_A), k8s_pod(POD_B, ready=False)]
    prometheus.answers = {
        WAITING_QUERY: [({"pod": POD_A}, 3)],
        RUNNING_QUERY: [({"pod": POD_A}, 2)],
        REMAINING_QUERY: [({}, 7)],
        ACTIVE_QUERY: [({}, 4)],
        CONTEXT_QUERY: [({}, 1500)],
    }


def test_the_cluster_is_mapped_from_kubernetes_and_prometheus(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    response = client.get("/cluster")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "remaining_work": 7,
        "active_runs": 4,
        "cache_pressure": 1.5,
        "pressure_warn": PRESSURE_WARN,
        "poll_seconds": POLL_SECONDS,
        "pods": [
            {"name": POD_A, "state": "ready", "queue": 3, "running": 2},
            {"name": POD_B, "state": "starting", "queue": None, "running": None},
        ],
    }


def test_the_body_follows_the_schema(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """Contract test (D12): what the app sends is what schemas/cluster.v1.json allows."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    body = client.get("/cluster").json()
    schema = json.loads(CLUSTER_SCHEMA.read_text())
    assert list(Draft202012Validator(schema).iter_errors(body)) == []


def test_pods_come_sorted_by_name(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods.reverse()
    names = [pod["name"] for pod in client.get("/cluster").json()["pods"]]
    assert names == [POD_A, POD_B]


def test_no_active_runs_reads_as_zero(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """With no run active the replayer exports no series (P3.8), so the sums are empty vectors."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    prometheus.answers[REMAINING_QUERY] = []
    prometheus.answers[ACTIVE_QUERY] = []
    body = client.get("/cluster").json()
    assert (body["remaining_work"], body["active_runs"]) == (0, 0)


def test_a_pod_being_deleted_is_draining(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods[0] = k8s_pod(POD_A, deleting=True)
    states = {pod["name"]: pod["state"] for pod in client.get("/cluster").json()["pods"]}
    assert states == {POD_A: "draining", POD_B: "starting"}


def test_a_failed_pod_is_failed(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods[1] = k8s_pod(POD_B, phase="Failed", ready=False)
    states = {pod["name"]: pod["state"] for pod in client.get("/cluster").json()["pods"]}
    assert states == {POD_A: "ready", POD_B: "failed"}


def test_a_pod_not_yet_scheduled_has_no_conditions_and_is_starting(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods[1] = {"metadata": {"name": POD_B}, "status": {"phase": "Pending"}}
    states = {pod["name"]: pod["state"] for pod in client.get("/cluster").json()["pods"]}
    assert states == {POD_A: "ready", POD_B: "starting"}


def test_a_killed_pod_leaves_and_its_replacement_shows_on_the_next_read(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """The panel follows Kubernetes, not a kube-state-metrics scrape (card 80): a pod force-deleted
    between two reads is gone from the second, and the one replacing it is there, starting, though
    Prometheus still holds the killed pod's vLLM series."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods = [
        k8s_pod(POD_B, ready=False),
        k8s_pod("vllm-568fc4b669-new01", phase="Pending", ready=False),
    ]
    pods = client.get("/cluster").json()["pods"]
    assert [(pod["name"], pod["state"]) for pod in pods] == [
        ("vllm-568fc4b669-fgdgm", "starting"),
        ("vllm-568fc4b669-new01", "starting"),
    ]


def test_the_pods_are_listed_with_the_service_account_token_read_each_time(
    client: TestClient,
    prometheus: FakePrometheus,
    kubernetes: FakeKubernetes,
    token: list[str],
) -> None:
    """The kubelet replaces the token in its file before it expires, so the app reads it for every
    request instead of keeping the one it started with."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    client.get("/cluster")
    token[0] = "service-account-token-2"
    client.get("/cluster")
    assert [r.headers["authorization"] for r in kubernetes.requests] == [
        "Bearer service-account-token-1",
        "Bearer service-account-token-2",
    ]


def test_prometheus_is_asked_only_the_numbers(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """No kube-state-metrics query is left: the pods come from Kubernetes alone."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    client.get("/cluster")
    assert sorted(prometheus.queries) == sorted(
        [WAITING_QUERY, RUNNING_QUERY, REMAINING_QUERY, ACTIVE_QUERY, CONTEXT_QUERY]
    )


def test_vllm_metrics_of_a_pod_kubernetes_no_longer_lists_are_left_out(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """A pod's vLLM series outlive it by one scrape; the pod list comes from Kubernetes."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    prometheus.answers[WAITING_QUERY].append(({"pod": "vllm-568fc4b669-gone1"}, 1))
    names = [pod["name"] for pod in client.get("/cluster").json()["pods"]]
    assert names == [POD_A, POD_B]


def test_prometheus_down_is_503_with_the_poll_interval(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """The UI says the numbers are missing instead of showing zeros, and still knows when to
    ask again."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    prometheus.down = True
    response = client.get("/cluster")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Prometheus did not answer",
        "poll_seconds": POLL_SECONDS,
    }


def test_kubernetes_down_is_503_with_the_poll_interval(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.down = True
    response = client.get("/cluster")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Kubernetes did not answer",
        "poll_seconds": POLL_SECONDS,
    }


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize("down", ["prometheus", "kubernetes"])
def test_a_server_unreachable_is_503(
    tmp_path: Path,
    prometheus: FakePrometheus,
    kubernetes: FakeKubernetes,
    token: list[str],
    down: str,
) -> None:
    answer_two_pods(prometheus, kubernetes)
    transports = {
        "prometheus": httpx.MockTransport(prometheus.handle),
        "kubernetes": httpx.MockTransport(kubernetes.handle),
    }
    transports[down] = httpx.MockTransport(unreachable)
    reader = make_reader(transports["prometheus"], transports["kubernetes"], token)
    app = create_app(make_config(), tmp_path / "app.db", no_runtime(), reader, lambda: 1000.0)
    client = TestClient(app, base_url="https://testserver")
    log_in(client)
    assert client.get("/cluster").status_code == 503


def test_a_service_account_without_its_role_is_a_bug_not_an_outage(
    tmp_path: Path, prometheus: FakePrometheus, token: list[str]
) -> None:
    """A 403 means the manifest's Role is missing or wrong: it raises as the 403 it is, so it is
    never shown as a passing outage the panel waits out."""

    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"kind": "Status", "reason": "Forbidden"})

    reader = make_reader(
        httpx.MockTransport(prometheus.handle), httpx.MockTransport(forbidden), token
    )
    app = create_app(make_config(), tmp_path / "app.db", no_runtime(), reader, lambda: 1000.0)
    client = TestClient(app, base_url="https://testserver")
    log_in(client)
    with pytest.raises(httpx.HTTPStatusError, match="403 Forbidden"):
        client.get("/cluster")


def test_logged_out_is_401_and_asks_neither_server(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    assert client.get("/cluster").status_code == 401
    assert prometheus.queries == []
    assert kubernetes.requests == []


def pressure(client: TestClient) -> float | None:
    value: float | None = client.get("/cluster").json()["cache_pressure"]
    return value


def test_two_ready_pods_hold_twice_the_cache(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods = [k8s_pod(POD_A), k8s_pod(POD_B)]
    assert pressure(client) == 0.75


def test_no_ready_pod_is_no_pressure_not_a_division_by_zero(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods = [k8s_pod(POD_A, ready=False), k8s_pod(POD_B, ready=False)]
    assert pressure(client) is None


def test_no_context_series_is_no_pressure_not_zero(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """Neither the agent runtime nor a replayer is scraped: nothing says how much context there
    is, so the panel must not read 0."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    prometheus.answers[CONTEXT_QUERY] = []
    assert pressure(client) is None


def test_no_context_held_is_zero_pressure(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """The agent runtime exports 0 while no run is active (P4.10): a known 0, not a missing one."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    prometheus.answers[CONTEXT_QUERY] = [({}, 0)]
    assert pressure(client) == 0.0


def test_a_draining_pod_holds_no_share_of_the_cache(
    client: TestClient, prometheus: FakePrometheus, kubernetes: FakeKubernetes
) -> None:
    """Kubernetes still marks a pod being deleted Ready, but the picker sends it nothing new, so
    only the pods the panel shows as ready count (decided 2026-11-20)."""
    log_in(client)
    answer_two_pods(prometheus, kubernetes)
    kubernetes.pods = [k8s_pod(POD_A, deleting=True), k8s_pod(POD_B)]
    assert pressure(client) == 1.5


@pytest.mark.parametrize("dashboard", DASHBOARDS, ids=lambda path: path.parts[-3])
def test_grafanas_pressure_panel_holds_the_same_rule(dashboard: Path) -> None:
    """The panel and Grafana agree within one scrape only if Grafana divides the same sum by the
    same pods: Ready and not being deleted, each holding the profile's KV cache, which the chart
    sets as the hidden dashboard variable kv_cache_tokens."""
    board = json.loads(dashboard.read_text())
    [panel] = [panel for panel in board["panels"] if panel["title"].startswith("Cache pressure")]
    [target] = panel["targets"]
    assert target["expr"] == (
        f"{CONTEXT_QUERY} / (count({READY_QUERY} unless on (pod) {DELETING_QUERY})"
        " * $kv_cache_tokens)"
    )
    [variable] = [v for v in board["templating"]["list"] if v["name"] == "kv_cache_tokens"]
    assert (variable["type"], variable["hide"]) == ("constant", 2)


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


# The app's access to Kubernetes (infra/manifests/app.yaml, card 80)

MANIFEST = REPO_ROOT / "infra" / "manifests" / "app.yaml"
DOWN = REPO_ROOT / "infra" / "down.sh"


def manifest_objects() -> dict[str, dict[str, Any]]:
    return {doc["kind"]: doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc}


def test_the_app_runs_as_a_service_account_that_may_only_list_pods() -> None:
    objects = manifest_objects()
    account = objects["ServiceAccount"]["metadata"]["name"]
    deployment = objects["Deployment"]["spec"]["template"]["spec"]
    assert deployment["serviceAccountName"] == account
    role = objects["Role"]
    assert role["metadata"]["namespace"] == "default"
    assert role["rules"] == [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}]
    binding = objects["RoleBinding"]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": role["metadata"]["name"],
    }
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": account, "namespace": "default"}
    ]


def test_the_app_is_told_where_the_kubernetes_api_is() -> None:
    [container] = manifest_objects()["Deployment"]["spec"]["template"]["spec"]["containers"]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["KUBERNETES_URL"] == "https://kubernetes.default.svc"


def test_make_down_removes_the_apps_access_with_it() -> None:
    """make down deletes the app by name, to keep its claim (P2.10b); the access objects go with
    the Deployment."""
    objects = manifest_objects()
    down = DOWN.read_text()
    for kind, short in (
        ("ServiceAccount", "serviceaccount"),
        ("Role", "role"),
        ("RoleBinding", "rolebinding"),
    ):
        assert f"{short}/{objects[kind]['metadata']['name']}" in down
