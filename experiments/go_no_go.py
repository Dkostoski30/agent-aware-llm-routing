"""Practical go/no-go on the demo model (P1.6b).

Sends every question of an experiment file once per seed through the agent runtime in k3s, with the
experiment's CSV attached, in each D10 mode. Scores every run, writes one row per run to
results/<experiment>-<hash>.csv next to the experiment file (D11) and prints the score per mode and
the decision.

The mode is switched the way the cluster is configured, not by a code path: the profile with
agent.tool_choice set goes into the ConfigMap agent-config, and the thesis/config-hash annotation
restarts the agent on it (infra/deploy-agent.sh). The profile from config/ is put back at the end,
also after a failure or Ctrl+C.

Usage: go_no_go.py EXPERIMENT_YAML
"""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# The agent's Service (infra/manifests/agent.yaml). The host is the k3s node, so its ClusterIP is
# reachable from here (as in infra/checks/agent_check.sh).
AGENT_SERVICE = "agent"
AGENT_PORT = 8080
AGENT_PODS = "app.kubernetes.io/name=agent"
# The agent image is already in k3s, so a restarted pod is Ready in seconds; 120 s also covers a
# node that is busy loading a model. Also the wait for the old pod to go, which uvicorn's shutdown
# keeps within the 30 s default grace period.
ROLLOUT_TIMEOUT = "120s"
# One call to the agent's API, which answers from Redis without waiting for the model.
REQUEST_TIMEOUT_SECONDS = 10
# How often a run's status is read, and so the resolution of duration_s.
POLL_SECONDS = 1.0

# A number as a model writes it: digits with optional thousands commas and decimals. Unsigned:
# every expected value of the experiment is positive (test_go_no_go), and a minus sign is ambiguous
# next to a hyphen in text.
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

AGGREGATES: dict[str, Callable[[list[float]], float]] = {"sum": math.fsum, "mean": statistics.fmean}

# The D10 mode that wins a tie: it cannot produce a malformed call.
REQUIRED = "required"
AUTO = "auto"
# The tool that ends a run in required mode; auto mode does not offer it (agent_runtime.tools, D10).
FINAL_ANSWER = "final_answer"

# The card's columns first, then what shows why a run was scored wrong.
COLUMNS = (
    "mode",
    "question",
    "seed",
    "correct",
    "steps",
    "invalid_calls",
    "duration_s",
    "expected",
    "answer",
    "status",
    "error",
)


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    # Each {tool, arguments?}: a call to tool that returned a result, with these arguments among its
    # own; a correct run made every one of them.
    expected_calls: list[dict[str, Any]]
    # How expected_value computes the answer from the CSV.
    expected: dict[str, Any]
    # An answer that matches states the opposite of the fact around its number.
    wrong_if_matches: str | None = None


@dataclass(frozen=True)
class Experiment:
    path: Path
    # First 12 hex digits of the experiment file's sha256; stamps the result file (D11).
    hash: str
    profile: str
    # The file as run-request.v1 carries it: id, name, content.
    file: dict[str, str]
    rows: list[dict[str, str]]
    seeds: list[int]
    modes: list[str]
    go_threshold: int
    questions: list[Question]


def load_experiment(path: Path) -> Experiment:
    spec = yaml.safe_load(path.read_text())
    content = (path.parent / spec["file"]["path"]).read_text()
    return Experiment(
        path=path,
        hash=hashlib.sha256(path.read_bytes()).hexdigest()[:12],
        profile=spec["profile"],
        file={"id": spec["file"]["id"], "name": spec["file"]["name"], "content": content},
        rows=list(csv.DictReader(io.StringIO(content))),
        seeds=spec["seeds"],
        modes=spec["modes"],
        go_threshold=spec["go_threshold"],
        questions=[Question(**question) for question in spec["questions"]],
    )


def numbers_in(text: str) -> list[float]:
    return [float(match.replace(",", "")) for match in NUMBER.findall(text)]


def _aggregate(rows: Sequence[Mapping[str, str]], agg: str, value: str) -> float:
    return AGGREGATES[agg]([float(row[value]) for row in rows])


def _groups(rows: Sequence[Mapping[str, str]], by: str) -> dict[str, list[Mapping[str, str]]]:
    groups: dict[str, list[Mapping[str, str]]] = {}
    for row in rows:
        groups.setdefault(row[by], []).append(row)
    return groups


def expected_value(rows: Sequence[Mapping[str, str]], spec: Mapping[str, Any]) -> float:
    """The answer to a question, computed from the CSV's rows with math.fsum and the statistics
    module, independently of the agent's tools, so a tool bug cannot hide by agreeing with itself.

    aggregate: agg of value over all rows, or over the rows of one group.
    difference: agg of value for group right minus group left.
    of_top_group: agg of value for the group that ranks first by rank's agg of rank's value.
    """
    kind = spec["kind"]
    if kind == "aggregate":
        group = spec.get("group")
        selected = rows if group is None else _groups(rows, group["by"])[group["equals"]]
        return _aggregate(selected, spec["agg"], spec["value"])
    if kind == "difference":
        groups = _groups(rows, spec["by"])
        right = _aggregate(groups[spec["right"]], spec["agg"], spec["value"])
        left = _aggregate(groups[spec["left"]], spec["agg"], spec["value"])
        return right - left
    if kind == "of_top_group":
        groups = _groups(rows, spec["by"])
        rank = spec["rank"]
        scores = {
            name: _aggregate(group, rank["agg"], rank["value"]) for name, group in groups.items()
        }
        ordered = sorted(scores, key=lambda name: scores[name], reverse=True)
        if len(ordered) > 1 and scores[ordered[0]] == scores[ordered[1]]:
            raise ValueError(
                f"a tie for the top group of {spec['by']}: the question has two answers"
            )
        return _aggregate(groups[ordered[0]], spec["agg"], spec["value"])
    raise ValueError(f"unknown expected kind {kind!r}")


def _calls_ran(run: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Name and arguments of every call that returned a result. A call the runtime rejected (D10) or
    that hit a tool error carries an error; a final_answer call has no result."""
    return [
        (call["name"], call["arguments"])
        for step in run["steps"]
        for call in step["tool_calls"]
        if call["result"] is not None and "error" not in call["result"]
    ]


def _made(calls: Sequence[tuple[str, dict[str, Any]]], expected_call: Mapping[str, Any]) -> bool:
    wanted = expected_call.get("arguments", {})
    return any(
        name == expected_call["tool"] and all(arguments.get(k) == v for k, v in wanted.items())
        for name, arguments in calls
    )


def is_correct(
    run: Mapping[str, Any], question: Question, expected: float, tolerance: float
) -> bool:
    """run is a GET /runs/{id} body. Correct when the run is done, made every expected call, does
    not match wrong_if_matches, and a number in its answer is within tolerance (a share of
    expected) of expected."""
    if run["status"] != "done":
        return False
    calls = _calls_ran(run)
    if not all(_made(calls, expected_call) for expected_call in question.expected_calls):
        return False
    answer: str = run["answer"]
    if question.wrong_if_matches and re.search(question.wrong_if_matches, answer, re.IGNORECASE):
        return False
    return any(abs(n - expected) <= tolerance * abs(expected) for n in numbers_in(answer))


def served_mode(run: Mapping[str, Any]) -> str | None:
    """The D10 mode of the agent that answered a run: a done run ended with final_answer in
    required mode and with a text answer in auto mode. None for a run that did not end done."""
    if run["status"] != "done":
        return None
    calls = (call["name"] for step in run["steps"] for call in step["tool_calls"])
    return REQUIRED if FINAL_ANSWER in calls else AUTO


def decide(correct: Mapping[str, int], threshold: int) -> str | None:
    """The mode the demo keeps: the one with the most correct runs among those reaching threshold,
    required on a tie (D10). None is a no-go."""
    passing = [mode for mode, count in correct.items() if count >= threshold]
    if not passing:
        return None
    return max(passing, key=lambda mode: (correct[mode], mode == REQUIRED))


def kubectl(*args: str, stdin: str | None = None) -> str:
    """kubectl's output; its errors go to the terminal, so a failing command says why."""
    return subprocess.run(
        ["kubectl", *args], input=stdin, stdout=subprocess.PIPE, text=True, check=True
    ).stdout


def apply_profile(text: str) -> None:
    """Puts a profile into the ConfigMap agent-config and restarts the agent on it, as
    infra/deploy-agent.sh does. The annotation is the first 12 hex digits of the profile's sha256,
    so putting back the file from config/ leaves the pod template deploy-agent.sh made.

    Returns once the old pod is deleted, not only when the new one is Ready: until then a pooled
    connection or a Service route not yet updated still reaches the old pod and its profile.
    """
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "agent-config"},
        "data": {"profile.yaml": text},
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(configmap))
    config_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
    patch = {
        "spec": {"template": {"metadata": {"annotations": {"thesis/config-hash": config_hash}}}}
    }
    deployment = json.loads(kubectl("get", "deployment", AGENT_SERVICE, "-o", "json"))
    if (
        deployment["spec"]["template"]["metadata"]["annotations"]["thesis/config-hash"]
        == config_hash
    ):
        # Already running this profile: the patch would change nothing and replace no pod.
        return
    old_pods = kubectl("get", "pods", "--selector", AGENT_PODS, "--output", "name").split()
    kubectl("patch", "deployment", AGENT_SERVICE, "--patch", json.dumps(patch))
    kubectl("rollout", "status", f"deployment/{AGENT_SERVICE}", f"--timeout={ROLLOUT_TIMEOUT}")
    if old_pods:
        kubectl("wait", "--for=delete", *old_pods, f"--timeout={ROLLOUT_TIMEOUT}")


def with_mode(profile_text: str, mode: str) -> str:
    profile = yaml.safe_load(profile_text)
    profile["agent"]["tool_choice"] = mode
    return yaml.safe_dump(profile, sort_keys=False)


def run_question(
    client: httpx.Client, file: Mapping[str, str], task: str, seed: int, run_timeout: float
) -> tuple[dict[str, Any], float]:
    """Starts a run and polls it until it ends; returns its GET /runs/{id} body and the seconds from
    the POST until the end was seen. A run still going after run_timeout comes back as status
    timeout."""
    started = time.monotonic()
    response = client.post("/runs", json={"task": task, "seed": seed, "files": [file]})
    response.raise_for_status()
    run_id = response.json()["run_id"]
    while True:
        response = client.get(f"/runs/{run_id}")
        response.raise_for_status()
        run: dict[str, Any] = response.json()
        elapsed = time.monotonic() - started
        if run["status"] != "running":
            return run, elapsed
        if elapsed > run_timeout:
            return {
                **run,
                "status": "timeout",
                "error": f"running after {run_timeout:.0f} s",
            }, elapsed
        time.sleep(POLL_SECONDS)


def run_mode(
    client: httpx.Client,
    experiment: Experiment,
    mode: str,
    expected: Mapping[str, float],
    tolerance: float,
    run_timeout: float,
) -> list[dict[str, Any]]:
    """Every question once per seed, one after the other, on an agent already running in mode;
    one result row per run, printed as it ends."""
    rows: list[dict[str, Any]] = []
    for question in experiment.questions:
        for seed in experiment.seeds:
            run, duration = run_question(client, experiment.file, question.text, seed, run_timeout)
            served = served_mode(run)
            if served not in (None, mode):
                raise RuntimeError(
                    f"{run['run_id']} was answered in {served} mode, not {mode}: an agent pod "
                    "with the other profile served it"
                )
            correct = is_correct(run, question, expected[question.id], tolerance)
            row = {
                "mode": mode,
                "question": question.id,
                "seed": seed,
                "correct": correct,
                "steps": len(run["steps"]),
                "invalid_calls": run["invalid_calls"],
                "duration_s": round(duration, 1),
                "expected": expected[question.id],
                "answer": run["answer"],
                "status": run["status"],
                "error": run["error"],
            }
            rows.append(row)
            verdict = "correct" if correct else "wrong"
            print(
                f"go-no-go: {mode} {question.id} seed {seed}: {verdict} ({run['status']}, "
                f"{row['steps']} steps, {row['invalid_calls']} invalid, {row['duration_s']} s): "
                f"{run['answer']!r}",
                flush=True,
            )
    return rows


def report(experiment: Experiment, rows: Sequence[Mapping[str, Any]]) -> None:
    """Writes the result CSV and prints the score per mode and the decision."""
    results = experiment.path.parent / "results" / f"{experiment.path.stem}-{experiment.hash}.csv"
    results.parent.mkdir(exist_ok=True)
    with results.open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"go-no-go: {len(rows)} rows in {results}")

    correct_by_mode: dict[str, int] = {}
    for mode in experiment.modes:
        mode_rows = [row for row in rows if row["mode"] == mode]
        correct_by_mode[mode] = sum(row["correct"] for row in mode_rows)
        invalid = sum(row["invalid_calls"] for row in mode_rows)
        print(f"{mode}: {correct_by_mode[mode]}/{len(mode_rows)} correct, {invalid} invalid calls")
    threshold = f"{experiment.go_threshold}/{len(experiment.questions) * len(experiment.seeds)}"
    decision = decide(correct_by_mode, experiment.go_threshold)
    if decision is None:
        print(
            f"no-go: no mode reaches {threshold}; narrow the tasks or ask about a FINKI GPU "
            "server at Checkpoint 1 (PLAN.md P1.6)"
        )
    else:
        print(
            f"go: {decision} reaches {threshold}; "
            f"config/{experiment.profile}.yaml keeps agent.tool_choice {decision}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Practical go/no-go on the demo model (P1.6b).")
    parser.add_argument(
        "experiment", type=Path, help="experiment YAML, e.g. experiments/demo-go-no-go.yaml"
    )
    experiment = load_experiment(parser.parse_args().experiment)
    profile_path = REPO_ROOT / "config" / f"{experiment.profile}.yaml"
    profile_text = profile_path.read_text()
    profile = yaml.safe_load(profile_text)
    tolerance: float = profile["agent"]["answer_tolerance"]
    # The agent fails a run after agent.max_steps calls of at most agent.llm_timeout_seconds
    # each, so a run still going after that is stuck.
    run_timeout = profile["agent"]["max_steps"] * profile["agent"]["llm_timeout_seconds"]
    # Before the cluster is touched, so a broken question fails first.
    expected = {q.id: expected_value(experiment.rows, q.expected) for q in experiment.questions}

    running = yaml.safe_load(
        kubectl("get", "configmap", "agent-config", "-o", r"jsonpath={.data.profile\.yaml}")
    )
    if running["model"]["name"] != profile["model"]["name"]:
        sys.exit(
            f"go-no-go: the agent runs {running['model']['name']}, profile {experiment.profile} is "
            f"{profile['model']['name']}: run make up PROFILE={experiment.profile} first"
        )
    agent_ip = kubectl("get", "service", AGENT_SERVICE, "-o", "jsonpath={.spec.clusterIP}")

    rows: list[dict[str, Any]] = []
    try:
        for mode in experiment.modes:
            print(f"go-no-go: agent.tool_choice {mode}, restarting the agent", flush=True)
            apply_profile(with_mode(profile_text, mode))
            # A new client per mode, so no pooled connection leads to the pod of the last mode.
            with httpx.Client(
                base_url=f"http://{agent_ip}:{AGENT_PORT}", timeout=REQUEST_TIMEOUT_SECONDS
            ) as client:
                rows += run_mode(client, experiment, mode, expected, tolerance, run_timeout)
    finally:
        print(f"go-no-go: putting back {profile_path}", flush=True)
        apply_profile(profile_text)
    report(experiment, rows)


if __name__ == "__main__":
    main()
