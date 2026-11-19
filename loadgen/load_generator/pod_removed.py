"""make pod-removed (P3.4): a replay with one vLLM pod deleted partway, and where its runs went.

The picker runs the experiment's rules, affinity on. A replay runs in-process; delete_after_seconds
after it starts (its prompt check included) the Ready pod holding the most live pins in the
picker's log is deleted, so the delete hits runs (the first by name held none on 2026-11-11). The
picker drops a pod as soon as its deletion is asked for (llm-d-router v0.11.0's IsPodReady), while
the simulator answers new calls 503 and lets the ones it runs go on until the pod's 30 s grace
period ends, when the kubelet kills it (llm-d-inference-sim v1.0.0's drain). After the replay the
replacement pod is waited for, the picker's log since the start is kept, and the profile's routing
is put back, whatever happened.

The report sorts the calls by when they were sent: finished before the delete, running at it, sent
during the settle (the picker learning of the delete, and the VM's wall clock, which can step 2.5 s,
P2.3), and sent after. It passes when no call sent after failed or went to the deleted pod, and each
run pinned to that pod at the delete with a call sent after has one remapped line from it in the
picker's log. A replay never retries (P2.3), so the calls running at the delete are counted, not
judged, those on the deleted pod apart: retry is P4.4, which reuses the delete and the report.
Decided with the user, docs/task-cards/39-P3.4-pod-removed.md.

Usage: python -m load_generator.pod_removed EXPERIMENT
"""

import argparse
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import yaml

from load_generator.experiment import (
    EPP_DEPLOYMENT,
    SCALE_TIMEOUT,
    VLLM_DEPLOYMENT,
    VLLM_SELECTOR,
    plugin_on,
    pods_to_warm,
    set_routing,
)
from load_generator.record import REPO_ROOT, experiment_hash, kubectl
from load_generator.replay import replay_to_file

FINISHED_BEFORE = "finished before"
RUNNING_AT_DELETE = "running at the delete"
SENT_DURING_SETTLE = "sent during the settle"
SENT_AFTER = "sent after"
GROUPS = (FINISHED_BEFORE, RUNNING_AT_DELETE, SENT_DURING_SETTLE, SENT_AFTER)
# What replay.py prints for a call with no full reply, the only place its reason is kept (P2.3).
FAILURE_LINE = re.compile(
    r"^replay: (?P<run>\S+) step (?P<step>\d+): no full reply: (?P<reason>.*)$"
)
# The plugin's lines (gateway/runrouter/plugin.go), in llm-d-router's JSON, the message in body.
PICKED = "Run call picked"
RUN_ENDED = "Run ended"
REMAPPED = "remapped"
# The picker names an endpoint <pod>-rank-<n> (llm-d-router v0.11.0, createEndpointNamespacedName in
# pkg/epp/datastore/datastore.go), and the plugin logs that name; the replay's lines hold the pod's.
RANK_SUFFIX = re.compile(r"-rank-\d+$")
# The pod's grace period is the default 30 s, after which the kubelet kills the simulator, whose own
# drain would take up to 30 s and 5 s more. 120 s covers that with a wide margin, as experiment.py's
# UNINSTALL_TIMEOUT; a pod still there by then is reported, and the run goes on.
POD_GONE_TIMEOUT = "120s"
# A failed call whose reason the replay's printed lines lack.
NO_REASON = "no reason printed"


class PodRemovedError(Exception):
    """The run must not start or its result would not show a remap."""


@dataclass(frozen=True)
class PodRemovedExperiment:
    # The file's stem, which names its results.
    name: str
    hash: str
    profile: str
    routing: Mapping[str, bool]
    trace: Path
    pattern: str
    seed: int
    delete_after_seconds: float
    settle_seconds: float


def load_pod_removed(path: Path) -> PodRemovedExperiment:
    """A missing key raises KeyError. Rules the profile lacks are refused by set-routing.sh, which
    runs first, before any replay."""
    spec = yaml.safe_load(path.read_text())
    routing = spec["routing"]
    if not isinstance(routing, dict) or not all(isinstance(on, bool) for on in routing.values()):
        raise PodRemovedError(f"routing must map rules to true or false, got {routing}")
    for rule in ("plugin", "affinity"):
        if routing.get(rule) is not True:
            raise PodRemovedError(f"routing.{rule} must be on: with it off no run is pinned")
    if spec["delete_after_seconds"] <= 0:
        raise PodRemovedError("delete_after_seconds must be after the replay starts")
    if spec["settle_seconds"] < 0:
        raise PodRemovedError("settle_seconds must not be negative")
    trace = (path.parent / spec["trace"]).resolve()
    if not trace.exists():
        raise PodRemovedError(f"no trace {spec['trace']}")
    return PodRemovedExperiment(
        name=path.stem,
        hash=experiment_hash(path),
        profile=spec["profile"],
        routing=routing,
        trace=trace,
        pattern=spec["pattern"],
        seed=spec["seed"],
        delete_after_seconds=spec["delete_after_seconds"],
        settle_seconds=spec["settle_seconds"],
    )


def group_of(line: Mapping[str, Any], deleted_ms: int, settle_ms: int) -> str:
    """ts_start and the delete are both on the wall clock. A failed call's end is not recorded, so
    one sent before the delete counts as running at it. Calls on every pod are grouped; the report
    counts those running on the deleted pod apart."""
    start: int = line["ts_start"]
    if start >= deleted_ms + settle_ms:
        return SENT_AFTER
    if start >= deleted_ms:
        return SENT_DURING_SETTLE
    if "duration_ms" in line and start + line["duration_ms"] <= deleted_ms:
        return FINISHED_BEFORE
    return RUNNING_AT_DELETE


def failure_reasons(log: str) -> dict[tuple[str, int], list[str]]:
    """Each failed call's reason by run and step; a fanned-out step can have several."""
    reasons: dict[tuple[str, int], list[str]] = {}
    for text in log.splitlines():
        match = FAILURE_LINE.match(text)
        if match is not None:
            key = (match["run"], int(match["step"]))
            reasons.setdefault(key, []).append(match["reason"])
    return reasons


def pinned_pods(lines: Iterable[Mapping[str, Any]], deleted_ms: int) -> dict[str, str]:
    """Each run's pod at the delete: that of its last call sent before it that got a reply, which
    is the pod its pin named. A call with no reply names no pod."""
    pods: dict[str, tuple[int, str]] = {}
    for line in lines:
        if "pod" not in line or line["ts_start"] >= deleted_ms:
            continue
        run = line["run_id"]
        if run not in pods or line["ts_start"] >= pods[run][0]:
            pods[run] = (line["ts_start"], line["pod"])
    return {run: pod for run, (_, pod) in pods.items()}


def endpoint_pod(endpoint: str) -> str:
    return RANK_SUFFIX.sub("", endpoint)


def live_pins(picker: Iterable[Mapping[str, Any]], since: str) -> dict[str, str]:
    """Each run's pod in the plugin's own lines logged from since on: its last pick's, until its end
    of run. kubectl's --since-time returned set-routing's probe, logged before the window, so each
    line's own timestamp is checked too (pod-removed-346f89bc516e); the picker's clock is the VM's,
    as since is."""
    start = datetime.fromisoformat(since)
    pins: dict[str, str] = {}
    for line in picker:
        if line.get("body") not in (PICKED, RUN_ENDED):
            continue
        if datetime.fromisoformat(line["timestamp"]) < start:
            continue
        if line["body"] == PICKED:
            pins[line["run-id"]] = endpoint_pod(line["pod"])
        else:
            pins.pop(line["run-id"], None)
    return pins


def choose_pod(pins: Mapping[str, str], ready: Sequence[str]) -> tuple[str, dict[str, int]]:
    """The Ready pod holding the most pins, a tie going by name, and each one's count."""
    counts = {pod: sum(pinned == pod for pinned in pins.values()) for pod in sorted(ready)}
    return min(counts, key=lambda pod: -counts[pod]), counts


def picker_lines(text: str) -> list[dict[str, Any]]:
    """The JSON objects in the picker's log. Anything else is skipped, as gateway_check.sh's
    fromjson? does; a log the report cannot read leaves no remapped line, so the run fails."""
    found = []
    for text_line in text.splitlines():
        with contextlib.suppress(ValueError):
            line = json.loads(text_line)
            if isinstance(line, dict):
                found.append(line)
    return found


@dataclass(frozen=True)
class Group:
    calls: int
    failed: int
    # "<run> step <n>: <reason>" for each failed call.
    reasons: list[str]


@dataclass(frozen=True)
class Report:
    deleted_pod: str
    groups: dict[str, Group]
    # Runs pinned to the deleted pod at the delete that had not finished by then.
    affected: list[str]
    # Those of them with a call sent after the settle: the ones the check tests.
    tested: list[str]
    sent_after_on_deleted: int
    # Calls running at the delete on the deleted pod, and how many of them failed. A call with no
    # reply names no pod, so its run's pin at the delete stands for it.
    running_on_deleted: int
    running_on_deleted_failed: int
    # Tested runs without exactly one remapped line from the deleted pod.
    remap_misses: list[str]
    runs: int
    # Replayed runs with a Run ended line, D1's end of run seen by the picker.
    ended: int

    @property
    def passed(self) -> bool:
        return (
            self.groups[SENT_AFTER].failed == 0
            and self.sent_after_on_deleted == 0
            and bool(self.tested)
            and not self.remap_misses
        )


def build_report(
    lines: Sequence[Mapping[str, Any]],
    log: str,
    picker: str,
    deleted_pod: str,
    deleted_ms: int,
    settle_ms: int,
) -> Report:
    reasons = failure_reasons(log)
    grouped: dict[str, list[Mapping[str, Any]]] = {name: [] for name in GROUPS}
    for line in lines:
        grouped[group_of(line, deleted_ms, settle_ms)].append(line)
    groups = {}
    for name, calls in grouped.items():
        failed = [line for line in calls if "duration_ms" not in line]
        keys = list(dict.fromkeys((line["run_id"], line["step"]) for line in failed))
        texts = [
            f"{run} step {step}: {reason}"
            for run, step in keys
            for reason in reasons.get((run, step), [NO_REASON])
        ]
        groups[name] = Group(calls=len(calls), failed=len(failed), reasons=texts)
    pins = pinned_pods(lines, deleted_ms)
    going_on = {
        line["run_id"]
        for name, calls in grouped.items()
        if name != FINISHED_BEFORE
        for line in calls
    }
    affected = sorted(run for run, pod in pins.items() if pod == deleted_pod and run in going_on)
    running = [
        line
        for line in grouped[RUNNING_AT_DELETE]
        if line.get("pod", pins.get(line["run_id"])) == deleted_pod
    ]
    after = grouped[SENT_AFTER]
    tested = sorted({line["run_id"] for line in after} & set(affected))
    picked = picker_lines(picker)
    remaps: dict[str, list[str]] = {}
    for line in picked:
        if line.get("body") == PICKED and line.get("reason") == REMAPPED:
            remaps.setdefault(line["run-id"], []).append(endpoint_pod(line["from"]))
    runs = {line["run_id"] for line in lines}
    ended = {line.get("run-id") for line in picked if line.get("body") == RUN_ENDED} & runs
    return Report(
        deleted_pod=deleted_pod,
        groups=groups,
        affected=affected,
        tested=tested,
        sent_after_on_deleted=sum(line.get("pod") == deleted_pod for line in after),
        running_on_deleted=len(running),
        running_on_deleted_failed=sum("duration_ms" not in line for line in running),
        remap_misses=[run for run in tested if remaps.get(run) != [deleted_pod]],
        runs=len(runs),
        ended=len(ended),
    )


def report_lines(report: Report) -> list[str]:
    lines = []
    for name, group in report.groups.items():
        text = f"  {name}: {group.calls} calls, {group.failed} failed"
        if name == RUNNING_AT_DELETE:
            text += (
                f"; {report.running_on_deleted} on {report.deleted_pod}, "
                f"{report.running_on_deleted_failed} failed"
            )
        lines.append(text)
        lines += [f"    {reason}" for reason in group.reasons]
    lines.append(
        f"  runs pinned to {report.deleted_pod} at the delete: {len(report.affected)} "
        f"{report.affected}; with a call sent after: {len(report.tested)} {report.tested}"
    )
    lines.append(f"  calls sent after on {report.deleted_pod}: {report.sent_after_on_deleted}")
    lines.append(
        f"  of those runs, without one remapped line from {report.deleted_pod}: "
        f"{len(report.remap_misses)} {report.remap_misses}"
    )
    lines.append(f"  Run ended lines: {report.ended} of {report.runs} runs")
    if report.passed:
        lines.append("pod-removed: passed")
    elif not report.tested:
        lines.append(
            "pod-removed: failed, nothing tested: no run pinned to the deleted pod had a call "
            "sent after the settle"
        )
    else:
        lines.append("pod-removed: failed")
    return lines


class Tee:
    """A stream that writes to several: the replay's printed lines go to the terminal and to the
    file kept beside its result."""

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def _now_ms() -> int:
    return round(time.time() * 1000)


@dataclass
class Deletion:
    # The Ready pods at the start, and where the picker's log of the replay starts.
    ready: list[str]
    since: str
    # Set as the timer's thread starts, and once kubectl has taken the delete: a thread that died
    # in between is told from a replay that ended before the timer.
    started: bool = False
    deleted: bool = False
    # Chosen at the delete; each Ready pod's live pins then.
    pod: str | None = None
    pins: dict[str, int] | None = None
    # When the delete was asked for and when the pod was gone, on the wall clock as ts_start.
    requested_ms: int | None = None
    gone_ms: int | None = None
    # Raised after the replay, so a delete that never happened cannot read as a pass.
    error: subprocess.CalledProcessError | None = None


@dataclass(frozen=True)
class Removed:
    """A delete that happened during the replay."""

    pod: str
    pins: dict[str, int]
    # Where the picker's log of the replay starts, which the pins were counted from.
    since: str
    requested_ms: int
    # None when the pod outlived POD_GONE_TIMEOUT.
    gone_ms: int | None


def delete_pod(deletion: Deletion) -> None:
    """Runs in a timer's thread while the replay runs: the pod with the most live pins in the
    picker's log so far is deleted. Fetching that log takes about 2 s on a replay's DEBUG lines, so
    the pins are counted that long before the delete. --wait=false, so the time asked is the
    delete's; the wait after it gives how long the pod drained. A failed log or delete is raised
    after the replay; a pod that outlives the wait is printed and leaves gone_ms None, since the
    delete itself went through."""
    deletion.started = True
    try:
        picker = kubectl("logs", f"deployment/{EPP_DEPLOYMENT}", f"--since-time={deletion.since}")
        pod, deletion.pins = choose_pod(
            live_pins(picker_lines(picker), deletion.since), deletion.ready
        )
        deletion.pod = pod
        print(f"pod-removed: deleting {pod}, live pins {deletion.pins}", flush=True)
        deletion.requested_ms = _now_ms()
        kubectl("delete", "pod", pod, "--wait=false")
    except subprocess.CalledProcessError as error:
        deletion.error = error
        return
    deletion.deleted = True
    try:
        kubectl("wait", f"pod/{pod}", "--for=delete", f"--timeout={POD_GONE_TIMEOUT}")
    except subprocess.CalledProcessError:
        print(f"pod-removed: {pod} not gone after {POD_GONE_TIMEOUT}", flush=True)
        return
    deletion.gone_ms = _now_ms()


def log_path(out: Path) -> Path:
    """Where the replay's printed lines are kept, beside its result."""
    return out.with_name(f"{out.name}.log")


def run_pod_removed(
    experiment: PodRemovedExperiment, profile: Mapping[str, Any], out: Path
) -> tuple[Removed, list[dict[str, Any]], str]:
    """The delete, the replay's lines and the picker's log."""
    pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
    ready = sorted(pods_to_warm(pods, profile["replicas"]))
    try:
        # Inside the try: a switch that fails after its Helm upgrade may have changed the picker.
        set_routing(experiment.profile, None, experiment.routing)
        # After set-routing.sh. kubectl's --since-time keeps whole seconds and returned its probe
        # (pod-removed-346f89bc516e); live_pins compares each line's own timestamp with this.
        since = datetime.now(UTC).isoformat()
        deletion = Deletion(ready, since)
        timer = threading.Timer(experiment.delete_after_seconds, delete_pod, (deletion,))
        with log_path(out).open("x") as log, contextlib.redirect_stdout(Tee(sys.stdout, log)):
            timer.start()
            try:
                lines = replay_to_file(
                    profile,
                    experiment.trace,
                    experiment.pattern,
                    experiment.seed,
                    out,
                    plugin_on(profile["routing"], experiment.routing),
                )
            finally:
                timer.cancel()
                timer.join()
        if deletion.error is not None:
            raise deletion.error
        if not deletion.started:
            raise PodRemovedError(
                f"the replay ended before the delete at {experiment.delete_after_seconds} s"
            )
        if (
            not deletion.deleted
            or deletion.pod is None
            or deletion.pins is None
            or deletion.requested_ms is None
        ):
            raise PodRemovedError(
                "the delete thread stopped before the pod was deleted: its traceback is above"
            )
        kubectl("rollout", "status", VLLM_DEPLOYMENT, f"--timeout={SCALE_TIMEOUT}")
        picker = kubectl("logs", f"deployment/{EPP_DEPLOYMENT}", f"--since-time={since}")
    finally:
        set_routing(experiment.profile, None, None)
    removed = Removed(
        deletion.pod, deletion.pins, deletion.since, deletion.requested_ms, deletion.gone_ms
    )
    return removed, lines, picker


def main() -> None:
    parser = argparse.ArgumentParser(description="P3.4: a replay with a vLLM pod deleted.")
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()

    experiment = load_pod_removed(args.experiment)
    profile_path = REPO_ROOT / "config" / f"{experiment.profile}.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    results = REPO_ROOT / "experiments" / "results"
    stem = f"{experiment.name}-{experiment.hash}"
    found = sorted(results.glob(f"{stem}*"))
    if found:
        raise PodRemovedError(f"results of this file exist, delete them to run it again: {found}")
    out = results / f"{stem}.jsonl"
    deletion, lines, picker = run_pod_removed(experiment, profile, out)
    picker_path = results / f"{stem}-picker.log"
    picker_path.write_text(picker)
    log = log_path(out).read_text()
    report = build_report(
        lines,
        log,
        picker,
        deletion.pod,
        deletion.requested_ms,
        round(experiment.settle_seconds * 1000),
    )
    meta = {
        "experiment": experiment.name,
        "hash": experiment.hash,
        "profile": experiment.profile,
        "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        "trace": experiment.trace.name,
        "pattern": experiment.pattern,
        "seed": experiment.seed,
        "routing": dict(experiment.routing),
        "delete_after_seconds": experiment.delete_after_seconds,
        "settle_seconds": experiment.settle_seconds,
        "deleted_pod": deletion.pod,
        "live_pins_at_delete": deletion.pins,
        "picker_log_since": deletion.since,
        "delete_requested_ms": deletion.requested_ms,
        "pod_gone_ms": deletion.gone_ms,
        "groups": {
            name: {"calls": g.calls, "failed": g.failed} for name, g in report.groups.items()
        },
        "affected_runs": report.affected,
        "tested_runs": report.tested,
        "sent_after_on_deleted": report.sent_after_on_deleted,
        "running_on_deleted": report.running_on_deleted,
        "running_on_deleted_failed": report.running_on_deleted_failed,
        "remap_misses": report.remap_misses,
        "runs": report.runs,
        "ended_runs": report.ended,
        "passed": report.passed,
    }
    meta_path = results / f"{stem}.meta.json"
    with meta_path.open("x") as file:
        json.dump(meta, file, indent=2)
        file.write("\n")
    gone = (
        f"not gone after {POD_GONE_TIMEOUT}"
        if deletion.gone_ms is None
        else f"gone after {(deletion.gone_ms - deletion.requested_ms) / 1000:.1f} s"
    )
    settle = experiment.settle_seconds
    print(f"pod-removed: deleted {deletion.pod}, {gone}; settle {settle} s")
    for line in report_lines(report):
        print(line)
    print(
        f"pod-removed: wrote {out}, {log_path(out).name}, {picker_path.name} and {meta_path.name}"
    )
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
