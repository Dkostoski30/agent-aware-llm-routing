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

Usage: python -m load_generator.failover EXPERIMENT
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
    VLLM_DEPLOYMENT,
    VLLM_SELECTOR,
    plugin_on,
    pods_to_warm,
    set_routing,
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


@dataclass(frozen=True)
class FailoverVariant:
    name: str
    # The replay's replay.retry for this variant: off is the replay as before P4.4.
    retry: bool


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
    variants: list[FailoverVariant]


def load_failover(path: Path) -> FailoverExperiment:
    """A missing key raises KeyError. Rules the profile lacks are refused by set-routing.sh, before
    any replay."""
    spec = yaml.safe_load(path.read_text())
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
    trace = (path.parent / spec["trace"]).resolve()
    if not trace.exists():
        raise FailoverError(f"no trace {spec['trace']}")
    variants = []
    for section in spec["variants"]:
        if not isinstance(section["retry"], bool):
            raise FailoverError(f"variant {section['name']}: retry must be true or false")
        variants.append(FailoverVariant(section["name"], section["retry"]))
    names = [variant.name for variant in variants]
    if not names or len(set(names)) != len(names):
        raise FailoverError(f"the variants need names, each once, got {names}")
    return FailoverExperiment(
        name=path.stem,
        hash=experiment_hash(path),
        profile=spec["profile"],
        routing=routing,
        trace=trace,
        pattern=spec["pattern"],
        seed=spec["seed"],
        kill_after_seconds=spec["kill_after_seconds"],
        wait_for_calls_seconds=spec["wait_for_calls_seconds"],
        settle_seconds=spec["settle_seconds"],
        variants=variants,
    )


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
        return not self.on_killed

    @property
    def passed(self) -> bool:
        return self.retry and not self.void and not self.failed_by_kill


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
) -> FailoverReport:
    """A call is placed by when its first attempt was sent, and its end is its last attempt's."""
    reasons = failure_reasons(log)
    starts = retry_starts(log)
    pins = pinned_pods(lines, killed_ms)
    picks = picked_before(picker, killed_ms)
    on_killed = []
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
        if in_window and failed and any(text != DEADLINE_REASON for text in texts):
            by_kill.add(line["run_id"])
        # The line names the pod that answered, which is the one the call ran on unless it was
        # sent again or got no reply.
        unnamed = attempts or failed
        pod = picks.get(key, pins.get(line["run_id"])) if unnamed else line.get("pod")
        if group == RUNNING_AT_DELETE and pod == killed_pod:
            outcome = _outcome(line, len(attempts), texts)
            on_killed.append(f"{line['run_id']} step {line['step']}: {outcome}")
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
        runs=len(runs),
        done=len(runs - failed_runs),
        deadline_runs=deadline_runs,
        on_killed=on_killed,
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
        f"  retries: {report.retries}",
        f"  runs failed on their deadline only: {len(report.deadline_runs)} {report.deadline_runs}",
        f"  runs pinned to it at the kill: {len(report.affected)} {report.affected}; "
        f"remapped off it: {len(report.remapped)} {report.remapped}",
    ]
    if not report.retry:
        lines.append(f"{variant}: control, {completion}, {by_kill}")
    elif report.passed:
        lines.append(f"{variant}: passed, {completion}")
    elif report.void:
        lines.append(f"{variant}: void, no call was running on the killed pod at the kill")
    else:
        lines.append(f"{variant}: failed, {completion}, {by_kill}")
    return lines


def _now_ms() -> int:
    return round(time.time() * 1000)


@dataclass
class Kill:
    # The Ready pods at the start, and where the picker's log of the replay starts.
    ready: list[str]
    since: str
    # Set as the timer's thread starts, so a replay that ended before the kill is told apart.
    started: bool = False
    # The pod killed, each Ready pod's live pins then, and when the delete was asked for, on the
    # wall clock as ts_start.
    pod: str | None = None
    pins: dict[str, int] | None = None
    killed_ms: int | None = None
    # kubectl's message when the log read or the delete failed.
    error: str | None = None


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
        kubectl("delete", "pod", pod, "--grace-period=0", "--force", "--wait=false")
    except subprocess.CalledProcessError as error:
        state.error = kubectl_failure(error)
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
    stem = f"{experiment.name}-{experiment.hash}-{variant.name}"
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
    state = Kill(ready, since)
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
            timer.cancel()
            timer.join()
    # Kept before anything else can fail: switching the routing back restarts the picker, and its
    # log with it (P3.4's second review).
    picker = kubectl("logs", f"deployment/{EPP_DEPLOYMENT}", f"--since-time={since}")
    picker_path = results / f"{stem}-picker.log"
    picker_path.write_text(picker)
    if not state.started:
        raise FailoverError(
            f"the replay ended before the kill at {experiment.kill_after_seconds} s"
        )
    replacement_error = None
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


def variant_summary(result: VariantResult, report: FailoverReport | None) -> dict[str, Any]:
    kill = result.kill
    summary: dict[str, Any] = {
        "name": result.variant.name,
        "retry": result.variant.retry,
        "killed_pod": kill.pod,
        "live_pins_at_kill": kill.pins,
        "picker_log_since": kill.since,
        "kill_requested_ms": kill.killed_ms,
        "kill_error": kill.error,
        "replacement_error": result.replacement_error,
        "picker_restarts": result.picker_restarts,
        "passed": report is not None and report.passed,
    }
    if report is not None:
        summary.update(
            runs=report.runs,
            done=report.done,
            completion_rate=report.completion_rate,
            deadline_runs=report.deadline_runs,
            on_killed=report.on_killed,
            retries=report.retries,
            failed_by_kill=report.failed_by_kill,
            affected_runs=report.affected,
            remapped_runs=report.remapped,
            void=report.void,
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="P4.4: a replay with a pod killed, retried or not")
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()

    experiment = load_failover(args.experiment)
    profile_path = REPO_ROOT / "config" / f"{experiment.profile}.yaml"
    profile_text = profile_path.read_text()
    profile = yaml.safe_load(profile_text)
    results = REPO_ROOT / "experiments" / "results"
    stem = f"{experiment.name}-{experiment.hash}"
    found = sorted(results.glob(f"{stem}*"))
    if found:
        raise FailoverError(f"results of this file exist, delete them to run it again: {found}")
    settle_ms = round(experiment.settle_seconds * 1000)
    summaries = []
    judged = []
    for result in run_failover(experiment, profile, results):
        name = result.variant.name
        kill = result.kill
        print(f"failover: {name}, retry {'on' if result.variant.retry else 'off'}")
        print(f"  picker restarts during the replay: {result.picker_restarts}")
        report = None
        if kill.pod is None or kill.killed_ms is None:
            print(f"{name}: void, no pod held a live pin, so none was killed")
        else:
            report = build_failover_report(
                result.lines,
                log_path(result.out).read_text(),
                result.picker,
                kill.pod,
                kill.killed_ms,
                settle_ms,
                result.variant.retry,
            )
            for line in report_lines(name, report):
                print(line)
        judged.append(not result.variant.retry or (report is not None and report.passed))
        summaries.append(variant_summary(result, report))
    meta = {
        "experiment": experiment.name,
        "hash": experiment.hash,
        "profile": experiment.profile,
        "profile_sha256": hashlib.sha256(profile_text.encode()).hexdigest(),
        "trace": experiment.trace.name,
        "pattern": experiment.pattern,
        "seed": experiment.seed,
        "routing": dict(experiment.routing),
        "kill_after_seconds": experiment.kill_after_seconds,
        "wait_for_calls_seconds": experiment.wait_for_calls_seconds,
        "settle_seconds": experiment.settle_seconds,
        "agent_retry": {
            "llm_retries": profile["agent"]["llm_retries"],
            "llm_retry_backoff_seconds": profile["agent"]["llm_retry_backoff_seconds"],
        },
        "variants": summaries,
    }
    meta_path = results / f"{stem}.meta.json"
    with meta_path.open("x") as file:
        json.dump(meta, file, indent=2)
        file.write("\n")
    print(f"failover: wrote {stem}-<variant>.jsonl, .jsonl.log, -picker.log and {meta_path.name}")
    if not all(judged):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
