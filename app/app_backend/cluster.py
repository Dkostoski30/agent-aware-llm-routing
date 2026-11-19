"""GET /cluster (P3.11): the vLLM pods, their queues and the cluster's remaining work, for the
UI's cluster panel, as schemas/cluster.v1.json.

Read from Prometheus with instant queries, the queue ones word for word the Grafana panels', so
the panel and Grafana differ by at most one scrape. Prometheus has no login, so it is reached only
inside the cluster, and the browser asks the app backend instead. The pod list comes from
kube-state-metrics, so a pod shows before its first vLLM scrape and leaves when Kubernetes drops it.

Sync httpx, like the runtime client: the endpoint runs in FastAPI's threadpool. from_env builds the
client on PROMETHEUS_URL; tests build it on an httpx.MockTransport.
"""

import logging
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app_backend import logs

PodState = Literal["ready", "starting", "draining", "failed"]

# Pods of the vllm Deployment (real and sim charts alike): vllm-<ReplicaSet hash>-<suffix>.
# PromQL regexes match the whole label, so the picker's vllm-pool-epp-<hash>-<suffix> does not.
VLLM_PODS = 'namespace="default",pod=~"vllm-[a-z0-9]+-[a-z0-9]+"'
# kube-state-metrics keeps a series per phase, 1 for the pod's current one.
PHASE_QUERY = f"kube_pod_status_phase{{{VLLM_PODS}}} == 1"
READY_QUERY = f'kube_pod_status_ready{{{VLLM_PODS},condition="true"}} == 1'
DELETING_QUERY = f"kube_pod_deletion_timestamp{{{VLLM_PODS}}}"
# The Grafana queue panel's query (infra/charts/*/dashboards/vllm.json), and its running twin.
WAITING_QUERY = "sum by (pod) (vllm:num_requests_waiting)"
RUNNING_QUERY = "sum by (pod) (vllm:num_requests_running)"
REMAINING_QUERY = "sum(thesis_remaining_work)"
ACTIVE_QUERY = "sum(thesis_active_runs)"
PROMETHEUS_DOWN = "Prometheus did not answer"

logger = logging.getLogger(__name__)


class PrometheusUnavailable(Exception):
    """No answer to use: the connection failed, timed out, or Prometheus answered 5xx."""


class Pod(BaseModel):
    name: str
    state: PodState
    queue: int | None
    running: int | None


class Cluster(BaseModel):
    remaining_work: int
    active_runs: int
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


class ClusterReader:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def _instant(self, query: str) -> list[tuple[dict[str, str], float]]:
        """One instant query's vector as (labels, value) pairs. A 4xx is a query this module got
        wrong, so it raises as a bug rather than a Prometheus outage."""
        try:
            response = self._http.get("/api/v1/query", params={"query": query})
        except httpx.HTTPError as error:
            raise PrometheusUnavailable(f"{query}: {error!r}") from error
        if response.is_server_error:
            raise PrometheusUnavailable(f"{query}: {response.status_code}")
        response.raise_for_status()
        return [
            (sample["metric"], float(sample["value"][1]))
            for sample in response.json()["data"]["result"]
        ]

    def _by_pod(self, query: str) -> dict[str, int]:
        return {labels["pod"]: int(value) for labels, value in self._instant(query)}

    def _total(self, query: str) -> int:
        """A sum over no series is an empty vector, which here means none active: 0."""
        return sum(int(value) for _, value in self._instant(query))

    def read(self, poll_seconds: int) -> Cluster:
        phases = {labels["pod"]: labels["phase"] for labels, _ in self._instant(PHASE_QUERY)}
        ready = self._by_pod(READY_QUERY)
        deleting = self._by_pod(DELETING_QUERY)
        waiting = self._by_pod(WAITING_QUERY)
        running = self._by_pod(RUNNING_QUERY)
        pods = [
            Pod(
                name=name,
                state=pod_state(phases[name], ready=name in ready, deleting=name in deleting),
                queue=waiting.get(name),
                running=running.get(name),
            )
            for name in sorted(phases)
        ]
        return Cluster(
            remaining_work=self._total(REMAINING_QUERY),
            active_runs=self._total(ACTIVE_QUERY),
            poll_seconds=poll_seconds,
            pods=pods,
        )


def router(reader: ClusterReader, poll_seconds: int, use_user_id: Any) -> APIRouter:
    """GET /cluster for logged-in users. use_user_id is app.py's dependency (401 without a
    session); the id itself is not needed, since every user sees the same cluster."""
    cluster = APIRouter()
    UserId = Annotated[int, use_user_id]

    @cluster.get("/cluster", response_model=Cluster)
    def read_cluster(_: UserId) -> Cluster | JSONResponse:
        """503 when Prometheus cannot answer, with the poll interval, so the UI says the numbers
        are missing instead of showing zeros, and still knows when to ask again."""
        try:
            return reader.read(poll_seconds)
        except PrometheusUnavailable as error:
            logs.log(logger, logging.WARNING, "prometheus unavailable", error=str(error))
            return JSONResponse(
                status_code=503,
                content={"detail": PROMETHEUS_DOWN, "poll_seconds": poll_seconds},
            )

    return cluster
