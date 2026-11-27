"""make failover-check (P4.4): a replay with a vLLM (or simulated) pod killed partway, and how many
runs complete with and without the retry.

Per variant, in the file's order: the pods are restarted (empty caches), the picker runs the file's
rules (affinity on, so runs are pinned), and a replay runs in-process with the variant's
replay.retry. kill_after_seconds after it starts, the Ready pod holding the most live pins in the
picker's log is force-deleted with no grace: a plain delete would drain it (P4.3), and this is a
failure. The kubelet then gives the pod ~1 s of its preStop, SIGTERM, and SIGKILL 2 s later
(Kubernetes v1.36.5, pod_workers.go and kuberuntime_container.go), so its calls die, on vLLM and on
the simulator alike. The ReplicaSet starts a replacement, which is waited for after the replay. If
no pod holds a pin within wait_for_calls_seconds, the variant is not killed and reports void. The
profile's routing comes back after the last variant, whatever happened.

The report counts the runs that complete, those that failed on their deadline apart (one pod is
left to carry the load), and each call running on the killed pod at the kill: answered there before
it died, sent again and answered elsewhere, or failed. A run with a call running at the kill or sent
during the settle (P3.4's: the picker learning of the kill, the VM's clock stepping 2.5 s) that
failed for any reason but its deadline was failed by the kill. With the retry, a variant passes
with at least one call running on the killed pod at the kill and no run failed by the kill; without
it, it is the control: counted, not judged. Decided with the user,
docs/task-cards/60-P4.4-failover.md.

A file may name a list of seeds in place of one, each with the order its variants run in, and a
variant with no kill (P5.3, docs/task-cards/77-P5.3-failover.md). Each seed is then its own run,
its results named with the seed and its .meta.json written after its last variant; a seed whose
.meta.json exists is skipped, so the same command picks up a run that stopped, and a seed left
halfway is refused before anything runs. The no-kill variant is the reference: each kill
variant's runs are paired with the same runs in it, and the difference is the recovery cost,
apart for the runs with a call on the killed pod at the kill, the runs only pinned there, and the
rest, whose difference is the noise and the one pod lost. After the last seed, every seed's
numbers go into <name>-<hash>-summary.csv, and each variant's completion over the seeds is
printed.

In P5.3 every call on the killed pod with the retry on finished in the seconds the kubelet leaves
it, so the retry was never tested (docs/task-cards/79-P5.3b-failover-cut.md). A file may then set
stop_container: the kill first stops the pod's running containers with crictl and no timeout,
which sends SIGKILL with no SIGTERM before it, so every stream on the pod is cut at the kill, and
then force-deletes the pod as before. containerd's socket is root's, so this runs through sudo -n,
checked before anything runs. And require_cut: the verdict then needs a call the kill cut, a call
on the killed pod, running at the kill or sent during the settle, with an attempt that failed for
a reason but its deadline; a kill that cut none is void.

Usage: python -m load_generator.failover EXPERIMENT
"""

import argparse
import contextlib
import csv
import dataclasses
import hashlib
import json
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from load_generator.drain import DEADLINE_REASON, busiest_pod, picker_restarts
from load_generator.experiment import (
    EPP_DEPLOYMENT,
    SCALE_TIMEOUT,
    SIM_PROFILE,
    SOURCE_ROOT,
    VLLM_DEPLOYMENT,
    VLLM_SELECTOR,
    plugin_on,
    pods_to_warm,
    set_routing,
    source_state,
)
from load_generator.pod_removed import (
    FINISHED_BEFORE,
    NO_REASON,
    PICKED,
    REMAPPED,
    RUNNING_AT_DELETE,
    SENT_DURING_SETTLE,
    Tee,
    choose_pod,
    endpoint_pod,
    failure_reasons,
    group_of,
    live_pins,
    log_path,
    picker_lines,
    pinned_pods,
)
from load_generator.record import REPO_ROOT, experiment_hash
from load_generator.replay import replay_to_file

# What replay.py prints before it sends a call again (replay._call): the attempt that failed and
# when it was sent, on the wall clock as ts_start.
RETRY_LINE = re.compile(
    r"^replay: (?P<run>\S+) step (?P<step>\d+): retry (?P<attempt>\d+), "
    r"attempt sent at (?P<sent>\d+): (?P<reason>.*)$"
)
# How often the kill reads the picker's log while it waits for a pinned pod. Fetching it takes
# about 2 s on a replay's DEBUG lines (P3.4), so a shorter wait would only overlap the fetches.
PICKER_POLL_SECONDS = 2.0
# The pods' Helm release: make sim-deploy installs the sim chart into it, make vllm-deploy the vllm
# chart, each with the profile's model section among its values (Makefile). The agent's ConfigMap,
# which holds the profile's text, is not there on sim: make up deploys no agent for it.
POOL_RELEASE = "vllm"


class FailoverError(Exception):
    """The run must not start, or its result would not show a failover."""


def kubectl(*args: str) -> str:
    """kubectl's output. Its message is kept on the error, so a wait that timed out is told from
    one that failed (P3.4's second review)."""
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=True).stdout


def helm(*args: str) -> str:
    """helm's output, its message kept on the error as kubectl's."""
    return subprocess.run(["helm", *args], capture_output=True, text=True, check=True).stdout


def kubectl_failure(error: subprocess.CalledProcessError) -> str:
    message = (error.stderr or "").strip() or f"exit {error.returncode}"
    return f"kubectl {error.cmd[1]} failed: {message}"


# k3s's crictl, through sudo with no prompt: containerd's socket is root's (P5.3b).
CRICTL = ("sudo", "-n", "k3s", "crictl")


def crictl(*args: str) -> str:
    """crictl's output, its message kept on the error as kubectl's."""
    return subprocess.run([*CRICTL, *args], capture_output=True, text=True, check=True).stdout


def command_failure(error: subprocess.CalledProcessError) -> str:
    """kubectl's or crictl's failure, named by the tool and its verb."""
    if tuple(error.cmd[: len(CRICTL)]) != CRICTL:
        return kubectl_failure(error)
    message = (error.stderr or "").strip() or f"exit {error.returncode}"
    return f"crictl {error.cmd[len(CRICTL)]} failed: {message}"


@dataclass(frozen=True)
class FailoverVariant:
    name: str
    # The replay's replay.retry for this variant: off is the replay as before P4.4.
    retry: bool
    # False: the replay runs with no pod killed, the reference the kill variants' runs are paired
    # with (P5.3).
    kill: bool = True


@dataclass(frozen=True)
class FailoverExperiment:
    # The file's stem, which names its results.
    name: str
    hash: str
    profile: str
    routing: Mapping[str, bool]
    trace: Path
    pattern: str
    seed: int
    kill_after_seconds: float
    wait_for_calls_seconds: float
    settle_seconds: float
    # In the order they run.
    variants: list[FailoverVariant]
    # What its results are named: <name>-<hash> for a file of one seed, as P4.4's, and
    # <name>-<hash>-s<seed> for each seed of a file of seeds.
    stem: str
    # The kill stops the pod's containers at once before the delete, and the verdict needs a call
    # the kill cut (P5.3b). Off in P4.4's and P5.3's files.
    stop_container: bool = False
    require_cut: bool = False


def _variants(sections: Sequence[Mapping[str, Any]]) -> list[FailoverVariant]:
    variants = []
    for section in sections:
        name, retry, kill = section["name"], section["retry"], section.get("kill", True)
        if not isinstance(retry, bool):
            raise FailoverError(f"variant {name}: retry must be true or false")
        if not isinstance(kill, bool):
            raise FailoverError(f"variant {name}: kill must be true or false")
        if not kill and not retry:
            raise FailoverError(
                f"variant {name}: the no-kill variant is the retry's reference, so it retries too"
            )
        variants.append(FailoverVariant(name, retry, kill))
    names = [variant.name for variant in variants]
    if not names or len(set(names)) != len(names):
        raise FailoverError(f"the variants need names, each once, got {names}")
    if sum(not variant.kill for variant in variants) > 1:
        raise FailoverError("at most one variant runs with no kill: the one reference")
    if not any(variant.kill for variant in variants):
        raise FailoverError("no variant kills a pod")
    return variants


def _experiment(
    path: Path, spec: Mapping[str, Any], seed: int, variants: list[FailoverVariant], stem: str
) -> FailoverExperiment:
    """A missing key raises KeyError. Rules the profile lacks are refused by set-routing.sh, before
    any replay."""
    routing = spec["routing"]
    if not isinstance(routing, dict) or not all(isinstance(on, bool) for on in routing.values()):
        raise FailoverError(f"routing must map rules to true or false, got {routing}")
    for rule in ("plugin", "affinity"):
        if routing.get(rule) is not True:
            raise FailoverError(f"routing.{rule} must be on: the pod killed is the one most pinned")
    if spec["kill_after_seconds"] <= 0:
        raise FailoverError("kill_after_seconds must be after the replay starts")
    for key in ("wait_for_calls_seconds", "settle_seconds"):
        if spec[key] < 0:
            raise FailoverError(f"{key} must not be negative")
    for key in ("stop_container", "require_cut"):
        if not isinstance(spec.get(key, False), bool):
            raise FailoverError(f"{key} must be true or false")
    trace = (path.parent / spec["trace"]).resolve()
    if not trace.exists():
        raise FailoverError(f"no trace {spec['trace']}")
    return FailoverExperiment(
        name=path.stem,
        hash=experiment_hash(path),
        profile=spec["profile"],
        routing=routing,
        trace=trace,
        pattern=spec["pattern"],
        seed=seed,
        kill_after_seconds=spec["kill_after_seconds"],
        wait_for_calls_seconds=spec["wait_for_calls_seconds"],
        settle_seconds=spec["settle_seconds"],
        variants=variants,
        stem=stem,
        stop_container=spec.get("stop_container", False),
        require_cut=spec.get("require_cut", False),
    )


def load_failover(path: Path) -> FailoverExperiment:
    """A file of one seed, P4.4's form, its variants in the file's order."""
    spec = yaml.safe_load(path.read_text())
    if "seeds" in spec:
        raise FailoverError(f"{path.name} names seeds: each is its own experiment")
    stem = f"{path.stem}-{experiment_hash(path)}"
    return _experiment(path, spec, spec["seed"], _variants(spec["variants"]), stem)


def load_failover_seeds(path: Path) -> list[FailoverExperiment]:
    """Each seed's experiment, in the file's order of seeds (P5.3); a file of one seed is one."""
    spec = yaml.safe_load(path.read_text())
    if "seeds" not in spec:
        return [load_failover(path)]
    if "seed" in spec:
        raise FailoverError("a file names one seed or a list of seeds, not both")
    variants = {variant.name: variant for variant in _variants(spec["variants"])}
    seeds = [entry["seed"] for entry in spec["seeds"]]
    if not seeds or len(set(seeds)) != len(seeds):
        raise FailoverError(f"the seeds must be named, each once, got {seeds}")
    stem = f"{path.stem}-{experiment_hash(path)}"
    experiments = []
    for entry in spec["seeds"]:
        order = entry["order"]
        if sorted(order) != sorted(variants):
            raise FailoverError(
                f"seed {entry['seed']}: the order must name each variant once, got {order}"
            )
        experiments.append(
            _experiment(
                path,
                spec,
                entry["seed"],
                [variants[name] for name in order],
                f"{stem}-s{entry['seed']}",
            )
        )
    return experiments


def variant_profile(profile: Mapping[str, Any], retry: bool) -> dict[str, Any]:
    """The profile with the variant's replay.retry; the agent's retry rule stays the profile's."""
    return {**profile, "replay": {**profile["replay"], "retry": retry}}


def check_pool(
    name: str, profile: Mapping[str, Any], chart: str, values: Mapping[str, Any]
) -> None:
    """chart (<name>-<version>, as helm list shows it) and values are the pods' release's: the
    simulator's chart for sim, vLLM's for any other profile, with config/<name>.yaml's model, which
    also tells real from real-plain (the adapter) and demo (the model)."""
    wanted_chart = "sim" if name == SIM_PROFILE else "vllm"
    if chart.rsplit("-", 1)[0] != wanted_chart or values.get("model") != profile["model"]:
        raise FailoverError(
            f"the cluster's pods are not config/{name}.yaml's: run make up PROFILE={name} first"
        )


def deployed_pool() -> tuple[str, dict[str, Any]]:
    """The pods' release's chart and values."""
    releases = json.loads(helm("list", "--filter", f"^{POOL_RELEASE}$", "-o", "json"))
    if len(releases) != 1:
        raise FailoverError(f"no Helm release {POOL_RELEASE}: run make up first")
    values: dict[str, Any] = json.loads(helm("get", "values", POOL_RELEASE, "-o", "json"))
    return releases[0]["chart"], values


def retry_starts(log: str) -> dict[tuple[str, int], list[int]]:
    """When each retried call's failed attempts were sent, by run and step, in the replay's
    printed lines."""
    starts: dict[tuple[str, int], list[int]] = {}
    for text in log.splitlines():
        match = RETRY_LINE.match(text)
        if match is not None:
            starts.setdefault((match["run"], int(match["step"])), []).append(int(match["sent"]))
    return starts


def picked_before(picker: str, killed_ms: int) -> dict[tuple[str, int], str]:
    """Each call's pod by run and step: the last pick the plugin logged for it before the kill, on
    the VM's clock as ts_start. A fanned-out step's calls share one key."""
    killed = datetime.fromtimestamp(killed_ms / 1000, UTC)
    picks = {}
    for line in picker_lines(picker):
        if line.get("body") == PICKED and datetime.fromisoformat(line["timestamp"]) < killed:
            picks[(line["run-id"], int(line["step"]))] = endpoint_pod(line["pod"])
    return picks


@dataclass(frozen=True)
class FailoverReport:
    killed_pod: str
    retry: bool
    runs: int
    # Runs with no failed call.
    done: int
    # Runs whose failed calls all failed on their deadline: overload, not the kill.
    deadline_runs: list[str]
    # "<run> step <n>: <outcome>" for each call running on the killed pod at the kill. A failed
    # attempt names no pod: the picker's pick of it stands for it, else the run's pin at the kill
    # (P3.4); a first call has no pin (failover-576b813b6b4c).
    on_killed: list[str]
    # The runs of those calls, each once.
    on_killed_runs: list[str]
    # "<run> step <n>: <outcome>" for each call the kill cut: on the killed pod, running at the
    # kill or sent during the settle, with an attempt that failed for a reason but its deadline.
    cut: list[str]
    # The verdict needs a cut call (P5.3b); else one running on the killed pod will do (P4.4).
    require_cut: bool
    # Retries printed during the variant, for any reason.
    retries: int
    # Runs with a call running at the kill or sent during the settle that failed for a reason other
    # than its deadline.
    failed_by_kill: list[str]
    # Runs pinned to the killed pod at the kill that had not finished, and those of them with a
    # remapped line from it in the picker's log.
    affected: list[str]
    remapped: list[str]

    @property
    def completion_rate(self) -> float:
        return self.done / self.runs

    @property
    def void(self) -> bool:
        return not (self.cut if self.require_cut else self.on_killed)

    @property
    def passed(self) -> bool:
        return self.retry and not self.void and not self.failed_by_kill

    @property
    def verdict(self) -> str:
        if not self.retry:
            return "control"
        if self.void:
            return "void"
        return "passed" if self.passed else "failed"


def completion(
    lines: Sequence[Mapping[str, Any]], reasons: Mapping[tuple[str, int], Sequence[str]]
) -> tuple[int, int, list[str]]:
    """The runs, those with no failed call, and those whose failed calls all failed on their
    deadline: overload, not the kill."""
    runs = {line["run_id"] for line in lines}
    failed_runs = {line["run_id"] for line in lines if "duration_ms" not in line}
    deadline_runs = sorted(
        run
        for run in failed_runs
        if all(
            text == DEADLINE_REASON
            for line in lines
            if line["run_id"] == run and "duration_ms" not in line
            for text in reasons.get((run, line["step"]), [NO_REASON])
        )
    )
    return len(runs), len(runs - failed_runs), deadline_runs


def retry_lost_ms(lines: Sequence[Mapping[str, Any]], log: str) -> dict[tuple[str, int], int]:
    """Each retried step's time from its first attempt to its last, by run and step: the failed
    attempts and the backoffs. On the wall clock, as the retry line's sent time and ts_start are,
    so a step of the VM's clock (P2.3) inside it is in it too. A fanned-out step's calls share a
    key and one retry line's attempts, so its time ends at the latest of their starts."""
    starts = retry_starts(log)
    last: dict[tuple[str, int], int] = {}
    for line in lines:
        key = (line["run_id"], line["step"])
        if key in starts:
            last[key] = max(last.get(key, line["ts_start"]), line["ts_start"])
    return {key: last[key] - min(starts[key]) for key in last}


def run_times_ms(lines: Sequence[Mapping[str, Any]], log: str) -> dict[str, int | None]:
    """Each run's time from its first call to its last reply, None for a run with a failed call:
    each step's slowest call and its gap, on the replay's monotonic clock, as
    results.run_completion_ms counts it. A later call's gap runs from the step before's end to its
    last attempt (replay._call), so it holds a retry's lost time; a first call's gap is 0, so a
    retried first call's lost time is added from retry_lost_ms."""
    steps: dict[str, dict[int, int]] = {}
    failed = set()
    for line in lines:
        by_step = steps.setdefault(line["run_id"], {})
        if "duration_ms" not in line:
            failed.add(line["run_id"])
            continue
        took = line["gap_before_ms"] + line["duration_ms"]
        by_step[line["step"]] = max(by_step.get(line["step"], 0), took)
    lost = retry_lost_ms(lines, log)
    return {
        run_id: None
        if run_id in failed
        else sum(by_step.values()) + lost.get((run_id, min(by_step)), 0)
        for run_id, by_step in steps.items()
    }


@dataclass(frozen=True)
class GroupCost:
    # The group's runs that finished in both replays, and their extra time over the no-kill's.
    runs: int
    median_ms: float | None
    max_ms: int | None


@dataclass(frozen=True)
class RecoveryCost:
    """A kill variant's runs against the same runs in the no-kill variant at its seed (P5.3)."""

    # Runs with a call running on the killed pod at the kill.
    on_killed: GroupCost
    # Runs pinned to the killed pod at the kill and not finished, with no call running on it: their
    # later steps lose their cached context.
    pinned: GroupCost
    # The rest: the noise between two replays and the one pod left carrying the load.
    others: GroupCost
    # Runs that did not finish in one of the two, which have no time to pair.
    unpaired: list[str]
    # Steps sent again, and their lost time in all (retry_lost_ms).
    retried_steps: int
    lost_ms: int


def _group(extra: Mapping[str, int], runs: set[str]) -> GroupCost:
    values = [extra[run] for run in runs if run in extra]
    if not values:
        return GroupCost(0, None, None)
    return GroupCost(len(values), statistics.median(values), max(values))


def recovery_cost(
    killed: Mapping[str, int | None],
    reference: Mapping[str, int | None],
    on_killed_runs: Sequence[str],
    affected: Sequence[str],
    lost: Mapping[tuple[str, int], int],
) -> RecoveryCost:
    """killed and reference are run_times_ms of the kill variant and the no-kill one: the same
    runs, the same seed. Each run is in one group, the first it fits."""
    extra = {}
    unpaired = []
    for run, took in sorted(killed.items()):
        before = reference.get(run)
        if took is None or before is None:
            unpaired.append(run)
        else:
            extra[run] = took - before
    hit = set(on_killed_runs)
    pinned = set(affected) - hit
    return RecoveryCost(
        on_killed=_group(extra, hit),
        pinned=_group(extra, pinned),
        others=_group(extra, set(extra) - hit - pinned),
        unpaired=unpaired,
        retried_steps=len(lost),
        lost_ms=sum(lost.values()),
    )


def _outcome(line: Mapping[str, Any], retries: int, reasons: Sequence[str]) -> str:
    said = f"retried {retries}, " if retries else ""
    if "duration_ms" in line:
        return f"{said}answered by {line.get('pod')}"
    return f"{said}failed, {'; '.join(reasons)}"


def build_failover_report(
    lines: Sequence[Mapping[str, Any]],
    log: str,
    picker: str,
    killed_pod: str,
    killed_ms: int,
    settle_ms: int,
    retry: bool,
    require_cut: bool = False,
) -> FailoverReport:
    """A call is placed by when its first attempt was sent, and its end is its last attempt's."""
    reasons = failure_reasons(log)
    starts = retry_starts(log)
    pins = pinned_pods(lines, killed_ms)
    picks = picked_before(picker, killed_ms)
    on_killed = []
    on_killed_runs = set()
    cut = []
    by_kill = set()
    for line in lines:
        key = (line["run_id"], line["step"])
        attempts = starts.get(key, [])
        first = min([*attempts, line["ts_start"]])
        placed: dict[str, Any] = {"ts_start": first}
        if "duration_ms" in line:
            placed["duration_ms"] = line["ts_start"] + line["duration_ms"] - first
        group = group_of(placed, killed_ms, settle_ms)
        texts = reasons.get(key, [NO_REASON])
        failed = "duration_ms" not in line
        in_window = group in (RUNNING_AT_DELETE, SENT_DURING_SETTLE)
        not_deadline = any(text != DEADLINE_REASON for text in texts)
        if in_window and failed and not_deadline:
            by_kill.add(line["run_id"])
        # The line names the pod that answered, which is the one the call ran on unless it was
        # sent again or got no reply.
        unnamed = attempts or failed
        pod = picks.get(key, pins.get(line["run_id"])) if unnamed else line.get("pod")
        said = f"{line['run_id']} step {line['step']}: {_outcome(line, len(attempts), texts)}"
        if group == RUNNING_AT_DELETE and pod == killed_pod:
            on_killed.append(said)
            on_killed_runs.add(line["run_id"])
        # A retried attempt failed for a reason the retry covers, which the deadline is not.
        if in_window and pod == killed_pod and (attempts or (failed and not_deadline)):
            cut.append(said)
    runs, done, deadline_runs = completion(lines, reasons)
    going_on = {
        line["run_id"] for line in lines if group_of(line, killed_ms, settle_ms) != FINISHED_BEFORE
    }
    affected = sorted(run for run, pod in pins.items() if pod == killed_pod and run in going_on)
    moved = {
        line["run-id"]
        for line in picker_lines(picker)
        if line.get("body") == PICKED
        and line.get("reason") == REMAPPED
        and endpoint_pod(line.get("from", "")) == killed_pod
    }
    return FailoverReport(
        killed_pod=killed_pod,
        retry=retry,
        runs=runs,
        done=done,
        deadline_runs=deadline_runs,
        on_killed=on_killed,
        on_killed_runs=sorted(on_killed_runs),
        cut=cut,
        require_cut=require_cut,
        retries=sum(len(attempts) for attempts in starts.values()),
        failed_by_kill=sorted(by_kill),
        affected=affected,
        remapped=[run for run in affected if run in moved],
    )


def report_lines(variant: str, report: FailoverReport) -> list[str]:
    completion = (
        f"completion {report.done} of {report.runs} runs ({100 * report.completion_rate:.1f}%)"
    )
    by_kill = f"{len(report.failed_by_kill)} failed by the kill {report.failed_by_kill}"
    lines = [
        f"  killed pod {report.killed_pod}",
        f"  calls running on it at the kill: {len(report.on_killed)}",
        *(f"    {text}" for text in report.on_killed),
        f"  calls on it cut by the kill: {len(report.cut)}",
        *(f"    {text}" for text in report.cut),
        f"  retries: {report.retries}",
        f"  runs failed on their deadline only: {len(report.deadline_runs)} {report.deadline_runs}",
        f"  runs pinned to it at the kill: {len(report.affected)} {report.affected}; "
        f"remapped off it: {len(report.remapped)} {report.remapped}",
    ]
    said = {
        "control": f"control, {completion}, {by_kill}",
        "passed": f"passed, {completion}",
        "void": (
            "void, no call on the killed pod was cut by the kill"
            if report.require_cut
            else "void, no call was running on the killed pod at the kill"
        ),
        "failed": f"failed, {completion}, {by_kill}",
    }
    lines.append(f"{variant}: {said[report.verdict]}")
    return lines


def _seconds(milliseconds: float | None) -> str:
    return "-" if milliseconds is None else f"{milliseconds / 1000:.1f} s"


def recovery_lines(reference: str, cost: RecoveryCost) -> list[str]:
    groups = [
        ("a call on the killed pod at the kill", cost.on_killed),
        ("pinned there only", cost.pinned),
        ("the rest", cost.others),
    ]
    return [
        f"  recovery against {reference}, extra time per run:",
        *(
            f"    {name}: {group.runs} runs, median {_seconds(group.median_ms)}, "
            f"max {_seconds(group.max_ms)}"
            for name, group in groups
        ),
        f"    not finished in one of the two: {len(cost.unpaired)} {cost.unpaired}",
        f"    steps sent again: {cost.retried_steps}, lost {_seconds(cost.lost_ms)} in all",
    ]


def _now_ms() -> int:
    return round(time.time() * 1000)


@dataclass
class Kill:
    # The Ready pods at the start, and where the picker's log of the replay starts.
    ready: list[str]
    since: str
    # Stop the pod's containers at once before the delete (P5.3b).
    stop_container: bool = False
    # Set as the timer's thread starts, so a replay that ended before the kill is told apart.
    started: bool = False
    # The pod killed, each Ready pod's live pins then, and when the delete was asked for, on the
    # wall clock as ts_start.
    pod: str | None = None
    pins: dict[str, int] | None = None
    killed_ms: int | None = None
    # kubectl's or crictl's message when the log read, the stop or the delete failed.
    error: str | None = None


def stop_containers(pod: str) -> None:
    """The pod's running containers stopped with no timeout: containerd then sends SIGKILL with no
    SIGTERM first. The pod is found by its ready sandbox, which must be the one."""
    sandboxes = crictl("pods", "--name", f"^{pod}$", "--state", "ready", "-q").split()
    if len(sandboxes) != 1:
        raise FailoverError(f"{pod} has {len(sandboxes)} ready sandboxes in crictl, not 1")
    containers = crictl("ps", "--pod", sandboxes[0], "--state", "running", "-q").split()
    crictl("stop", "--timeout", "0", *containers)


def kill_pod(state: Kill, wait_for_calls_seconds: float) -> None:
    """Runs in a timer's thread while the replay runs. Nothing is killed, and the variant reports
    void, when no pod holds a live pin within the wait."""
    state.started = True
    try:
        give_up = time.monotonic() + wait_for_calls_seconds
        while True:
            picker = kubectl("logs", f"deployment/{EPP_DEPLOYMENT}", f"--since-time={state.since}")
            pins = live_pins(picker_lines(picker), state.since)
            pod = busiest_pod(pins, state.ready)
            if pod is not None:
                break
            if time.monotonic() >= give_up:
                print(
                    f"failover: no pod held a live pin within {wait_for_calls_seconds} s",
                    flush=True,
                )
                return
            time.sleep(PICKER_POLL_SECONDS)
        state.pins = choose_pod(pins, state.ready)[1]
        print(f"failover: killing {pod}, live pins {state.pins}", flush=True)
        killed_ms = _now_ms()
        if state.stop_container:
            stop_containers(pod)
        kubectl("delete", "pod", pod, "--grace-period=0", "--force", "--wait=false")
    except subprocess.CalledProcessError as error:
        state.error = command_failure(error)
        print(f"failover: {state.error}", flush=True)
        return
    except FailoverError as error:
        state.error = str(error)
        print(f"failover: {state.error}", flush=True)
        return
    state.pod, state.killed_ms = pod, killed_ms


def current_picker_restarts() -> int:
    deployment = json.loads(kubectl("get", "deployment", EPP_DEPLOYMENT, "-o", "json"))
    labels = deployment["spec"]["selector"]["matchLabels"]
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    return picker_restarts(json.loads(kubectl("get", "pods", "-l", selector, "-o", "json")))


def run_command(command: list[str]) -> None:
    subprocess.run(command, cwd=REPO_ROOT, check=True)


@dataclass(frozen=True)
class VariantResult:
    variant: FailoverVariant
    kill: Kill
    lines: list[dict[str, Any]]
    picker: str
    picker_path: Path
    # The picker's container restarts during the replay (P3.11).
    picker_restarts: int
    out: Path
    # kubectl's message when the replacement pod was not Ready in time.
    replacement_error: str | None


def run_variant(
    experiment: FailoverExperiment,
    variant: FailoverVariant,
    profile: Mapping[str, Any],
    results: Path,
) -> VariantResult:
    stem = f"{experiment.stem}-{variant.name}"
    out = results / f"{stem}.jsonl"
    # Every variant starts from empty caches and the profile's pods, the last kill's replacement
    # included.
    run_command(["bash", str(REPO_ROOT / "infra" / "restart-vllm.sh")])
    pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
    ready = sorted(pods_to_warm(pods, profile["replicas"]))
    set_routing(experiment.profile, None, experiment.routing)
    restarts = current_picker_restarts()
    # After set-routing.sh, as P3.4's: live_pins compares each line's own timestamp with this.
    since = datetime.now(UTC).isoformat()
    state = Kill(ready, since, experiment.stop_container)
    # The no-kill variant's timer kills nothing and is never started.
    timer = threading.Timer(
        experiment.kill_after_seconds, kill_pod, (state, experiment.wait_for_calls_seconds)
    )
    # stderr too: a kill thread that dies prints its traceback there (threading.excepthook), and
    # it is kept with the result (P3.4's second review).
    with (
        log_path(out).open("x") as log,
        contextlib.redirect_stdout(Tee(sys.stdout, log)),
        contextlib.redirect_stderr(Tee(sys.stderr, log)),
    ):
        if variant.kill:
            timer.start()
        try:
            lines = replay_to_file(
                variant_profile(profile, variant.retry),
                experiment.trace,
                experiment.pattern,
                experiment.seed,
                out,
                plugin_on(profile["routing"], experiment.routing),
            )
        finally:
            if variant.kill:
                timer.cancel()
                timer.join()
    # Kept before anything else can fail: switching the routing back restarts the picker, and its
    # log with it (P3.4's second review).
    picker = kubectl("logs", f"deployment/{EPP_DEPLOYMENT}", f"--since-time={since}")
    picker_path = results / f"{stem}-picker.log"
    picker_path.write_text(picker)
    replacement_error = None
    if variant.kill:
        if not state.started:
            raise FailoverError(
                f"the replay ended before the kill at {experiment.kill_after_seconds} s"
            )
        try:
            kubectl("rollout", "status", VLLM_DEPLOYMENT, f"--timeout={SCALE_TIMEOUT}")
        except subprocess.CalledProcessError as error:
            replacement_error = kubectl_failure(error)
            print(f"failover: the replacement pod: {replacement_error}", flush=True)
    return VariantResult(
        variant,
        state,
        lines,
        picker,
        picker_path,
        current_picker_restarts() - restarts,
        out,
        replacement_error,
    )


def run_failover(
    experiment: FailoverExperiment, profile: Mapping[str, Any], results: Path
) -> list[VariantResult]:
    """Every variant, then the profile's routing back, whatever happened."""
    if profile["replicas"] < 2:
        raise FailoverError(
            f"the profile runs {profile['replicas']} pod; a failover needs at least 2"
        )
    check_pool(experiment.profile, profile, *deployed_pool())
    done = []
    try:
        for variant in experiment.variants:
            done.append(run_variant(experiment, variant, profile, results))
    finally:
        set_routing(experiment.profile, None, None)
    return done


@dataclass(frozen=True)
class NoKillReport:
    """The no-kill variant's own numbers, as a kill variant's report counts them."""

    runs: int
    done: int
    deadline_runs: list[str]
    retries: int

    @property
    def completion_rate(self) -> float:
        return self.done / self.runs


def no_kill_report(lines: Sequence[Mapping[str, Any]], log: str) -> NoKillReport:
    runs, done, deadline_runs = completion(lines, failure_reasons(log))
    retries = sum(len(attempts) for attempts in retry_starts(log).values())
    return NoKillReport(runs, done, deadline_runs, retries)


def variant_summary(
    result: VariantResult,
    report: FailoverReport | NoKillReport | None,
    recovery: RecoveryCost | None,
) -> dict[str, Any]:
    """report is None for a kill variant whose kill did not happen."""
    kill = result.kill
    summary: dict[str, Any] = {
        "name": result.variant.name,
        "retry": result.variant.retry,
        "kill": result.variant.kill,
        "killed_pod": kill.pod,
        "live_pins_at_kill": kill.pins,
        "picker_log_since": kill.since,
        "kill_requested_ms": kill.killed_ms,
        "kill_error": kill.error,
        "replacement_error": result.replacement_error,
        "picker_restarts": result.picker_restarts,
        "passed": isinstance(report, FailoverReport) and report.passed,
    }
    if isinstance(report, NoKillReport):
        summary.update(
            verdict="reference",
            runs=report.runs,
            done=report.done,
            completion_rate=report.completion_rate,
            deadline_runs=report.deadline_runs,
            retries=report.retries,
        )
    elif isinstance(report, FailoverReport):
        summary.update(
            verdict=report.verdict,
            runs=report.runs,
            done=report.done,
            completion_rate=report.completion_rate,
            deadline_runs=report.deadline_runs,
            on_killed=report.on_killed,
            on_killed_runs=report.on_killed_runs,
            cut=report.cut,
            retries=report.retries,
            failed_by_kill=report.failed_by_kill,
            affected_runs=report.affected,
            remapped_runs=report.remapped,
            void=report.void,
        )
    else:
        summary["verdict"] = "void"
    if recovery is not None:
        summary["recovery"] = dataclasses.asdict(recovery)
    return summary


def _on(flag: bool) -> str:
    return "on" if flag else "off"


def variant_report(
    seed: int, result: VariantResult, settle_ms: int, require_cut: bool = False
) -> FailoverReport | NoKillReport | None:
    """Printed as it is made. None for a kill variant whose kill did not happen."""
    variant, kill = result.variant, result.kill
    log = log_path(result.out).read_text()
    print(
        f"failover: seed {seed}, {variant.name}, "
        f"retry {_on(variant.retry)}, kill {_on(variant.kill)}"
    )
    print(f"  picker restarts during the replay: {result.picker_restarts}")
    if not variant.kill:
        plain = no_kill_report(result.lines, log)
        print(
            f"{variant.name}: reference, completion {plain.done} of {plain.runs} runs "
            f"({100 * plain.completion_rate:.1f}%), retries {plain.retries}, "
            f"failed on their deadline only {plain.deadline_runs}"
        )
        return plain
    if kill.pod is None or kill.killed_ms is None:
        print(f"{variant.name}: void, no pod held a live pin, so none was killed")
        return None
    report = build_failover_report(
        result.lines,
        log,
        result.picker,
        kill.pod,
        kill.killed_ms,
        settle_ms,
        variant.retry,
        require_cut,
    )
    for line in report_lines(variant.name, report):
        print(line)
    return report


def run_seed(
    experiment: FailoverExperiment,
    profile: Mapping[str, Any],
    profile_text: str,
    results: Path,
) -> dict[str, Any]:
    """One seed's variants, its report and its .meta.json, which is returned."""
    settle_ms = round(experiment.settle_seconds * 1000)
    done = run_failover(experiment, profile, results)
    reports = [
        variant_report(experiment.seed, result, settle_ms, experiment.require_cut)
        for result in done
    ]
    references = [result for result in done if not result.variant.kill]
    reference_times = (
        run_times_ms(references[0].lines, log_path(references[0].out).read_text())
        if references
        else None
    )
    summaries = []
    for result, report in zip(done, reports, strict=True):
        recovery = None
        if reference_times is not None and isinstance(report, FailoverReport):
            log = log_path(result.out).read_text()
            recovery = recovery_cost(
                run_times_ms(result.lines, log),
                reference_times,
                report.on_killed_runs,
                report.affected,
                retry_lost_ms(result.lines, log),
            )
            print(f"failover: seed {experiment.seed}, {result.variant.name}")
            for line in recovery_lines(references[0].variant.name, recovery):
                print(line)
        summaries.append(variant_summary(result, report, recovery))
    meta = {
        "experiment": experiment.name,
        "hash": experiment.hash,
        "source": source_state(SOURCE_ROOT),
        "profile": experiment.profile,
        "profile_sha256": hashlib.sha256(profile_text.encode()).hexdigest(),
        "trace": experiment.trace.name,
        "pattern": experiment.pattern,
        "seed": experiment.seed,
        "routing": dict(experiment.routing),
        "kill_after_seconds": experiment.kill_after_seconds,
        "wait_for_calls_seconds": experiment.wait_for_calls_seconds,
        "settle_seconds": experiment.settle_seconds,
        "stop_container": experiment.stop_container,
        "require_cut": experiment.require_cut,
        "agent_retry": {
            "llm_retries": profile["agent"]["llm_retries"],
            "llm_retry_backoff_seconds": profile["agent"]["llm_retry_backoff_seconds"],
        },
        "variants": summaries,
    }
    meta_path = results / f"{experiment.stem}.meta.json"
    with meta_path.open("x") as file:
        json.dump(meta, file, indent=2)
        file.write("\n")
    print(
        f"failover: wrote {experiment.stem}-<variant>.jsonl, .jsonl.log, -picker.log and "
        f"{meta_path.name}"
    )
    return meta


SUMMARY_FIELDS = (
    "seed",
    "variant",
    "retry",
    "kill",
    "verdict",
    "runs",
    "done",
    "completion_rate",
    "failed_by_kill",
    "deadline_runs",
    "calls_on_killed_pod",
    "cut_calls",
    "retries",
    "on_killed_runs",
    "on_killed_median_s",
    "on_killed_max_s",
    "pinned_runs",
    "pinned_median_s",
    "pinned_max_s",
    "others_runs",
    "others_median_s",
    "others_max_s",
    "unpaired_runs",
    "retried_steps",
    "lost_s",
)


def _in_seconds(milliseconds: float | None) -> float | None:
    return None if milliseconds is None else milliseconds / 1000


def summary_row(seed: int, variant: Mapping[str, Any]) -> dict[str, Any]:
    """One seed's variant from its .meta.json; a number it has not got is a blank."""
    row: dict[str, Any] = {
        "seed": seed,
        "variant": variant["name"],
        "retry": variant["retry"],
        "kill": variant["kill"],
        "verdict": variant["verdict"],
        "runs": variant.get("runs"),
        "done": variant.get("done"),
        "completion_rate": variant.get("completion_rate"),
        "deadline_runs": len(variant.get("deadline_runs", [])),
        "retries": variant.get("retries"),
    }
    if "failed_by_kill" in variant:
        row["failed_by_kill"] = len(variant["failed_by_kill"])
        row["calls_on_killed_pod"] = len(variant["on_killed"])
    # P5.3's .meta.json files have none.
    if "cut" in variant:
        row["cut_calls"] = len(variant["cut"])
    recovery = variant.get("recovery")
    if recovery is not None:
        for group in ("on_killed", "pinned", "others"):
            row[f"{group}_runs"] = recovery[group]["runs"]
            row[f"{group}_median_s"] = _in_seconds(recovery[group]["median_ms"])
            row[f"{group}_max_s"] = _in_seconds(recovery[group]["max_ms"])
        row["unpaired_runs"] = len(recovery["unpaired"])
        row["retried_steps"] = recovery["retried_steps"]
        row["lost_s"] = _in_seconds(recovery["lost_ms"])
    return row


def write_summary(path: Path, metas: Sequence[Mapping[str, Any]]) -> None:
    """Written again after every run, from the seeds' .meta.json files alone."""
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for meta in metas:
            for variant in meta["variants"]:
                writer.writerow(summary_row(meta["seed"], variant))


def completion_over_seeds(
    metas: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[float, float, float]]:
    """Each variant's completion rate over the seeds that counted it: mean, lowest, highest."""
    rates: dict[str, list[float]] = {}
    for meta in metas:
        for variant in meta["variants"]:
            if "completion_rate" in variant:
                rates.setdefault(variant["name"], []).append(variant["completion_rate"])
    return {
        name: (statistics.mean(values), min(values), max(values)) for name, values in rates.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="P4.4: a replay with a pod killed, retried or not")
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()

    experiments = load_failover_seeds(args.experiment)
    first = experiments[0]
    profile_path = REPO_ROOT / "config" / f"{first.profile}.yaml"
    profile_text = profile_path.read_text()
    profile = yaml.safe_load(profile_text)
    results = REPO_ROOT / "experiments" / "results"
    finished = {}
    for experiment in experiments:
        meta_path = results / f"{experiment.stem}.meta.json"
        if meta_path.exists():
            finished[experiment.seed] = json.loads(meta_path.read_text())
            continue
        found = sorted(results.glob(f"{experiment.stem}*"))
        if found:
            raise FailoverError(
                f"seed {experiment.seed} stopped halfway, delete its results to run it again: "
                f"{found}"
            )
    if len(finished) == len(experiments):
        raise FailoverError(
            f"results of this file exist for every seed, delete them to run it again: "
            f"{sorted(results.glob(f'{first.name}-{first.hash}*'))}"
        )
    if first.stop_container:
        try:
            crictl("version")
        except subprocess.CalledProcessError as error:
            raise FailoverError(
                f"the kill stops the pod's containers through sudo, run sudo -v first: "
                f"{command_failure(error)}"
            ) from error
    metas = []
    for experiment in experiments:
        if experiment.seed in finished:
            print(f"failover: seed {experiment.seed} finished before, skipped")
            metas.append(finished[experiment.seed])
        else:
            metas.append(run_seed(experiment, profile, profile_text, results))
    summary_path = results / f"{first.name}-{first.hash}-summary.csv"
    write_summary(summary_path, metas)
    print(f"failover: over {len(metas)} seeds, wrote {summary_path.name}")
    for name, (mean, low, high) in completion_over_seeds(metas).items():
        print(
            f"  {name}: completion mean {100 * mean:.1f}%, "
            f"{100 * low:.1f}-{100 * high:.1f}% over the seeds"
        )
    judged = [
        variant["verdict"] == "passed"
        for meta in metas
        for variant in meta["variants"]
        if variant["kill"] and variant["retry"]
    ]
    if not all(judged):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
