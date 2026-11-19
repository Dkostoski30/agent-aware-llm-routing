"""KEDA on the vLLM Deployment (P3.9b, D2): the ScaledObject a profile's autoscaling section gives,
the series a replay leaves in Prometheus, and the moments the report reads from them. And the
baselines (P3.10): KEDA on the queue, and a plain HPA on the pods' CPU, real pods only (D5), with
the numbers P5.2 compares them by.

At the floor of 1 pod the HPA KEDA makes does all the scaling: it reads the metric every 15 s
(k3s's --horizontal-pod-autoscaler-sync-period, a flag of the whole machine) and wants
ceil(metric / threshold) pods, and changes nothing within 10% of that (its default tolerance).
KEDA's pollingInterval and cooldownPeriod only act on scaling to and from 0, so the object sets
neither (KEDA 2.21's ScaledObject reference, decided 2026-11-14).

A crossing is per pod of the spec replicas, the count the HPA divides by, and past the target, not
at it. The scale-up is the first sample with spec replicas over the floor. A series whose first
count is already over the floor was scaled before the replay, out of its sight: it has no
scale-up time, no verdict against the queue and no time to capacity, and says so (P3.10's review:
a CPU HPA up during the warm-up scaled on its calls; the experiment now starts it after them).

The summary times runs as the call rows do, from each run's planned start (D9, results.timed_calls),
so completion and makespan compare with every other experiment's. Against the series it places
calls by the VM's clock instead (ts_start, from the replay's start), the clock Prometheus stamps
its samples with: on real pods the plan's times drifted from it by up to 34 s with 24 clock jumps
(P3.10's review), while on the sim the two agree to about 2 s. An idle pod is a Ready one with no
call in flight at a sample, each sample one scrape step; a starting pod is not Ready, so not idle.
An interrupted run failed a call that began after the first scale-down less the call deadline:
one begun earlier had failed by then, whatever ended it (decided 2026-11-15).
"""

import csv
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import astuple, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from load_generator.plan import run_position
from load_generator.record import REPO_ROOT
from load_generator.results import percentile, timed_calls

CHART = REPO_ROOT / "infra" / "charts" / "autoscaling"
# Each autoscaler's trigger: one number for the whole Deployment, which KEDA divides by its
# threshold. Remaining work is the run tracker's (P3.8), from the agent runtime and the replayer.
TRIGGER_QUERIES = {
    "remaining": "sum(thesis_remaining_work)",
    "queue": "sum(vllm:num_requests_waiting)",
}
# Each autoscaler's threshold, a key of the profile's autoscaling section.
TARGET_KEYS = {"remaining": "remaining_work_target", "queue": "queue_target"}
# A plain HPA on the pods' CPU, as a share of their request (pod.cpu_request): the baseline the
# approved topic names (P3.10). Only it reads CPU_TARGET_KEY, so the simulator's section has none.
CPU = "cpu"
CPU_TARGET_KEY = "cpu_target_percent"
KINDS = (*TRIGGER_QUERIES, CPU)
# The plain HPA's own reading, from kube-state-metrics: the pods' mean CPU as a percent of their
# request, the number it holds to the target. KEDA's HPA has another name, so a KEDA variant has
# none.
CPU_QUERY = (
    'max(kube_horizontalpodautoscaler_status_target_metric{namespace="default",'
    'horizontalpodautoscaler="vllm",metric_name="cpu",metric_target_type="utilization"})'
)
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
    # Also None if the pods were scaled before the replay.
    scale_up_seconds: float | None
    queue_seconds: float | None
    # False by default: P3.9b's .meta.json files have no such key, and their series started at the
    # floor.
    scaled_before_replay: bool = False

    def as_meta(self) -> dict[str, float | bool | None]:
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


def check_cpu_target(section: Mapping[str, Any]) -> None:
    """A target the HPA takes: averageUtilization is a whole percent over 0. Over 100 is allowed,
    since a pod may use more CPU than it requests."""
    if CPU_TARGET_KEY not in section:
        raise AutoscaleError(f"autoscaling has no {CPU_TARGET_KEY}")
    if not _whole(section[CPU_TARGET_KEY]) or section[CPU_TARGET_KEY] < 1:
        raise AutoscaleError(f"autoscaling.{CPU_TARGET_KEY} must be a whole percent from 1 up")


def helm_values(section: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """infra/charts/autoscaling's values for an autoscaler on a profile's section. cpuTargetPercent
    is what makes the chart render the plain HPA in place of KEDA's object."""
    if kind not in KINDS:
        raise AutoscaleError(f"no autoscaler {kind}, there are {', '.join(KINDS)}")
    if kind == CPU:
        return {
            "minReplicas": section["min_replicas"],
            "maxReplicas": section["max_replicas"],
            "cpuTargetPercent": section[CPU_TARGET_KEY],
            "scaleDownStabilizationSeconds": section[SECONDS_KEY],
        }
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


def scaled_before_replay(rows: Sequence[AutoscaleRow], section: Mapping[str, Any]) -> bool:
    """The first sample with a spec count has it over the floor."""
    first = next((row.spec_replicas for row in rows if row.spec_replicas is not None), None)
    return first is not None and first > section["min_replicas"]


def first_crossings(rows: Sequence[AutoscaleRow], section: Mapping[str, Any]) -> Crossings:
    def over(value: float | None, target: float) -> bool:
        return value is not None and value > target

    scaled = scaled_before_replay(rows, section)
    scale_up = (
        None
        if scaled
        else _first(rows, lambda row: over(row.spec_replicas, section["min_replicas"]))
    )
    return Crossings(
        remaining_seconds=_first(
            rows,
            lambda row: over(
                _per_pod(row.remaining_work, row.spec_replicas), section["remaining_work_target"]
            ),
        ),
        scale_up_seconds=scale_up,
        queue_seconds=_first(
            rows,
            lambda row: over(_per_pod(row.waiting, row.spec_replicas), section["queue_target"]),
        ),
        scaled_before_replay=scaled,
    )


def scale_up_before_queue(crossings: Crossings) -> bool | None:
    """The done-when's question: a scale-up began, and the queue had not yet passed its target.
    None when the pods were scaled before the replay, which the series cannot time."""
    if crossings.scaled_before_replay:
        return None
    if crossings.scale_up_seconds is None:
        return False
    return crossings.queue_seconds is None or crossings.scale_up_seconds < crossings.queue_seconds


def _when(seconds: float | None) -> str:
    return "never" if seconds is None else f"at {seconds:.1f} s"


def crossing_text(kind: str, section: Mapping[str, Any], crossings: Crossings) -> str:
    if crossings.scaled_before_replay:
        scale_up = f"spec replicas were over {section['min_replicas']} from the replay's start"
        verdict = "the scale-up came before the replay, so it is not timed against the queue"
    else:
        scale_up = (
            f"spec replicas rose over {section['min_replicas']} {_when(crossings.scale_up_seconds)}"
        )
        began = "began" if scale_up_before_queue(crossings) else "did not begin"
        verdict = f"the scale-up {began} before the queue passed its target"
    return (
        f"  autoscaler {kind}: remaining work passed {section['remaining_work_target']} per pod "
        f"{_when(crossings.remaining_seconds)}, {scale_up}, the queue passed "
        f"{section['queue_target']} per pod {_when(crossings.queue_seconds)}; {verdict}"
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


@dataclass(frozen=True)
class CpuRow:
    # Since the replay's start.
    seconds: float
    cpu_utilization_percent: float | None


def cpu_rows_from(result: Sequence[Mapping[str, Any]], since: float, until: float) -> list[CpuRow]:
    """The plain HPA's reading from since to until, from the range answer to CPU_QUERY."""
    samples = _samples("cpu_utilization_percent", result)
    return [
        CpuRow(round(time - since, 3), value)
        for time, value in sorted(samples.items())
        if since <= time <= until
    ]


def write_cpu_csv(path: Path, rows: Sequence[CpuRow]) -> None:
    with path.open("x", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([field.name for field in fields(CpuRow)])
        for row in rows:
            writer.writerow([str(row.seconds), _cell(row.cpu_utilization_percent)])


# Kubernetes' suffixes of a CPU quantity below one core; none is whole cores.
CPU_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
# Go's duration units, the longer of m and ms first so the pattern reads ms whole.
DURATION_UNITS = {"h": 3600.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9, "m": 60.0, "s": 1.0}
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(" + "|".join(DURATION_UNITS) + ")")


def cpu_cores(quantity: str) -> float:
    """A CPU quantity as metrics-server and a profile write it: 155102366n, 1174m or 2."""
    if quantity[-1:] in CPU_SUFFIXES:
        return float(quantity[:-1]) * CPU_SUFFIXES[quantity[-1]]
    return float(quantity)


def go_seconds(duration: str) -> float:
    """A duration as Go prints it, which is how metrics-server writes a sample's window."""
    parts = DURATION_PART.findall(duration)
    if not parts or "".join(number + unit for number, unit in parts) != duration:
        raise AutoscaleError(f"{duration!r} is not a Go duration")
    return sum(float(number) * DURATION_UNITS[unit] for number, unit in parts)


def cpu_quiet(
    answer: Mapping[str, Any], request_cores: float, target_percent: int, since: float
) -> bool:
    """Whether a plain HPA installed now would start at the floor: each vLLM pod in metrics-server's
    PodMetricsList has a sample whose window began at or after since, the warm-up's end, and is
    under the target share of its request. The HPA reads that latest sample, whose window was up
    to 32 s long on 2026-11-17 and then held the warm-up (P3.10's review). No pod is no reading."""
    items = answer["items"]
    if not items:
        return False
    for item in items:
        ended = datetime.fromisoformat(item["timestamp"]).timestamp()
        if ended - go_seconds(item["window"]) < since:
            return False
        used = sum(cpu_cores(container["usage"]["cpu"]) for container in item["containers"])
        if used * 100 >= target_percent * request_cores:
            return False
    return True


@dataclass(frozen=True)
class CallSpan:
    """One call, in milliseconds from the replay's start on the VM's clock. A failed call has no
    end and no pod."""

    run: str
    pod: str | None
    begun_ms: int
    ended_ms: int | None


def wall_spans(lines: Sequence[Mapping[str, Any]], started: float) -> list[CallSpan]:
    """From each call's ts_start, which the replay stamps with the VM's clock, as Prometheus stamps
    its samples; started is the experiment clock's reading the series count from."""
    zero = round(started * 1000)
    spans = []
    for line in lines:
        begun = line["ts_start"] - zero
        if "duration_ms" not in line:
            spans.append(CallSpan(line["run_id"], None, begun, None))
        else:
            spans.append(CallSpan(line["run_id"], line["pod"], begun, begun + line["duration_ms"]))
    return spans


@dataclass(frozen=True)
class AutoscaleSummary:
    """P5.2's numbers for one autoscaled variant, over its kept runs unless named otherwise."""

    runs: int
    finished_runs: int
    # A failed run has no completion time: read the times with this count.
    failed_runs: int
    # Of failed_runs, those a scale-down may have caused (P4.3's drain is not built yet).
    interrupted_runs: int
    # None with no finished run.
    completion_p50_ms: int | None
    completion_p95_ms: int | None
    # Over the finished runs.
    completion_total_ms: int
    # From the first kept run's start to the last kept call's end; None with no answered call.
    makespan_ms: int | None
    # Finished runs per minute of the makespan.
    runs_per_minute: float | None
    # The series' first count was already over the floor, so the scale-up is not timed.
    scaled_before_replay: bool
    # From the first scale-up to that many pods Ready; None if either never came, or if the pods
    # were scaled before the replay.
    time_to_capacity_seconds: float | None
    # Over the whole replay, the discarded runs' time included: those pods cost the same.
    idle_pod_minutes: float

    def as_meta(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


def time_to_capacity(rows: Sequence[AutoscaleRow], section: Mapping[str, Any]) -> float | None:
    """From the first sample with spec replicas over the floor to the first, from then on, with
    that many Ready."""
    found = _first_over_floor(rows, section["min_replicas"])
    if found is None or scaled_before_replay(rows, section):
        return None
    index, wanted = found
    ready = _first(
        rows[index:], lambda row: row.ready_replicas is not None and row.ready_replicas >= wanted
    )
    return None if ready is None else ready - rows[index].seconds


def _first_over_floor(rows: Sequence[AutoscaleRow], floor: int) -> tuple[int, float] | None:
    """The first sample with spec replicas over the floor: its index and its count."""
    for index, row in enumerate(rows):
        if row.spec_replicas is not None and row.spec_replicas > floor:
            return index, row.spec_replicas
    return None


def _busy_pods(spans: Sequence[CallSpan], at_ms: float) -> int:
    """Pods with a call in flight: begun, and not yet ended."""
    return len(
        {
            span.pod
            for span in spans
            if span.pod is not None
            and span.ended_ms is not None
            and span.begun_ms <= at_ms < span.ended_ms
        }
    )


def idle_pod_minutes(
    rows: Sequence[AutoscaleRow], spans: Sequence[CallSpan], step_seconds: int
) -> float:
    """Each sample with a Ready count stands for one step. More busy pods than Ready is none idle:
    a pod leaving Ready may still finish its calls."""
    idle = sum(
        max(row.ready_replicas - _busy_pods(spans, row.seconds * 1000), 0)
        for row in rows
        if row.ready_replicas is not None
    )
    return idle * step_seconds / 60


def first_scale_down(rows: Sequence[AutoscaleRow]) -> float | None:
    """The first sample whose spec replicas are below the last sample that had them."""
    previous = None
    for row in rows:
        if row.spec_replicas is None:
            continue
        if previous is not None and row.spec_replicas < previous:
            return row.seconds
        previous = row.spec_replicas
    return None


def interrupted_runs(
    rows: Sequence[AutoscaleRow], spans: Sequence[CallSpan], deadline_seconds: float
) -> int:
    """Runs with a failed call begun after the first scale-down less the call deadline."""
    down = first_scale_down(rows)
    if down is None:
        return 0
    since_ms = (down - deadline_seconds) * 1000
    return len({span.run for span in spans if span.ended_ms is None and span.begun_ms > since_ms})


def summarise_autoscale(
    rows: Sequence[AutoscaleRow],
    lines: Sequence[Mapping[str, Any]],
    starts: Sequence[float],
    started: float,
    runs_discarded: int,
    section: Mapping[str, Any],
    step_seconds: int,
    deadline_seconds: float,
) -> AutoscaleSummary:
    """lines are the replay's. starts are the plan's, by run position: a run's time is from its
    planned start, as the call rows' (results.run_completion_ms). started is the clock reading the
    series count from, which the calls are lined up with them by."""

    def kept(run: str) -> bool:
        return run_position(run) > runs_discarded

    ends: dict[str, list[int]] = {}
    failed: set[str] = set()
    for call in timed_calls(lines, starts):
        run = call.line["run_id"]
        if not kept(run):
            continue
        if call.ended is None:
            failed.add(run)
        else:
            ends.setdefault(run, []).append(call.ended)
    completions = sorted(
        max(run_ends) - round(starts[run_position(run) - 1] * 1000)
        for run, run_ends in ends.items()
        if run not in failed
    )
    makespan = None
    if ends and runs_discarded < len(starts):
        makespan = max(max(run_ends) for run_ends in ends.values()) - round(
            starts[runs_discarded] * 1000
        )
    per_minute = None
    if makespan:
        per_minute = len(completions) / (makespan / 60_000)
    runs = len(ends.keys() | failed)
    spans = wall_spans(lines, started)
    scaled = scaled_before_replay(rows, section)
    return AutoscaleSummary(
        runs=runs,
        finished_runs=len(completions),
        failed_runs=runs - len(completions),
        interrupted_runs=interrupted_runs(
            rows, [span for span in spans if kept(span.run)], deadline_seconds
        ),
        completion_p50_ms=percentile(completions, 0.5) if completions else None,
        completion_p95_ms=percentile(completions, 0.95) if completions else None,
        completion_total_ms=sum(completions),
        makespan_ms=makespan,
        runs_per_minute=per_minute,
        scaled_before_replay=scaled,
        time_to_capacity_seconds=time_to_capacity(rows, section),
        idle_pod_minutes=idle_pod_minutes(rows, spans, step_seconds),
    )


def _summary_cell(value: object) -> str:
    return "" if value is None else str(value)


def write_summary_csv(path: Path, summaries: Mapping[str, tuple[str, Mapping[str, Any]]]) -> None:
    """One row per variant: its name, its autoscaler and its summary as in the .meta.json."""
    names = [field.name for field in fields(AutoscaleSummary)]
    with path.open("x", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["variant", "autoscaler", *names])
        for variant, (kind, summary) in summaries.items():
            writer.writerow([variant, kind, *(_summary_cell(summary[name]) for name in names)])


def _ms_text(milliseconds: int | None) -> str:
    return "-" if milliseconds is None else f"{milliseconds / 1000:.1f} s"


def summary_text(summary: AutoscaleSummary) -> str:
    rate = "-" if summary.runs_per_minute is None else f"{summary.runs_per_minute:.2f}"
    if summary.scaled_before_replay:
        capacity = "not timed, the pods were scaled up before the replay"
    elif summary.time_to_capacity_seconds is None:
        capacity = "never came"
    else:
        capacity = f"in {summary.time_to_capacity_seconds:.1f} s"
    return (
        f"  {summary.finished_runs} of {summary.runs} kept runs finished in "
        f"{_ms_text(summary.makespan_ms)}, {rate} per minute; run time p50 "
        f"{_ms_text(summary.completion_p50_ms)}, p95 {_ms_text(summary.completion_p95_ms)}, total "
        f"{_ms_text(summary.completion_total_ms)}; capacity {capacity}; "
        f"{summary.idle_pod_minutes:.2f} idle pod-minutes; {summary.interrupted_runs} of "
        f"{summary.failed_runs} failed runs interrupted by a scale-down"
    )
