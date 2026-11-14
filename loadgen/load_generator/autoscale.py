"""KEDA on the vLLM Deployment (P3.9b, D2): the ScaledObject a profile's autoscaling section gives,
the series a replay leaves in Prometheus, and the moments the report reads from them.

At the floor of 1 pod the HPA KEDA makes does all the scaling: it reads the metric every 15 s
(k3s's --horizontal-pod-autoscaler-sync-period, a flag of the whole machine) and wants
ceil(metric / threshold) pods, and changes nothing within 10% of that (its default tolerance).
KEDA's pollingInterval and cooldownPeriod only act on scaling to and from 0, so the object sets
neither (KEDA 2.21's ScaledObject reference, decided 2026-11-14).

A crossing is per pod of the spec replicas, the count the HPA divides by, and past the target, not
at it. The scale-up is the first sample with spec replicas over the floor.
"""

import csv
from collections.abc import Callable, Mapping, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

from load_generator.record import REPO_ROOT

CHART = REPO_ROOT / "infra" / "charts" / "autoscaling"
# Each autoscaler's trigger: one number for the whole Deployment, which KEDA divides by its
# threshold. Remaining work is the run tracker's (P3.8), from the agent runtime and the replayer.
TRIGGER_QUERIES = {
    "remaining": "sum(thesis_remaining_work)",
    "queue": "sum(vllm:num_requests_waiting)",
}
# Each autoscaler's threshold, a key of the profile's autoscaling section.
TARGET_KEYS = {"remaining": "remaining_work_target", "queue": "queue_target"}
# What a replay leaves, each as one series: kube-state-metrics' spec and Ready counts of the vLLM
# Deployment (max() drops the labels; there is one Deployment), and the two triggers.
SERIES_QUERIES = {
    "spec_replicas": 'max(kube_deployment_spec_replicas{namespace="default",deployment="vllm"})',
    "ready_replicas": (
        'max(kube_deployment_status_replicas_ready{namespace="default",deployment="vllm"})'
    ),
    "waiting": TRIGGER_QUERIES["queue"],
    "remaining_work": TRIGGER_QUERIES["remaining"],
}
COUNT_KEYS = ("min_replicas", "max_replicas")
NUMBER_KEYS = ("remaining_work_target", "queue_target")
SECONDS_KEY = "scale_down_stabilization_seconds"


class AutoscaleError(Exception):
    pass


@dataclass(frozen=True)
class AutoscaleRow:
    # Since the replay's start.
    seconds: float
    # None where Prometheus had no sample.
    spec_replicas: float | None
    ready_replicas: float | None
    waiting: float | None
    remaining_work: float | None


@dataclass(frozen=True)
class Crossings:
    # Seconds since the replay's start; None if it never happened.
    remaining_seconds: float | None
    scale_up_seconds: float | None
    queue_seconds: float | None

    def as_meta(self) -> dict[str, float | None]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_section(section: Mapping[str, Any]) -> None:
    """A section KEDA can scale by: a floor of at least 1 pod (decided 2026-11-09), a ceiling over
    it, targets over 0 and a stabilization window of 0 s or more."""
    for key in (*COUNT_KEYS, *NUMBER_KEYS, SECONDS_KEY):
        if key not in section:
            raise AutoscaleError(f"autoscaling has no {key}")
    for key in COUNT_KEYS:
        if not _whole(section[key]) or section[key] < 1:
            raise AutoscaleError(f"autoscaling.{key} must be a whole number of pods from 1 up")
    if section["max_replicas"] <= section["min_replicas"]:
        raise AutoscaleError(
            "autoscaling.max_replicas must be over min_replicas, or nothing scales"
        )
    for key in NUMBER_KEYS:
        value = section[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise AutoscaleError(f"autoscaling.{key} must be a number over 0")
    if not _whole(section[SECONDS_KEY]) or section[SECONDS_KEY] < 0:
        raise AutoscaleError(f"autoscaling.{SECONDS_KEY} must be a whole number from 0 up")


def helm_values(section: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """infra/charts/autoscaling's values for an autoscaler on a profile's section."""
    if kind not in TRIGGER_QUERIES:
        raise AutoscaleError(f"no autoscaler {kind}, there are {', '.join(TRIGGER_QUERIES)}")
    return {
        "minReplicas": section["min_replicas"],
        "maxReplicas": section["max_replicas"],
        "query": TRIGGER_QUERIES[kind],
        "threshold": section[TARGET_KEYS[kind]],
        "scaleDownStabilizationSeconds": section[SECONDS_KEY],
    }


def _samples(name: str, result: Sequence[Mapping[str, Any]]) -> dict[float, float]:
    if len(result) > 1:
        raise AutoscaleError(
            f"{name}: Prometheus answered {len(result)} series, the query adds one"
        )
    if not result:
        return {}
    return {float(time): float(value) for time, value in result[0]["values"]}


def rows_from(
    answers: Mapping[str, Sequence[Mapping[str, Any]]], since: float, until: float
) -> list[AutoscaleRow]:
    """One row per sample time from since to until, from the range answers to SERIES_QUERIES.
    Prometheus places a range query's samples at since plus whole steps, so the series line up."""
    samples = {name: _samples(name, answers[name]) for name in SERIES_QUERIES}
    times = sorted({time for series in samples.values() for time in series})
    return [
        AutoscaleRow(
            round(time - since, 3),
            *(samples[name].get(time) for name in SERIES_QUERIES),
        )
        for time in times
        if since <= time <= until
    ]


def _per_pod(value: float | None, spec: float | None) -> float | None:
    if value is None or spec is None or spec <= 0:
        return None
    return value / spec


def _first(rows: Sequence[AutoscaleRow], passed: Callable[[AutoscaleRow], bool]) -> float | None:
    return next((row.seconds for row in rows if passed(row)), None)


def first_crossings(rows: Sequence[AutoscaleRow], section: Mapping[str, Any]) -> Crossings:
    def over(value: float | None, target: float) -> bool:
        return value is not None and value > target

    return Crossings(
        remaining_seconds=_first(
            rows,
            lambda row: over(
                _per_pod(row.remaining_work, row.spec_replicas), section["remaining_work_target"]
            ),
        ),
        scale_up_seconds=_first(rows, lambda row: over(row.spec_replicas, section["min_replicas"])),
        queue_seconds=_first(
            rows,
            lambda row: over(_per_pod(row.waiting, row.spec_replicas), section["queue_target"]),
        ),
    )


def scale_up_before_queue(crossings: Crossings) -> bool:
    """The done-when's question: a scale-up began, and the queue had not yet passed its target."""
    if crossings.scale_up_seconds is None:
        return False
    return crossings.queue_seconds is None or crossings.scale_up_seconds < crossings.queue_seconds


def _when(seconds: float | None) -> str:
    return "never" if seconds is None else f"at {seconds:.1f} s"


def crossing_text(kind: str, section: Mapping[str, Any], crossings: Crossings) -> str:
    verdict = "began" if scale_up_before_queue(crossings) else "did not begin"
    return (
        f"  autoscaler {kind}: remaining work passed {section['remaining_work_target']} per pod "
        f"{_when(crossings.remaining_seconds)}, spec replicas rose over {section['min_replicas']} "
        f"{_when(crossings.scale_up_seconds)}, the queue passed {section['queue_target']} per pod "
        f"{_when(crossings.queue_seconds)}; the scale-up {verdict} before the queue passed its "
        "target"
    )


def _cell(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if value == int(value) else str(value)


def write_autoscale_csv(path: Path, rows: Sequence[AutoscaleRow]) -> None:
    """Counts as whole numbers, a blank where Prometheus had no sample."""
    with path.open("x", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([field.name for field in fields(AutoscaleRow)])
        for row in rows:
            seconds, *values = astuple(row)
            writer.writerow([str(seconds), *(_cell(value) for value in values)])
