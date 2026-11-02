"""make record (P2.2b): an experiment's runs through the agent runtime in k3s, as trace.v1 lines.

Sends every question of an experiment file once per seed, one run at a time, on the agent as make up
deployed it. After each run it reads the run's LLM calls from Redis (P2.2a) and turns them into one
line per call. The trace is written once, after the last run, so a recording that stops leaves no
partial trace for the replayer to pick up, and it is never written over.

Runs the model failed stay in the trace, since a real agent makes that traffic too. A call that got
no reply has no record, so the trace would miss it: that stops the recording, and so does a line
trace.v1 refuses (D12). Decided with the user, docs/task-cards/25-P2.2b-record-traces.md.

Usage: python -m load_generator.record EXPERIMENT_YAML   (experiments/record-demo.yaml)
"""

import argparse
import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_SCHEMA = REPO_ROOT / "schemas" / "trace.v1.json"
SCHEMA_VERSION = 1

# The agent's Deployment and its pods (infra/manifests/agent.yaml). The host is the k3s node, so a
# pod IP is reachable from here. The pod's own IP, not the Service: after an agent restart this
# node's Service rules led to the old pod for more than 10 s (P1.6c), and make up may have just
# restarted it.
AGENT_DEPLOYMENT = "agent"
AGENT_PODS = "app.kubernetes.io/name=agent"
AGENT_PORT = 8080
# One call to the agent's API, which answers from Redis without waiting for the model.
REQUEST_TIMEOUT_SECONDS = 10
# How often a run's status is read; a call takes seconds (experiments/log.md), and a poll delays
# only the start of the next run, never a call inside this one.
POLL_SECONDS = 1.0
# How the agent's loop starts the error of a run whose LLM call got no reply (agent_runtime/loop.py,
# run_task). A test runs that branch of the loop, so its wording cannot drift from this; a new
# failure path that words its error differently would count as a model failure.
NO_REPLY = "LLM call failed:"


class RecordError(Exception):
    """The recording must stop: it would not write one valid line per LLM call of every run."""


@dataclass(frozen=True)
class Task:
    # The trace's task_type: the runtime has no scenario until P4.8 (D13), the experiment file says.
    task_type: str
    text: str
    seed: int
    # The files as run-request.v1 carries them.
    files: list[dict[str, str]]


@dataclass(frozen=True)
class Experiment:
    path: Path
    # Stamps the trace (D11).
    hash: str
    # The config/ profile the agent must be running.
    profile: str
    # Every question once per seed, in file order.
    tasks: list[Task]


@dataclass(frozen=True)
class RecordedRun:
    task: Task
    # The run's GET /runs/{id} body.
    run: dict[str, Any]
    lines: list[dict[str, Any]]


def experiment_hash(path: Path) -> str:
    """The first 12 hex digits of the file's sha256, as go_no_go.py stamps its results."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _request_file(directory: Path, file: Mapping[str, str]) -> dict[str, str]:
    """An experiment's file as run-request.v1 carries it: its path replaced by its content."""
    fields = {name: value for name, value in file.items() if name != "path"}
    return {**fields, "content": (directory / file["path"]).read_text()}


def load_experiment(path: Path) -> Experiment:
    spec = yaml.safe_load(path.read_text())
    tasks = []
    for task_type, group in spec["task_types"].items():
        files = [_request_file(path.parent, file) for file in group.get("files", [])]
        for text in group["questions"]:
            for seed in spec["seeds"]:
                tasks.append(Task(task_type=task_type, text=text, seed=seed, files=files))
    return Experiment(path=path, hash=experiment_hash(path), profile=spec["profile"], tasks=tasks)


def run_request(task: Task) -> dict[str, Any]:
    """The POST /runs body (schemas/run-request.v1.json)."""
    return {"task": task.text, "seed": task.seed, "files": task.files}


def adapter_id(model: Mapping[str, Any]) -> str | None:
    """The first 12 hex digits of the served LoRA adapter's sha256 (model.lora, P1.7); None for a
    profile that serves no adapter."""
    lora = model.get("lora")
    return None if lora is None else str(lora["sha256"])[:12]


def trace_path(experiment: Experiment, agent_image: str, adapter: str | None) -> Path:
    """results/traces/<experiment>-<hash>-<agent image tag>[-<adapter>].jsonl next to the experiment
    file. The tag is a hash of the agent's build context (infra/deploy-agent.sh), so a trace is tied
    to the agent and the adapter that made it as well as to the experiment file (D11): P4.8
    re-records with a changed agent and must not replace the trace earlier replays used."""
    tag = agent_image.rsplit(":", 1)[1]
    suffix = "" if adapter is None else f"-{adapter}"
    name = f"{experiment.path.stem}-{experiment.hash}-{tag}{suffix}.jsonl"
    return experiment.path.parent / "results" / "traces" / name


def trace_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(TRACE_SCHEMA.read_text()))


def trace_lines(
    run_id: str, task_type: str, calls: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """One line per call, in the calls' order. The calls hold trace.v1's own field names, and a
    measurement a reply lacked is already left out of them (agent_runtime.store.CallRecord)."""
    return [
        {"schema_version": SCHEMA_VERSION, "run_id": run_id, "task_type": task_type, **call}
        for call in calls
    ]


def check_line(validator: Draft202012Validator, line: Mapping[str, Any]) -> None:
    violation = best_match(validator.iter_errors(line))
    if violation is not None:
        raise RecordError(
            f"{line.get('run_id')} step {line.get('step')}: not a trace.v1 line: "
            f"{violation.message}"
        )


def check_calls(run_id: str, calls: Sequence[Mapping[str, Any]], steps_shown: int) -> None:
    """A run's calls must be steps 1..n, as many as GET /runs/{id} shows: one record per reply and
    one step per reply (P2.2a), saved in one transaction."""
    steps = [call["step"] for call in calls]
    if steps != list(range(1, steps_shown + 1)):
        raise RecordError(
            f"{run_id}: GET /runs/{{id}} shows {steps_shown} steps, Redis has calls for steps "
            f"{steps}"
        )


def kubectl(*args: str) -> str:
    """kubectl's output; its errors go to the terminal, so a failing command says why."""
    return subprocess.run(["kubectl", *args], stdout=subprocess.PIPE, text=True, check=True).stdout


def parse_calls(run_id: str, output: str) -> list[dict[str, Any]]:
    """The run's calls from redis-cli's output, which is an empty line for a missing key."""
    if not output.strip():
        raise RecordError(f"{run_id}: no calls in Redis")
    calls: list[dict[str, Any]] = json.loads(output)
    return calls


def redis_calls(run_id: str) -> list[dict[str, Any]]:
    """The calls key the agent saves with each step (agent_runtime.store.RedisStore)."""
    key = f"thesis:run:{run_id}:calls"
    return parse_calls(run_id, kubectl("exec", "deployment/redis", "--", "redis-cli", "GET", key))


def ready_pod_ip(pods: Mapping[str, Any]) -> str:
    """The IP of the one Ready agent pod that is not being deleted, from kubectl get pods -o json.
    The Deployment has one replica, so anything else means a rollout is not finished."""
    ready = [
        item["status"]["podIP"]
        for item in pods["items"]
        if "deletionTimestamp" not in item["metadata"]
        and any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in item["status"].get("conditions", [])
        )
    ]
    if len(ready) != 1:
        raise RecordError(f"expected one Ready agent pod, found {len(ready)}")
    ip: str = ready[0]
    return ip


def check_before_start(profile: str, running: str, wanted: str, trace: Path) -> None:
    """Stops before the first run if the agent runs another text than config/<profile>.yaml (running
    and wanted): another model, adapter or D10 mode would make other traffic under this trace's
    name. And if the trace exists: a re-run must not replace a recording."""
    if running != wanted:
        raise RecordError(
            f"the agent's ConfigMap differs from config/{profile}.yaml: run make up "
            f"PROFILE={profile} first"
        )
    if trace.exists():
        raise RecordError(f"{trace} exists: delete it to record again")


def _wait_for_end(
    client: httpx.Client, run_id: str, run_timeout: float, poll_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        response = client.get(f"/runs/{run_id}")
        response.raise_for_status()
        run: dict[str, Any] = response.json()
        if run["status"] != "running":
            return run
        if time.monotonic() - started > run_timeout:
            raise RecordError(f"{run_id} still running after {run_timeout:.0f} s")
        time.sleep(poll_seconds)


def record_run(
    client: httpx.Client,
    task: Task,
    read_calls: Callable[[str], list[dict[str, Any]]],
    validator: Draft202012Validator,
    run_timeout: float,
    poll_seconds: float = POLL_SECONDS,
) -> RecordedRun:
    """One run from POST /runs to its checked trace lines. The final save writes the status and
    the calls in one transaction (agent_runtime.store.RedisStore), so a run seen ended has all of
    its calls in Redis."""
    response = client.post("/runs", json=run_request(task))
    response.raise_for_status()
    run_id: str = response.json()["run_id"]
    run = _wait_for_end(client, run_id, run_timeout, poll_seconds)
    error = run["error"]
    if error is not None and error.startswith(NO_REPLY):
        raise RecordError(f"{run_id}: a call got no reply, so the trace would miss it: {error}")
    calls = read_calls(run_id)
    check_calls(run_id, calls, len(run["steps"]))
    lines = trace_lines(run_id, task.task_type, calls)
    for line in lines:
        check_line(validator, line)
    return RecordedRun(task=task, run=run, lines=lines)


def write_trace(path: Path, lines: Iterable[Mapping[str, Any]]) -> None:
    """Mode x, since check_before_start ran one recording ago: a trace that appeared since, from a
    second make record, raises FileExistsError instead of being replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as out:
        out.writelines(json.dumps(line) + "\n" for line in lines)


def summary(runs: Sequence[RecordedRun]) -> list[str]:
    """One line per task type, in the order they ran, then the totals: the numbers
    experiments/log.md keeps. The steps come from GET /runs/{id} and the lines from Redis, so the
    last line shows the done-when's two counts side by side."""
    by_type: dict[str, list[RecordedRun]] = {}
    for run in runs:
        by_type.setdefault(run.task.task_type, []).append(run)
    text = []
    for task_type, group in by_type.items():
        steps = [len(run.run["steps"]) for run in group]
        failed = sum(run.run["status"] == "failed" for run in group)
        lines = sum(len(run.lines) for run in group)
        text.append(
            f"{task_type}: {len(group)} runs, {failed} failed, {lines} lines, "
            f"{min(steps)}-{max(steps)} steps per run"
        )
    steps = [len(run.run["steps"]) for run in runs]
    longer = sum(step > 1 for step in steps)
    lines = sum(len(run.lines) for run in runs)
    text.append(
        f"all: {len(runs)} runs, {longer} with more than one step, {lines} lines, "
        f"{sum(steps)} steps in GET /runs/{{id}}"
    )
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.2b: record an experiment's runs as a trace.")
    parser.add_argument("experiment", type=Path, help="e.g. experiments/record-demo.yaml")
    experiment = load_experiment(parser.parse_args().experiment)
    wanted = (REPO_ROOT / "config" / f"{experiment.profile}.yaml").read_text()
    profile = yaml.safe_load(wanted)
    # The agent fails a run after agent.max_steps calls of at most agent.llm_timeout_seconds each,
    # so a run still going after that is stuck.
    run_timeout = profile["agent"]["max_steps"] * profile["agent"]["llm_timeout_seconds"]

    running = kubectl("get", "configmap", "agent-config", "-o", r"jsonpath={.data.profile\.yaml}")
    agent_image = kubectl(
        "get",
        "deployment",
        AGENT_DEPLOYMENT,
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    )
    trace = trace_path(experiment, agent_image, adapter_id(profile["model"]))
    check_before_start(experiment.profile, running, wanted, trace)

    validator = trace_validator()
    pods = json.loads(kubectl("get", "pods", "--selector", AGENT_PODS, "-o", "json"))
    runs: list[RecordedRun] = []
    with httpx.Client(
        base_url=f"http://{ready_pod_ip(pods)}:{AGENT_PORT}", timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        for index, task in enumerate(experiment.tasks, start=1):
            recorded = record_run(client, task, redis_calls, validator, run_timeout)
            runs.append(recorded)
            run = recorded.run
            print(
                f"record: {index}/{len(experiment.tasks)} {task.task_type} seed {task.seed}: "
                f"{run['run_id']} {run['status']}, {len(run['steps'])} steps, "
                f"{len(recorded.lines)} lines: {run['answer']!r}",
                flush=True,
            )

    write_trace(trace, [line for run in runs for line in run.lines])
    for text in summary(runs):
        print(f"record: {text}")
    print(f"record: wrote {trace}")


if __name__ == "__main__":
    main()
