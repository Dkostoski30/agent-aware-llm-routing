"""make drain-check (P4.3): a replay with a scale-down partway, and whether the removed pod's calls
ended.

Per variant, in the file's order: the vLLM (or simulated) pods get the variant's drain and are
restarted, the picker runs the file's rules (affinity on, so runs are pinned), and a replay runs
in-process. scale_down_after_seconds after it starts, the Ready pod holding the most live pins in
the picker's log is given the lowest pod-deletion-cost and the Deployment is scaled down by one, so
the ReplicaSet removes that pod; with affinity on and steps a few ms apart, a pinned run nearly
always has a call on its pod (card 39). If no pod holds a pin within wait_for_calls_seconds, the
variant is not scaled and reports void. After the last variant the profile's pods and routing come
back, whatever happened.

The picker drops the pod from its pool as soon as Kubernetes sets its deletion time (llm-d-router
v0.11.0's IsPodReady), so its pinned runs move at once (P3.4's remap) and no new call reaches it;
what the drain decides is whether the calls already on it end. The report judges the calls running
at the scale-down and those sent during the settle (the picker learning of it, and the VM's wall
clock, which can step 2.5 s, P2.3): a run with one of them failed for any reason but its deadline
is interrupted. A drain passes with at least one call running on the removed pod at the scale-down,
no interrupted run, and no call sent after the settle answered by that pod. A variant without the
drain is the control: counted, not judged. Decided with the user, docs/task-cards/59-P4.3-drain.md.

Usage: python -m load_generator.drain EXPERIMENT
"""

import argparse
import contextlib
import hashlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

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
    SENT_AFTER,
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
from load_generator.record import REPO_ROOT, experiment_hash, kubectl
from load_generator.replay import replay_to_file

# What replay.py prints for a call that hit its deadline: the repr of the TimeoutError that
# asyncio.timeout raises around the whole call (replay._call). Such a call fails whatever the pod
# does, so it is counted apart from the interrupted ones.
DEADLINE_REASON = "TimeoutError()"
# The ReplicaSet removes the pod with the lowest cost first; every other pod has none, which counts
# as 0 (Kubernetes' controller.kubernetes.io/pod-deletion-cost annotation).
COST_ANNOTATION = "controller.kubernetes.io/pod-deletion-cost=-1"
# How often the scale-down reads the picker's log while it waits for a pinned pod. Fetching it takes
# about 2 s on a replay's DEBUG lines (P3.4), so a shorter wait would only overlap the fetches.
PICKER_POLL_SECONDS = 2.0


class DrainError(Exception):
    """The run must not start, or its result would not show a drain."""


@dataclass(frozen=True)
class DrainVariant:
    name: str
    # The pods' drain.enabled for this variant: off is the pods as they were before P4.3.
    drain: bool


@dataclass(frozen=True)
class DrainExperiment:
    # The file's stem, which names its results.
    name: str
    hash: str
    profile: str
    routing: Mapping[str, bool]
    trace: Path
    pattern: str
    seed: int
    scale_down_after_seconds: float
    wait_for_calls_seconds: float
    settle_seconds: float
    variants: list[DrainVariant]


def load_drain(path: Path) -> DrainExperiment:
    """A missing key raises KeyError. Rules the profile lacks are refused by set-routing.sh, before
    any replay."""
    spec = yaml.safe_load(path.read_text())
    routing = spec["routing"]
    if not isinstance(routing, dict) or not all(isinstance(on, bool) for on in routing.values()):
        raise DrainError(f"routing must map rules to true or false, got {routing}")
    for rule in ("plugin", "affinity"):
        if routing.get(rule) is not True:
            raise DrainError(f"routing.{rule} must be on: the pod removed is the one most pinned")
    if spec["scale_down_after_seconds"] <= 0:
        raise DrainError("scale_down_after_seconds must be after the replay starts")
    for key in ("wait_for_calls_seconds", "settle_seconds"):
        if spec[key] < 0:
            raise DrainError(f"{key} must not be negative")
    trace = (path.parent / spec["trace"]).resolve()
    if not trace.exists():
        raise DrainError(f"no trace {spec['trace']}")
    variants = []
    for section in spec["variants"]:
        if not isinstance(section["drain"], bool):
            raise DrainError(f"variant {section['name']}: drain must be true or false")
        variants.append(DrainVariant(section["name"], section["drain"]))
    names = [variant.name for variant in variants]
    if not names or len(set(names)) != len(names):
        raise DrainError(f"the variants need names, each once, got {names}")
    return DrainExperiment(
        name=path.stem,
        hash=experiment_hash(path),
        profile=spec["profile"],
        routing=routing,
        trace=trace,
        pattern=spec["pattern"],
        seed=spec["seed"],
        scale_down_after_seconds=spec["scale_down_after_seconds"],
        wait_for_calls_seconds=spec["wait_for_calls_seconds"],
        settle_seconds=spec["settle_seconds"],
        variants=variants,
    )


def busiest_pod(pins: Mapping[str, str], ready: Sequence[str]) -> str | None:
    """The Ready pod holding the most live pins, a tie going by name; None when none holds one."""
    pod, counts = choose_pod(pins, ready)
    return pod if counts[pod] > 0 else None


def removed_pods(pods: Mapping[str, Any]) -> list[str]:
    """The pods from kubectl get pods -o json that Kubernetes is removing."""
    return sorted(
        item["metadata"]["name"]
        for item in pods["items"]
        if "deletionTimestamp" in item["metadata"]
    )


def picker_restarts(pods: Mapping[str, Any]) -> int:
    """Every container restart of the picker's pods: one under load fails closed (P3.11), and the
    calls lost then are not the drain's."""
    return sum(
        status["restartCount"]
        for item in pods["items"]
        for status in item["status"].get("containerStatuses", [])
    )


def pool_command(profile: str, drain: bool) -> list[str]:
    """The pool's Helm release with the variant's drain and every other value as deployed: make
    vllm-deploy's or make sim-deploy's, from the profile (make up's rule)."""
    chart = REPO_ROOT / "infra" / "charts" / ("sim" if profile == SIM_PROFILE else "vllm")
    value = f"drain.enabled={'true' if drain else 'false'}"
    return [
        "helm",
        "upgrade",
        "vllm",
        str(chart),
        "--reuse-values",
        "--force-conflicts",
        "--set",
        value,
    ]


def restore_command(profile: str) -> list[str]:
    target = ["sim-deploy"] if profile == SIM_PROFILE else ["vllm-deploy", f"PROFILE={profile}"]
    return ["make", *target]


def run_command(command: list[str]) -> None:
    subprocess.run(command, cwd=REPO_ROOT, check=True)


@dataclass(frozen=True)
class DrainReport:
    removed_pod: str
    drain: bool
    # Calls running at the scale-down on the removed pod, and how many failed. A call with no reply
    # names no pod, so its run's pin at the scale-down stands for it (P3.4).
    running_on_removed: int
    running_on_removed_failed: int
    # "<run> step <n>: <reason>" for each failed call running at the scale-down or sent during the
    # settle, on any pod.
    window_failures: list[str]
    # Runs with such a call failed for a reason other than its deadline.
    interrupted: list[str]
    # Such calls that failed on their deadline only.
    deadline_failures: int
    # Failed calls sent after the settle, with their reasons: counted, not judged.
    after_failures: list[str]
    # Calls sent after the settle that the removed pod answered: it should have had none.
    answered_after_on_removed: int
    # Runs pinned to the removed pod at the scale-down that had not finished, and those of them with
    # a remapped line from it in the picker's log: the drain's cost, their cache.
    affected: list[str]
    remapped: list[str]

    @property
    def void(self) -> bool:
        return self.running_on_removed == 0

    @property
    def passed(self) -> bool:
        return (
            self.drain
            and not self.void
            and not self.interrupted
            and self.answered_after_on_removed == 0
        )


def _reason_texts(
    calls: Sequence[Mapping[str, Any]], reasons: Mapping[tuple[str, int], list[str]]
) -> dict[tuple[str, int], list[str]]:
    """Each failed call's reasons by run and step; a fanned-out step can have several."""
    keys = dict.fromkeys(
        (line["run_id"], line["step"]) for line in calls if "duration_ms" not in line
    )
    return {key: reasons.get(key, [NO_REASON]) for key in keys}


def build_drain_report(
    lines: Sequence[Mapping[str, Any]],
    log: str,
    picker: str,
    removed_pod: str,
    scaled_ms: int,
    settle_ms: int,
    drain: bool,
) -> DrainReport:
    reasons = failure_reasons(log)
    grouped: dict[str, list[Mapping[str, Any]]] = {
        name: [] for name in (FINISHED_BEFORE, RUNNING_AT_DELETE, SENT_DURING_SETTLE, SENT_AFTER)
    }
    for line in lines:
        grouped[group_of(line, scaled_ms, settle_ms)].append(line)
    window = _reason_texts(grouped[RUNNING_AT_DELETE] + grouped[SENT_DURING_SETTLE], reasons)
    interrupted = sorted(
        {run for (run, _), texts in window.items() if any(t != DEADLINE_REASON for t in texts)}
    )
    after = _reason_texts(grouped[SENT_AFTER], reasons)
    pins = pinned_pods(lines, scaled_ms)
    running = [
        line
        for line in grouped[RUNNING_AT_DELETE]
        if line.get("pod", pins.get(line["run_id"])) == removed_pod
    ]
    going_on = {
        line["run_id"]
        for name, calls in grouped.items()
        if name != FINISHED_BEFORE
        for line in calls
    }
    affected = sorted(run for run, pod in pins.items() if pod == removed_pod and run in going_on)
    moved = {
        line["run-id"]
        for line in picker_lines(picker)
        if line.get("body") == PICKED
        and line.get("reason") == REMAPPED
        and endpoint_pod(line.get("from", "")) == removed_pod
    }
    return DrainReport(
        removed_pod=removed_pod,
        drain=drain,
        running_on_removed=len(running),
        running_on_removed_failed=sum("duration_ms" not in line for line in running),
        window_failures=[
            f"{run} step {step}: {t}" for (run, step), ts in window.items() for t in ts
        ],
        interrupted=interrupted,
        deadline_failures=sum(all(t == DEADLINE_REASON for t in ts) for ts in window.values()),
        after_failures=[f"{run} step {step}: {t}" for (run, step), ts in after.items() for t in ts],
        answered_after_on_removed=sum(
            line.get("pod") == removed_pod for line in grouped[SENT_AFTER]
        ),
        affected=affected,
        remapped=[run for run in affected if run in moved],
    )


def report_lines(variant: str, report: DrainReport, gone_seconds: float | None) -> list[str]:
    gone = (
        "not gone by the end"
        if gone_seconds is None
        else f"gone {gone_seconds:.1f} s after the scale-down"
    )
    lines = [
        f"  removed pod {report.removed_pod}, {gone}",
        f"  calls running on it at the scale-down: {report.running_on_removed}, "
        f"{report.running_on_removed_failed} failed",
        f"  failed calls running at the scale-down or sent during the settle: "
        f"{len(report.window_failures)}, {report.deadline_failures} on their deadline only",
    ]
    lines += [f"    {text}" for text in report.window_failures]
    lines.append(f"  failed calls sent after the settle: {len(report.after_failures)}")
    lines += [f"    {text}" for text in report.after_failures]
    lines.append(
        f"  calls sent after the settle answered by {report.removed_pod}: "
        f"{report.answered_after_on_removed}"
    )
    lines.append(
        f"  runs pinned to it at the scale-down: {len(report.affected)} {report.affected}; "
        f"remapped off it: {len(report.remapped)} {report.remapped}"
    )
    interrupted = f"{len(report.interrupted)} interrupted runs {report.interrupted}"
    if not report.drain:
        lines.append(f"{variant}: control, {interrupted}")
    elif report.passed:
        lines.append(f"{variant}: passed")
    elif report.void:
        lines.append(f"{variant}: void, no call was running on the removed pod at the scale-down")
    else:
        why = []
        if report.interrupted:
            why.append(interrupted)
        if report.answered_after_on_removed:
            why.append(f"{report.answered_after_on_removed} calls answered after the settle")
        lines.append(f"{variant}: failed, {'; '.join(why)}")
    return lines


def _now_ms() -> int:
    return round(time.time() * 1000)


@dataclass
class ScaleDown:
    # The Ready pods at the start, where the picker's log of the replay starts, and the pod count.
    ready: list[str]
    since: str
    replicas: int
    # Set as the timer's thread starts, and once kubectl has taken the scale-down: a thread that
    # died in between is told from a replay that ended before the timer.
    started: bool = False
    scaled: bool = False
    # The pod given the lowest cost, each Ready pod's live pins then, and the pod Kubernetes took.
    chosen: str | None = None
    pins: dict[str, int] | None = None
    removed: str | None = None
    # When the scale-down was asked for and when the pod was gone, on the wall clock as ts_start.
    requested_ms: int | None = None
    gone_ms: int | None = None
    # Raised after the replay, so a scale-down that never happened cannot read as a pass.
    error: Exception | None = field(default=None)


def scale_down(state: ScaleDown, wait_for_calls_seconds: float) -> None:
    """Runs in a timer's thread while the replay runs. The pods are left as they are and the
    variant reports void when no pod holds a live pin within the wait. A failed kubectl, or a pod
    removed other than the one chosen, is kept for after the replay; a pod that outlives
    SCALE_TIMEOUT leaves gone_ms None."""
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
                    f"drain: no pod held a live pin within {wait_for_calls_seconds} s", flush=True
                )
                return
            time.sleep(PICKER_POLL_SECONDS)
        state.chosen, state.pins = pod, choose_pod(pins, state.ready)[1]
        print(f"drain: scaling down, {pod} first, live pins {state.pins}", flush=True)
        kubectl("annotate", "pod", pod, COST_ANNOTATION, "--overwrite")
        state.requested_ms = _now_ms()
        count = state.replicas - 1
        kubectl("scale", VLLM_DEPLOYMENT, f"--replicas={count}")
        state.scaled = True
        timeout = f"--timeout={SCALE_TIMEOUT}"
        kubectl("wait", VLLM_DEPLOYMENT, f"--for=jsonpath={{.status.replicas}}={count}", timeout)
        going = removed_pods(json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json")))
        if going != [pod]:
            raise DrainError(f"Kubernetes is removing {going}, not {pod}")
        state.removed = pod
    except (subprocess.CalledProcessError, DrainError) as error:
        state.error = error
        return
    try:
        kubectl("wait", f"pod/{pod}", "--for=delete", timeout)
    except subprocess.CalledProcessError:
        print(f"drain: {pod} not gone after {SCALE_TIMEOUT}", flush=True)
        return
    state.gone_ms = _now_ms()


def current_picker_restarts() -> int:
    deployment = json.loads(kubectl("get", "deployment", EPP_DEPLOYMENT, "-o", "json"))
    labels = deployment["spec"]["selector"]["matchLabels"]
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    return picker_restarts(json.loads(kubectl("get", "pods", "-l", selector, "-o", "json")))


@dataclass(frozen=True)
class VariantResult:
    variant: DrainVariant
    state: ScaleDown
    lines: list[dict[str, Any]]
    picker: str
    # The picker's container restarts during the replay.
    picker_restarts: int
    out: Path


def run_variant(
    experiment: DrainExperiment, variant: DrainVariant, profile: Mapping[str, Any], out: Path
) -> VariantResult:
    run_command(pool_command(experiment.profile, variant.drain))
    # Every variant starts from empty caches; the restart also waits for the upgrade's pods.
    run_command(["bash", str(REPO_ROOT / "infra" / "restart-vllm.sh")])
    pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
    ready = sorted(pods_to_warm(pods, profile["replicas"]))
    set_routing(experiment.profile, None, experiment.routing)
    restarts = current_picker_restarts()
    # After set-routing.sh, as P3.4's: live_pins compares each line's own timestamp with this.
    since = datetime.now(UTC).isoformat()
    state = ScaleDown(ready, since, profile["replicas"])
    timer = threading.Timer(
        experiment.scale_down_after_seconds,
        scale_down,
        (state, experiment.wait_for_calls_seconds),
    )
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
    if state.error is not None:
        raise state.error
    if not state.started:
        raise DrainError(
            f"the replay ended before the scale-down at {experiment.scale_down_after_seconds} s"
        )
    picker = kubectl("logs", f"deployment/{EPP_DEPLOYMENT}", f"--since-time={since}")
    return VariantResult(variant, state, lines, picker, current_picker_restarts() - restarts, out)


def run_drain(
    experiment: DrainExperiment, profile: Mapping[str, Any], results: Path
) -> list[VariantResult]:
    """Every variant, then the profile's pods and routing back, whatever happened."""
    if profile["replicas"] < 2:
        raise DrainError(
            f"the profile runs {profile['replicas']} pod; a scale-down needs at least 2"
        )
    done = []
    try:
        for variant in experiment.variants:
            out = results / f"{experiment.name}-{experiment.hash}-{variant.name}.jsonl"
            done.append(run_variant(experiment, variant, profile, out))
    finally:
        try:
            run_command(restore_command(experiment.profile))
        finally:
            set_routing(experiment.profile, None, None)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(
        description="P4.3: a replay with a scale-down, drained or not."
    )
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()

    experiment = load_drain(args.experiment)
    profile_path = REPO_ROOT / "config" / f"{experiment.profile}.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    results = REPO_ROOT / "experiments" / "results"
    stem = f"{experiment.name}-{experiment.hash}"
    found = sorted(results.glob(f"{stem}*"))
    if found:
        raise DrainError(f"results of this file exist, delete them to run it again: {found}")
    settle_ms = round(experiment.settle_seconds * 1000)
    summaries = []
    judged = []
    for result in run_drain(experiment, profile, results):
        name = result.variant.name
        picker_path = results / f"{stem}-{name}-picker.log"
        picker_path.write_text(result.picker)
        state = result.state
        print(f"drain: {name}, drain {'on' if result.variant.drain else 'off'}")
        summary: dict[str, Any] = {
            "name": name,
            "drain": result.variant.drain,
            "chosen_pod": state.chosen,
            "live_pins_at_scale_down": state.pins,
            "picker_log_since": state.since,
            "scale_down_requested_ms": state.requested_ms,
            "pod_gone_ms": state.gone_ms,
            "picker_restarts": result.picker_restarts,
        }
        if state.removed is None or state.requested_ms is None:
            print(f"{name}: void, no pod held a live pin, so none was removed")
            summary.update(removed_pod=None, passed=False)
            judged.append(not result.variant.drain)
            summaries.append(summary)
            continue
        report = build_drain_report(
            result.lines,
            log_path(result.out).read_text(),
            result.picker,
            state.removed,
            state.requested_ms,
            settle_ms,
            result.variant.drain,
        )
        gone = None if state.gone_ms is None else (state.gone_ms - state.requested_ms) / 1000
        print(f"  picker restarts during the replay: {result.picker_restarts}")
        for line in report_lines(name, report, gone):
            print(line)
        summary.update(
            removed_pod=report.removed_pod,
            running_on_removed=report.running_on_removed,
            running_on_removed_failed=report.running_on_removed_failed,
            window_failures=report.window_failures,
            interrupted_runs=report.interrupted,
            deadline_failures=report.deadline_failures,
            after_failures=report.after_failures,
            answered_after_on_removed=report.answered_after_on_removed,
            affected_runs=report.affected,
            remapped_runs=report.remapped,
            void=report.void,
            passed=report.passed,
        )
        judged.append(report.passed or not report.drain)
        summaries.append(summary)
    meta = {
        "experiment": experiment.name,
        "hash": experiment.hash,
        "profile": experiment.profile,
        "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        "trace": experiment.trace.name,
        "pattern": experiment.pattern,
        "seed": experiment.seed,
        "routing": dict(experiment.routing),
        "scale_down_after_seconds": experiment.scale_down_after_seconds,
        "wait_for_calls_seconds": experiment.wait_for_calls_seconds,
        "settle_seconds": experiment.settle_seconds,
        "drain": profile["drain"],
        "variants": summaries,
    }
    meta_path = results / f"{stem}.meta.json"
    with meta_path.open("x") as file:
        json.dump(meta, file, indent=2)
        file.write("\n")
    print(f"drain: wrote {stem}-<variant>.jsonl, .jsonl.log, -picker.log and {meta_path.name}")
    if not all(judged):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
