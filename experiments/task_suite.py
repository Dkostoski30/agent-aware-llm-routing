"""The practical task suite on the demo model (P4.7, docs/task-cards/71-P4.7-task-suite.md).

Sends each demo scenario's questions, once per seed, through the chat path: one message in a new
chat of one user, with the scenario's files attached, to the app backend in k3s, which starts the
agent run (P3.12). Each run's answer is read from the chat and its steps from the app's GET
/runs/{id}, then scored as the go/no-go scores (go_no_go.is_correct). Writes one row per run to
results/<suite>-<hash>-<agent image tag>.csv next to the suite file (D11) and prints the success
table: per scenario, how many of its runs were right and what step 1 picked (D13).

The runs go to the app pod's IP, as go_no_go's go to the agent's; the host is the k3s node. The
session cookie is Secure (app.cookie_secure), which an HTTP client keeps for HTTPS only, so the
token from the login's Set-Cookie is sent by hand, as make smoke does.

Usage: task_suite.py SUITE_YAML   (experiments/task-suite.yaml)
"""

import argparse
import csv
import hashlib
import io
import json
import secrets
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml
from go_no_go import (
    CONTAINS,
    REPO_ROOT,
    REQUEST_TIMEOUT_SECONDS,
    Question,
    expected_value,
    is_correct,
    kubectl,
    ready_pod_ip,
    served_differs,
)

# The app backend's pods and port (infra/manifests/app.yaml), and the cookie a login sets
# (app_backend/app.py, SESSION_COOKIE).
APP_PODS = "app.kubernetes.io/name=app"
APP_PORT = 8080
SESSION_COOKIE = "session"
# The agent's Deployment, whose image tag stamps the results (D11).
AGENT_DEPLOYMENT = "agent"
# How often a chat is read for the answer, and so the resolution of duration_s; as go_no_go's.
POLL_SECONDS = 1.0
# .invalid is a reserved domain (RFC 2606), so the address can never be someone's, as make smoke's.
EMAIL_DOMAIN = "task-suite.invalid"
# 16 random bytes are 32 hex characters, above app.min_password_length in every profile.
PASSWORD_BYTES = 16

COLUMNS = (
    "scenario",
    "question",
    "seed",
    "correct",
    "picked",
    "steps",
    "invalid_calls",
    "duration_s",
    "expected",
    "answer",
    "status",
    "error",
)


class RunTimedOut(Exception):
    """A run still running after the suite's run timeout."""


@dataclass(frozen=True)
class Scenario:
    # As step 1 picks it (D13).
    name: str
    # As run-request.v1 carries them: id, name, content, and type for a text file.
    files: list[dict[str, str]]
    # The rows of its CSV, for the questions answered with a number; empty without one.
    rows: list[dict[str, str]]
    questions: list[Question]


@dataclass(frozen=True)
class Suite:
    path: Path
    # First 12 hex digits of the suite file's sha256; stamps the result file (D11).
    hash: str
    profile: str
    seeds: list[int]
    # Right runs of a scenario's out of its questions x seeds that pass it.
    threshold: int
    scenarios: list[Scenario]


def _file(base: Path, spec: Mapping[str, str]) -> dict[str, str]:
    file = {"id": spec["id"], "name": spec["name"], "content": (base / spec["path"]).read_text()}
    if "type" in spec:
        file["type"] = spec["type"]
    return file


def load_suite(path: Path) -> Suite:
    spec = yaml.safe_load(path.read_text())
    scenarios = []
    for name, scenario in spec["scenarios"].items():
        files = [_file(path.parent, file) for file in scenario["files"]]
        # A file without a type is CSV (schemas/run-request.v1.json).
        csvs = [file["content"] for file in files if file.get("type", "csv") == "csv"]
        rows = list(csv.DictReader(io.StringIO(csvs[0]))) if csvs else []
        questions = [Question(**question) for question in scenario["questions"]]
        scenarios.append(Scenario(name, files, rows, questions))
    return Suite(
        path=path,
        hash=hashlib.sha256(path.read_bytes()).hexdigest()[:12],
        profile=spec["profile"],
        seeds=spec["seeds"],
        threshold=spec["threshold"],
        scenarios=scenarios,
    )


def expected_values(suite: Suite) -> dict[tuple[str, str], float | None]:
    """(scenario, question) -> the number the scorer looks for, from the scenario's CSV; None for a
    question whose answer must contain texts. A question that cannot be computed raises before any
    run."""
    return {
        (scenario.name, question.id): None
        if question.expected["kind"] == CONTAINS
        else expected_value(scenario.rows, question.expected)
        for scenario in suite.scenarios
        for question in scenario.questions
    }


class Asker(Protocol):
    def ask(
        self, task: str, files: list[dict[str, str]], seed: int, run_timeout: float
    ) -> tuple[dict[str, Any], float]: ...


class AppClient:
    """One user's chats on the app backend at the http client's base_url."""

    def __init__(self, http: httpx.Client, poll_seconds: float = POLL_SECONDS) -> None:
        self._http = http
        self._poll_seconds = poll_seconds
        self._cookie = ""

    def _send(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"cookie": self._cookie} if self._cookie else {}
        response = self._http.request(method, path, json=body, headers=headers)
        response.raise_for_status()
        return response.json()

    def sign_up(self, email: str, password: str) -> None:
        """Registers the user and logs in; later requests carry the session."""
        credentials = {"email": email, "password": password}
        self._send("POST", "/auth/register", credentials)
        response = self._http.post("/auth/login", json=credentials)
        response.raise_for_status()
        self._cookie = f"{SESSION_COOKIE}={response.cookies[SESSION_COOKIE]}"

    def ask(
        self, task: str, files: list[dict[str, str]], seed: int, run_timeout: float
    ) -> tuple[dict[str, Any], float]:
        """task as the one message of a new chat; the run's GET /runs/{id} body once the answer is
        no longer running, and the seconds from the message to then. Raises RunTimedOut after
        run_timeout."""
        started = time.monotonic()
        chat_id = self._send("POST", "/chats", {"title": task})["id"]
        message = {"task": task, "files": files, "seed": seed}
        sent = self._send("POST", f"/chats/{chat_id}/messages", message)
        while True:
            chat = self._send("GET", f"/chats/{chat_id}")
            [reply] = [m for m in chat["messages"] if m["id"] == sent["assistant_message_id"]]
            elapsed = time.monotonic() - started
            if reply["status"] != "running":
                run: dict[str, Any] = self._send("GET", f"/runs/{sent['run_id']}")
                return run, elapsed
            if elapsed > run_timeout:
                raise RunTimedOut(f"{sent['run_id']} still running after {run_timeout:.0f} s")
            time.sleep(self._poll_seconds)


def _expected_text(question: Question, value: float | None) -> str:
    return " | ".join(question.expected["texts"]) if value is None else str(value)


def run_suite(
    client: Asker,
    suite: Suite,
    expected: Mapping[tuple[str, str], float | None],
    tolerance: float,
    run_timeout: float,
) -> list[dict[str, Any]]:
    """Every question of every scenario once per seed, one after the other; one row per run,
    printed as it ends."""
    rows: list[dict[str, Any]] = []
    for scenario in suite.scenarios:
        for question in scenario.questions:
            value = expected[(scenario.name, question.id)]
            for seed in suite.seeds:
                run, duration = client.ask(question.text, scenario.files, seed, run_timeout)
                correct = is_correct(run, question, value, tolerance)
                steps = run["steps"]
                row = {
                    "scenario": scenario.name,
                    "question": question.id,
                    "seed": seed,
                    "correct": correct,
                    "picked": steps[0]["content"] if steps else None,
                    "steps": len(steps),
                    "invalid_calls": run["invalid_calls"],
                    "duration_s": round(duration, 1),
                    "expected": _expected_text(question, value),
                    "answer": run["answer"],
                    "status": run["status"],
                    "error": run["error"],
                }
                rows.append(row)
                verdict = "correct" if correct else "wrong"
                print(
                    f"task-suite: {scenario.name} {question.id} seed {seed}: {verdict} "
                    f"(picked {row['picked']}, {run['status']}, {row['steps']} steps, "
                    f"{row['duration_s']} s): {run['answer']!r}",
                    flush=True,
                )
    return rows


def table(rows: Sequence[Mapping[str, Any]], suite: Suite) -> list[str]:
    """Per scenario, right runs out of its runs and step 1's picks, most first; then the verdict."""
    lines = []
    below = []
    for scenario in suite.scenarios:
        own = [row for row in rows if row["scenario"] == scenario.name]
        right = sum(row["correct"] for row in own)
        picks = ", ".join(f"{p} {n}" for p, n in Counter(r["picked"] for r in own).most_common())
        lines.append(f"{scenario.name}: {right}/{len(own)} correct, picked {picks}")
        if right < suite.threshold:
            below.append(scenario.name)
    runs = len(suite.seeds) * len(suite.scenarios[0].questions)
    if below:
        lines.append(f"no-go: {', '.join(below)} below {suite.threshold}/{runs}")
    else:
        lines.append(f"go: every scenario {suite.threshold}/{runs} or more")
    return lines


def result_path(suite: Suite, agent_image: str) -> Path:
    """results/<suite>-<hash>-<agent image tag>.csv next to the suite file (D11), as go_no_go's."""
    tag = agent_image.rsplit(":", 1)[1]
    return suite.path.parent / "results" / f"{suite.path.stem}-{suite.hash}-{tag}.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description="The practical task suite (P4.7).")
    parser.add_argument("suite", type=Path, help="suite YAML, e.g. experiments/task-suite.yaml")
    suite = load_suite(parser.parse_args().suite)
    profile = yaml.safe_load((REPO_ROOT / "config" / f"{suite.profile}.yaml").read_text())
    tolerance: float = profile["agent"]["answer_tolerance"]
    # The agent fails a run after agent.max_steps calls of at most agent.llm_timeout_seconds
    # each, so a run still going after that is stuck (go_no_go's bound).
    run_timeout = profile["agent"]["max_steps"] * profile["agent"]["llm_timeout_seconds"]
    # Before the cluster is touched, so a broken question fails first.
    expected = expected_values(suite)

    running = yaml.safe_load(
        kubectl("get", "configmap", "agent-config", "-o", r"jsonpath={.data.profile\.yaml}")
    )
    differs = served_differs(running["model"], profile["model"])
    if differs is not None:
        sys.exit(f"task-suite: {differs}: run make up PROFILE={suite.profile} first")
    agent_image = kubectl(
        "get",
        "deployment",
        AGENT_DEPLOYMENT,
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    )
    pods = json.loads(kubectl("get", "pods", "--selector", APP_PODS, "-o", "json"))
    app_url = f"http://{ready_pod_ip(pods, 'app')}:{APP_PORT}"
    with httpx.Client(base_url=app_url, timeout=REQUEST_TIMEOUT_SECONDS) as http:
        client = AppClient(http)
        email = f"suite-{secrets.token_hex(6)}@{EMAIL_DOMAIN}"
        client.sign_up(email, secrets.token_hex(PASSWORD_BYTES))
        print(f"task-suite: {email} on {app_url}, agent {agent_image}", flush=True)
        rows = run_suite(client, suite, expected, tolerance, run_timeout)

    results = result_path(suite, agent_image)
    results.parent.mkdir(exist_ok=True)
    with results.open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"task-suite: {len(rows)} rows in {results}")
    for line in table(rows, suite):
        print(line)


if __name__ == "__main__":
    main()
