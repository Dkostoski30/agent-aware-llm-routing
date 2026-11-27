"""GET /cluster (P3.11): the vLLM pods, their queues, the cluster's remaining work and its cache
pressure (P4.11), for the UI's cluster panel, as schemas/cluster.v1.json.

The numbers are Prometheus instant queries, the queue ones word for word the Grafana panels', so
the panel and Grafana differ by at most one scrape. The pod list is the Kubernetes API's, read with
the app pod's ServiceAccount (infra/manifests/app.yaml: list pods in default, nothing else), so a
pod shows as soon as it is created, before its first vLLM scrape, and leaves as soon as Kubernetes
drops it: a kill shows on the panel's next read, not a kube-state-metrics scrape later, which left
the panel up to ~10 s behind a kill (card 80). Neither server has a login the browser could use,
so the browser asks the app backend.

Sync httpx, like the runtime client: the endpoint runs in FastAPI's threadpool. from_env builds the
clients on PROMETHEUS_URL and KUBERNETES_URL; tests build them on httpx.MockTransport.
"""

import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app_backend import logs
from app_backend.config import AppConfig

PodState = Literal["ready", "starting", "draining", "failed"]

# The vllm Deployment's pods (real and sim charts alike), as make demo-runner's Kill finds them; the
# picker's pods carry another name label.
PODS_PATH = "/api/v1/namespaces/default/pods"
VLLM_SELECTOR = "app.kubernetes.io/name=vllm"
# The Grafana queue panel's query (infra/charts/*/dashboards/vllm.json), and its running twin.
WAITING_QUERY = "sum by (pod) (vllm:num_requests_waiting)"
RUNNING_QUERY = "sum by (pod) (vllm:num_requests_running)"
REMAINING_QUERY = "sum(thesis_remaining_work)"
ACTIVE_QUERY = "sum(thesis_active_runs)"
# The prompt tokens of every active run's latest call (P4.10), the agent runtime's and the
# replayer's alike.
CONTEXT_QUERY = "sum(thesis_context_tokens)"
PROMETHEUS_DOWN = "Prometheus did not answer"
KUBERNETES_DOWN = "Kubernetes did not answer"

logger = logging.getLogger(__name__)


class ClusterUnavailable(Exception):
    """No answer to use: the connection failed, timed out, or the server answered 5xx. detail is
    what the UI is told."""

    def __init__(self, detail: str, error: str) -> None:
        super().__init__(error)
        self.detail = detail


def _get(http: httpx.Client, detail: str, path: str, **kwargs: Any) -> httpx.Response:
    """A GET whose outage raises ClusterUnavailable. A 4xx is a request this module got wrong, or
    a ServiceAccount without its Role, so it raises as a bug rather than an outage."""
    try:
        response = http.get(path, **kwargs)
    except httpx.HTTPError as error:
        raise ClusterUnavailable(detail, f"{path}: {error!r}") from error
    if response.is_server_error:
        raise ClusterUnavailable(detail, f"{path}: {response.status_code}")
    response.raise_for_status()
    return response


class Pod(BaseModel):
    name: str
    state: PodState
    queue: int | None
    running: int | None


class Cluster(BaseModel):
    remaining_work: int
    active_runs: int
    cache_pressure: float | None
    pressure_warn: float
    poll_seconds: int
    pods: list[Pod]


def pod_state(phase: str, *, ready: bool, deleting: bool) -> PodState:
    """D14's state of a pod. A pod being deleted is leaving the pool whatever its phase, and P4.3's
    drain only makes that window longer. Kubernetes marks only a Running pod Ready, so the rest,
    Pending or Running before its readiness probe passes (the model still loading), is starting."""
    if deleting:
        return "draining"
    if phase in ("Failed", "Unknown"):
        return "failed"
    if ready:
        return "ready"
    return "starting"


def cache_pressure(
    context_tokens: float | None, ready_pods: int, kv_cache_tokens: int
) -> float | None:
    """The active runs' context over the KV cache of the ready pods: 1 is a cache full on paper.
    None when nothing exports the context or no pod is ready, so the panel never shows a made-up
    0 and never divides by zero."""
    if context_tokens is None or ready_pods == 0:
        return None
    return context_tokens / (ready_pods * kv_cache_tokens)


class PodLister:
    """The vLLM pods as the Kubernetes API lists them, each with D14's state. token is read for
    every request, since the kubelet replaces the ServiceAccount token in its file before it
    expires."""

    def __init__(self, http: httpx.Client, token: Callable[[], str]) -> None:
        self._http = http
        self._token = token

    def pods(self) -> dict[str, PodState]:
        response = _get(
            self._http,
            KUBERNETES_DOWN,
            PODS_PATH,
            params={"labelSelector": VLLM_SELECTOR},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        states: dict[str, PodState] = {}
        for item in response.json()["items"]:
            status = item["status"]
            # A pod not yet scheduled has no conditions.
            ready = any(
                c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", [])
            )
            deleting = "deletionTimestamp" in item["metadata"]
            states[item["metadata"]["name"]] = pod_state(
                status["phase"], ready=ready, deleting=deleting
            )
        return states


class ClusterReader:
    def __init__(self, http: httpx.Client, kubernetes: PodLister) -> None:
        self._http = http
        self._kubernetes = kubernetes

    def _instant(self, query: str) -> list[tuple[dict[str, str], float]]:
        """One instant query's vector as (labels, value) pairs."""
        response = _get(self._http, PROMETHEUS_DOWN, "/api/v1/query", params={"query": query})
        return [
            (sample["metric"], float(sample["value"][1]))
            for sample in response.json()["data"]["result"]
        ]

    def _by_pod(self, query: str) -> dict[str, int]:
        return {labels["pod"]: int(value) for labels, value in self._instant(query)}

    def _total(self, query: str) -> int:
        """A sum over no series is an empty vector, which here means none active: 0."""
        return sum(int(value) for _, value in self._instant(query))

    def _sum_or_none(self, query: str) -> float | None:
        """A sum over no series means nothing exports it: None, not 0."""
        vector = self._instant(query)
        return sum(value for _, value in vector) if vector else None

    def read(self, config: AppConfig) -> Cluster:
        states = self._kubernetes.pods()
        waiting = self._by_pod(WAITING_QUERY)
        running = self._by_pod(RUNNING_QUERY)
        # A pod's vLLM series outlive it by a scrape; Kubernetes's list decides which pods show.
        pods = [
            Pod(name=name, state=states[name], queue=waiting.get(name), running=running.get(name))
            for name in sorted(states)
        ]
        # The pods shown as ready, so a draining one, still Ready to Kubernetes but sent nothing
        # new by the picker, holds no share of the cache (decided 2026-11-20).
        ready_pods = sum(pod.state == "ready" for pod in pods)
        return Cluster(
            remaining_work=self._total(REMAINING_QUERY),
            active_runs=self._total(ACTIVE_QUERY),
            cache_pressure=cache_pressure(
                self._sum_or_none(CONTEXT_QUERY), ready_pods, config.kv_cache_tokens
            ),
            pressure_warn=config.pressure_warn,
            poll_seconds=config.cluster_poll_seconds,
            pods=pods,
        )


def router(reader: ClusterReader, config: AppConfig, use_user_id: Any) -> APIRouter:
    """GET /cluster for logged-in users. use_user_id is app.py's dependency (401 without a
    session); the id itself is not needed, since every user sees the same cluster."""
    cluster = APIRouter()
    UserId = Annotated[int, use_user_id]

    @cluster.get("/cluster", response_model=Cluster)
    def read_cluster(_: UserId) -> Cluster | JSONResponse:
        """503 when Prometheus or Kubernetes cannot answer, with the poll interval, so the UI says
        the numbers are missing instead of showing zeros, and still knows when to ask again."""
        try:
            return reader.read(config)
        except ClusterUnavailable as error:
            logs.log(
                logger,
                logging.WARNING,
                "cluster unavailable",
                detail=error.detail,
                error=str(error),
            )
            return JSONResponse(
                status_code=503,
                content={"detail": error.detail, "poll_seconds": config.cluster_poll_seconds},
            )

    return cluster
