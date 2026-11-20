"""Failover with live agent runs on real pods (P4.4, docs/task-cards/60-P4.4-failover.md).

Per case, in the file's order: the go/no-go's questions (each once per seed, with its CSV) start as
runs on the agent runtime at once; kill_after_seconds later the steps each run has saved are read,
then a pod is force-deleted with no grace (a plain delete would drain it, P4.3):
- vllm: the vLLM pod holding the most live pins in the picker's log (P3.4's choice), so the
  runtime's calls on it die and it sends them again (agent.llm_retries);
- agent: the agent runtime's own pod, so a new runtime starts and resumes the runs from Redis.
Each run is then followed to its end. A case passes when every run ended done, no step it saved
before the kill changed, and its calls are steps 1..n each once; it is void without a retry in the
agent's log (vllm) or a run resumed (agent). Answers are scored as go/no-go scores them and counted,
not judged (P4.7 judges them). The agent cannot run on the simulator, whose replies are no tool
calls, so the check runs on real pods.

The picker runs the file's rules for the check (affinity, so runs are pinned and the vllm case's
pod holds some) and the profile's afterwards, whatever happened. The agent sends no end of run with
the profile's plugin off (agent.end_of_run), so a run's pin outlives it; every run counted here is
still going when the pod is chosen.

Usage: failover_live.py EXPERIMENT   (make failover-live EXPERIMENT=experiments/failover-live.yaml)
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from go_no_go import Experiment as Questions
from go_no_go import expected_value, is_correct
from go_no_go import load_experiment as load_questions
from load_generator.experiment import VLLM_SELECTOR, pods_to_warm, set_routing
from load_generator.failover import Kill, kill_pod, kubectl, kubectl_failure
from load_generator.record import AGENT_DEPLOYMENT, AGENT_PODS, AGENT_PORT, REPO_ROOT

CASES = ("vllm", "agent")
# The profile the check runs on: the agent's runs need a real model (the module's docstring), and
# a kill needs a second vLLM pod to go on on, which the demo profile lacks.
PROFILE = "real"
# What the agent runtime logs for each signal (agent_runtime/loop.py).
RETRIED = "llm call retried"
RESUMED = "run resumed"
SIGNALS = {"vllm": RETRIED, "agent": RESUMED}
# One request to the agent's API, which answers from Redis without waiting for the model.
REQUEST_TIMEOUT_SECONDS = 10
# How often a run's status, and the new agent pod, are read; a call takes seconds.
POLL_SECONDS = 1.0
# The agent pod's start: its image is on the node, and it reads Redis before it is Ready. 300 s is
# a wide margin over the ~10 s it takes idle, for a node busy with two vLLM pods (P3.11).
AGENT_START_TIMEOUT_SECONDS = 300
# The vLLM replacement loads the model while the other pod serves: 900 s, as experiment.py's
# SCALE_TIMEOUT for a pod's load on a shared GPU (P0.7).
VLLM_REPLACEMENT_TIMEOUT = "900s"


class LiveError(Exception):
    """The check must not start, or its result would not show a failover."""


@dataclass(frozen=True)
class LiveExperiment:
    name: str
    hash: str
    profile: str
    routing: Mapping[str, bool]
    questions: Questions
    seeds: list[int]
    kill_after_seconds: float
    wait_for_calls_seconds: float
    cases: list[str]


def load_live(path: Path) -> LiveExperiment:
    """A missing key raises KeyError."""
    spec = yaml.safe_load(path.read_text())
    if spec["profile"] != PROFILE:
        raise LiveError(f"profile must be {PROFILE}: the agent needs a real model and two pods")
    routing = spec["routing"]
    for rule in ("plugin", "affinity"):
        if routing.get(rule) is not True:
            raise LiveError(
                f"routing.{rule} must be on: the vLLM pod killed is the one most pinned"
            )
    cases = spec["cases"]
    if not cases or len(set(cases)) != len(cases) or not set(cases) <= set(CASES):
        raise LiveError(f"cases must name some of {CASES}, each once, got {cases}")
    if spec["kill_after_seconds"] <= 0:
        raise LiveError("kill_after_seconds must be after the runs start")
    if spec["wait_for_calls_seconds"] < 0:
        raise LiveError("wait_for_calls_seconds must not be negative")
    if not spec["seeds"]:
        raise LiveError("seeds must name at least one seed")
    return LiveExperiment(
        name=path.stem,
        hash=hashlib.sha256(path.read_bytes()).hexdigest()[:12],
        profile=spec["profile"],
        routing=routing,
        questions=load_questions((path.parent / spec["go_no_go"]).resolve()),
        seeds=spec["seeds"],
        kill_after_seconds=spec["kill_after_seconds"],
        wait_for_calls_seconds=spec["wait_for_calls_seconds"],
        cases=cases,
    )


def check_profile(name: str, running: str, wanted: str) -> None:
    """running is the agent's ConfigMap's profile text, which make up writes, wanted
    config/<name>.yaml's."""
    if running != wanted:
        raise LiveError(
            f"the cluster does not run config/{name}.yaml: run make up PROFILE={name} first"
        )


def log_count(log: str, message: str, runs: set[str]) -> int:
    """The agent's JSON lines with this message for one of runs; other lines are skipped."""
    count = 0
    for text in log.splitlines():
        try:
            fields = json.loads(text)
        except ValueError:
            continue
        if isinstance(fields, dict) and fields.get("message") == message:
            count += fields.get("run-id") in runs
    return count


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    question: str
    seed: int
    status: str
    error: str | None
    answer: str | None
    correct: bool
    # The run's steps (GET /runs/{id}) read just before the kill, and at its end.
    steps_before: list[dict[str, Any]]
    steps_after: list[dict[str, Any]]
    # The x-step of each call record at its end (GET /runs/{id}/calls), in order.
    call_steps: list[int]


def outcome_of(
    run_id: str,
    question: str,
    seed: int,
    steps_before: list[dict[str, Any]],
    run: Mapping[str, Any],
    calls: Mapping[str, Any],
    correct: bool,
) -> RunOutcome:
    return RunOutcome(
        run_id=run_id,
        question=question,
        seed=seed,
        status=run["status"],
        error=run["error"],
        answer=run["answer"],
        correct=correct,
        steps_before=steps_before,
        steps_after=run["steps"],
        call_steps=[call["step"] for call in calls["calls"]],
    )


def run_problems(outcome: RunOutcome) -> list[str]:
    """Why the run did not go on from where the kill left it; none when it did."""
    problems = []
    if outcome.status != "done":
        problems.append(f"{outcome.run_id}: {outcome.status}, {outcome.error}")
    if outcome.steps_after[: len(outcome.steps_before)] != outcome.steps_before:
        problems.append(f"{outcome.run_id}: a step saved before the kill changed")
    if outcome.call_steps != list(range(1, len(outcome.call_steps) + 1)):
        problems.append(f"{outcome.run_id}: call steps {outcome.call_steps}, not 1..n each once")
    return problems


@dataclass(frozen=True)
class CaseVerdict:
    case: str
    # Retries (vllm) or resumed runs (agent) of the case's runs in the agent's log.
    signals: int
    outcomes: list[RunOutcome]

    @property
    def problems(self) -> list[str]:
        return [problem for outcome in self.outcomes for problem in run_problems(outcome)]

    @property
    def void(self) -> bool:
        return self.signals == 0

    @property
    def passed(self) -> bool:
        return not self.void and not self.problems


def case_lines(verdict: CaseVerdict) -> list[str]:
    outcomes = verdict.outcomes
    lines = [
        f"  {o.run_id} {o.question} seed {o.seed}: {o.status}, "
        f"{'correct' if o.correct else 'wrong'}, steps {len(o.steps_before)} saved at the kill, "
        f"{len(o.steps_after)} at the end: {o.answer!r}"
        for o in outcomes
    ]
    lines.append(f"  {SIGNALS[verdict.case]} lines: {verdict.signals}")
    lines += [f"    {problem}" for problem in verdict.problems]
    done = sum(o.status == "done" for o in outcomes)
    tally = f"{done} of {len(outcomes)} runs done, {sum(o.correct for o in outcomes)} correct"
    if verdict.passed:
        lines.append(f"{verdict.case}: passed, {tally}")
    elif verdict.void:
        missing = "retry" if verdict.case == "vllm" else "run resumed"
        lines.append(f"{verdict.case}: void, no {missing}")
    else:
        lines.append(f"{verdict.case}: failed, {tally}")
    return lines


def agent_pod(skip: str | None = None) -> tuple[str, str]:
    """The name and IP of the Ready agent pod, other than skip; waits for one up to
    AGENT_START_TIMEOUT_SECONDS. Its IP, not the Service: right after a restart the node's Service
    rules led to the deleted pod for more than 10 s (P1.6c)."""
    give_up = time.monotonic() + AGENT_START_TIMEOUT_SECONDS
    while True:
        pods = json.loads(kubectl("get", "pods", "-l", AGENT_PODS, "-o", "json"))
        ready = [
            (item["metadata"]["name"], item["status"]["podIP"])
            for item in pods["items"]
            if item["metadata"]["name"] != skip
            and "deletionTimestamp" not in item["metadata"]
            and any(
                condition["type"] == "Ready" and condition["status"] == "True"
                for condition in item["status"].get("conditions", [])
            )
        ]
        if len(ready) == 1:
            return ready[0]
        if time.monotonic() >= give_up:
            raise LiveError(f"no Ready agent pod after {AGENT_START_TIMEOUT_SECONDS} s: {ready}")
        time.sleep(POLL_SECONDS)


def client_of(ip: str) -> httpx.Client:
    return httpx.Client(base_url=f"http://{ip}:{AGENT_PORT}", timeout=REQUEST_TIMEOUT_SECONDS)


def start_runs(client: httpx.Client, experiment: LiveExperiment) -> list[tuple[str, str, int]]:
    """(run id, question id, seed) of every run, all started before the first ends."""
    started = []
    for question in experiment.questions.questions:
        for seed in experiment.seeds:
            body = {"task": question.text, "seed": seed, "files": [experiment.questions.file]}
            response = client.post("/runs", json=body)
            response.raise_for_status()
            started.append((response.json()["run_id"], question.id, seed))
    return started


def steps_now(client: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/runs/{run_id}")
    response.raise_for_status()
    steps: list[dict[str, Any]] = response.json()["steps"]
    return steps


def kill_vllm(experiment: LiveExperiment, profile: Mapping[str, Any], since: str) -> str:
    pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
    state = Kill(sorted(pods_to_warm(pods, profile["replicas"])), since)
    kill_pod(state, experiment.wait_for_calls_seconds)
    if state.error is not None:
        raise LiveError(state.error)
    if state.pod is None:
        raise LiveError(f"no vLLM pod held a live pin within {experiment.wait_for_calls_seconds} s")
    return state.pod


def wait_for_end(client: httpx.Client, run_id: str, timeout: float) -> dict[str, Any]:
    give_up = time.monotonic() + timeout
    while True:
        response = client.get(f"/runs/{run_id}")
        response.raise_for_status()
        run: dict[str, Any] = response.json()
        if run["status"] != "running":
            return run
        if time.monotonic() >= give_up:
            raise LiveError(f"{run_id} still running after {timeout:.0f} s")
        time.sleep(POLL_SECONDS)


def run_case(
    case: str,
    experiment: LiveExperiment,
    profile: Mapping[str, Any],
    expected: Mapping[str, float],
    run_timeout: float,
    log_path: Path,
) -> CaseVerdict:
    """One case's runs, its kill and the agent's log of it, kept at log_path."""
    since = datetime.now(UTC).isoformat()
    name, ip = agent_pod()
    with client_of(ip) as client:
        runs = start_runs(client, experiment)
        print(f"failover-live: {case}: started {[run for run, _, _ in runs]}", flush=True)
        time.sleep(experiment.kill_after_seconds)
        before = {run: steps_now(client, run) for run, _, _ in runs}
    if case == "vllm":
        killed = kill_vllm(experiment, profile, since)
    else:
        print(f"failover-live: agent: killing {name}", flush=True)
        kubectl("delete", "pod", name, "--grace-period=0", "--force", "--wait=false")
        killed = name
    print(f"failover-live: {case}: killed {killed}", flush=True)
    _, ip = agent_pod(skip=name if case == "agent" else None)
    tolerance: float = profile["agent"]["answer_tolerance"]
    questions = {question.id: question for question in experiment.questions.questions}
    outcomes = []
    with client_of(ip) as client:
        for run_id, question, seed in runs:
            run = wait_for_end(client, run_id, run_timeout)
            calls = client.get(f"/runs/{run_id}/calls")
            calls.raise_for_status()
            correct = is_correct(run, questions[question], expected[question], tolerance)
            outcomes.append(
                outcome_of(run_id, question, seed, before[run_id], run, calls.json(), correct)
            )
    log = kubectl("logs", f"deployment/{AGENT_DEPLOYMENT}", f"--since-time={since}")
    log_path.write_text(log)
    signals = log_count(log, SIGNALS[case], {run for run, _, _ in runs})
    return CaseVerdict(case, signals, outcomes)


def main() -> int:
    parser = argparse.ArgumentParser(description="P4.4: live agent runs through two kills")
    parser.add_argument("experiment", type=Path)
    experiment = load_live(parser.parse_args().experiment)
    profile_text = (REPO_ROOT / "config" / f"{experiment.profile}.yaml").read_text()
    profile = yaml.safe_load(profile_text)
    agent = profile["agent"]
    # Each of agent.max_steps calls may take every attempt to its deadline, with a backoff between.
    attempts = 1 + agent["llm_retries"]
    per_call = agent["llm_timeout_seconds"] + agent["llm_retry_backoff_seconds"]
    run_timeout = agent["max_steps"] * attempts * per_call
    # Before the cluster is touched, so a broken question fails first (as go_no_go.py).
    rows = experiment.questions.rows
    expected = {q.id: expected_value(rows, q.expected) for q in experiment.questions.questions}
    results = REPO_ROOT / "experiments" / "results"
    stem = f"{experiment.name}-{experiment.hash}"
    result_path = results / f"{stem}.json"
    found = sorted(results.glob(f"{stem}*"))
    if found:
        raise LiveError(f"results of this file exist, delete them to run it again: {found}")
    running = kubectl("get", "configmap", "agent-config", "-o", r"jsonpath={.data.profile\.yaml}")
    check_profile(experiment.profile, running, profile_text)
    verdicts = []
    try:
        set_routing(experiment.profile, None, experiment.routing)
        for case in experiment.cases:
            log_path = results / f"{stem}-{case}-agent.log"
            verdict = run_case(case, experiment, profile, expected, run_timeout, log_path)
            verdicts.append(verdict)
            for text in case_lines(verdict):
                print(text, flush=True)
            if case == "vllm":
                timeout = f"--timeout={VLLM_REPLACEMENT_TIMEOUT}"
                try:
                    kubectl("rollout", "status", "deployment/vllm", timeout)
                except subprocess.CalledProcessError as error:
                    raise LiveError(f"the vLLM replacement: {kubectl_failure(error)}") from error
    finally:
        set_routing(experiment.profile, None, None)
    with result_path.open("x") as file:
        json.dump(
            {
                "experiment": experiment.name,
                "hash": experiment.hash,
                "profile": experiment.profile,
                "profile_sha256": hashlib.sha256(profile_text.encode()).hexdigest(),
                "cases": [
                    {
                        "case": v.case,
                        "signals": v.signals,
                        "passed": v.passed,
                        "void": v.void,
                        "problems": v.problems,
                        "runs": [asdict(o) for o in v.outcomes],
                    }
                    for v in verdicts
                ],
            },
            file,
            indent=2,
        )
        file.write("\n")
    print(f"failover-live: wrote {result_path.name} and {stem}-<case>-agent.log", flush=True)
    return 0 if all(v.passed for v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
