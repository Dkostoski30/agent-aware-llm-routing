"""The demo rehearsal (P5.4, docs/task-cards/78-P5.4-demo-rehearsal.md).

A headless Chromium does what the presenter does in the UI that make ui serves, and checks what the
audience would see, for one part at a time on the cluster make up left:
- practical (demo profile): a new user registers, then each chat of the file starts from New chat,
  its first message with its scenario's files attached; each answer is read off the page and the
  run's steps from the app backend through the page's own session, then scored as the task suite
  scores them (go_no_go.is_correct), step 1's pick included (D13);
- infrastructure (real profile): with every vLLM pod Ready, the cluster panel's Start runs the
  file's pattern, Kill deletes the pod the panel offers first kill_after_seconds later (P4.6a, as
  built: a force delete), and the round waits for the replay's end and the killed pod's
  replacement Ready.
A round fails at the first thing that goes wrong, a raw error or failure caption on the page or a
working horse (the UI's spinner) still trotting settle_seconds after its step ended included (the
user, 2026-11-27). The rounds run back to back, each in a new browser session with a new user;
the part is green when every round passed. Each round's folder has round.json, a screenshot per
step and the session's video; the part's summary.json has the verdict and the source (P5.0).

Usage: demo_rehearsal.py REHEARSAL_YAML --part practical|infrastructure [--headed]
"""

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx
import yaml
from go_no_go import (
    CONTAINS,
    FIXED_REPLY,
    REPO_ROOT,
    REQUEST_TIMEOUT_SECONDS,
    Question,
    expected_value,
    is_correct,
    kubectl,
    served_differs,
)
from load_generator.experiment import ExperimentError, check_gpu_blockers, source_state
from playwright.sync_api import ConsoleMessage, Request, Response, ViewportSize, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage
from task_suite import SESSION_COOKIE, load_suite

PRACTICAL = "practical"
INFRASTRUCTURE = "infrastructure"
PARTS = (PRACTICAL, INFRASTRUCTURE)
# How often a round reads the page and the cluster while it waits, and so the resolution of its
# timings; the task suite's.
POLL_SECONDS = 1.0
# .invalid is a reserved domain (RFC 2606), so the address can never be someone's, as make smoke's.
EMAIL_DOMAIN = "demo-rehearsal.invalid"
# 16 random bytes are 32 hex characters, above app.min_password_length in every profile.
PASSWORD_BYTES = 16
# A laptop screen: wide enough for the chat list, the chat and the cluster panel side by side
# (D14), as the presenter shows them.
VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
# The UI's spinner is the horse trotting (ui/src/app/ui/horse.ts), its answer a paragraph in the
# answer section (ui/answer.ts), its failure notices ag-notice--fail (bundle.css).
WORKING_HORSE = 'svg.ag-horse[data-state="working"]'
ANSWER = ".ag-answer p"
ALERT = '[role="alert"]'
FAIL_NOTICE = ".ag-notice--fail"
# The vLLM Deployment's pods (infra/charts/vllm), the ones Kill may delete.
VLLM_PODS = "app.kubernetes.io/name=vllm"
# One line per vLLM pod: its name, its Ready condition's status and its deletion time, empty unless
# it is terminating, as make demo-runner reads them (P4.6c).
POD_FIELDS = (
    r'jsonpath={range .items[*]}{.metadata.name}{"\t"}'
    r'{.status.conditions[?(@.type=="Ready")].status}{"\t"}'
    r'{.metadata.deletionTimestamp}{"\n"}{end}'
)
CHAT_URL = re.compile(r"/chats/(\d+)$")
TIME_FORMAT = "%Y%m%dT%H%M%S"

T = TypeVar("T")


class RehearsalError(Exception):
    """The rehearsal file or the cluster is not fit to rehearse on; nothing has run."""


class RoundFailed(Exception):
    """What went wrong in a round, in the words its record keeps."""


@dataclass(frozen=True)
class Turn:
    question: Question
    # The number the scorer looks for, from the chat's CSV; None for texts and a fixed reply.
    expected: float | None


@dataclass(frozen=True)
class Chat:
    # Attached with the chat's first message, from disk, as a person picks them.
    files: list[Path]
    # What step 1 must pick on every turn (D13).
    pick: str
    turns: list[Turn]


@dataclass(frozen=True)
class Practical:
    profile: str
    chats: list[Chat]
    tolerance: float
    # The agent fails a run after agent.max_steps calls of at most agent.llm_timeout_seconds each,
    # so an answer not shown by then is stuck (go_no_go's bound).
    run_timeout: float


@dataclass(frozen=True)
class Infrastructure:
    profile: str
    pattern: str
    # The pattern's runs in the profile, which the replay must all finish.
    runs: int
    # The profile's vLLM pods, Ready before a round and again after the kill.
    replicas: int
    kill_after_seconds: float
    replay_timeout_seconds: float
    ready_timeout_seconds: float


@dataclass(frozen=True)
class Rehearsal:
    path: Path
    # First 12 hex digits of the file's sha256; stamps the results (D11).
    hash: str
    ui_url: str
    rounds: int
    settle_seconds: float
    raw_errors: list[re.Pattern[str]]
    practical: Practical
    infrastructure: Infrastructure


def _profile(config_dir: Path, name: str) -> dict[str, Any]:
    profile: dict[str, Any] = yaml.safe_load((config_dir / f"{name}.yaml").read_text())
    return profile


def _patterns(spec: Sequence[str]) -> list[re.Pattern[str]]:
    patterns = []
    for text in spec:
        try:
            patterns.append(re.compile(text))
        except re.error as error:
            raise RehearsalError(f"raw_errors: bad pattern {text!r}: {error}") from error
    return patterns


def _practical(spec: Mapping[str, Any], base: Path, config_dir: Path) -> Practical:
    profile = _profile(config_dir, spec["profile"])
    suite_path = base / spec["suite"]
    suite = load_suite(suite_path)
    scenarios = {scenario.name: scenario for scenario in suite.scenarios}
    suite_files = yaml.safe_load(suite_path.read_text())["scenarios"]
    chats = []
    for chat in spec["chats"]:
        if not chat["turns"]:
            raise RehearsalError("a chat needs a turn")
        scenario = None
        if "files_of" in chat:
            if chat["files_of"] not in scenarios:
                raise RehearsalError(f"no scenario {chat['files_of']} in {suite_path}")
            scenario = scenarios[chat["files_of"]]
        turns = []
        for turn in chat["turns"]:
            if ("suite" in turn) == ("question" in turn):
                raise RehearsalError("a turn is one of suite or question")
            if "suite" in turn:
                if scenario is None:
                    raise RehearsalError(
                        f"{turn['suite']}: a suite question needs its chat's files_of"
                    )
                found = [q for q in scenario.questions if q.id == turn["suite"]]
                if not found:
                    raise RehearsalError(f"{scenario.name} has no question {turn['suite']}")
                question = found[0]
            else:
                question = Question(**turn["question"])
            kind = question.expected["kind"]
            if kind == FIXED_REPLY:
                reply = profile["agent"].get("out_of_scope_reply")
                if reply is None:
                    raise RehearsalError(
                        f"{question.id}: {spec['profile']} has no agent.out_of_scope_reply"
                    )
                question = Question(
                    question.id,
                    question.text,
                    question.expected_calls,
                    {**question.expected, "reply": reply},
                )
            rows = scenario.rows if scenario is not None else []
            value = (
                None if kind in (CONTAINS, FIXED_REPLY) else expected_value(rows, question.expected)
            )
            turns.append(Turn(question, value))
        files = (
            [(suite_path.parent / f["path"]).resolve() for f in suite_files[scenario.name]["files"]]
            if scenario is not None
            else []
        )
        chats.append(Chat(files, chat["pick"], turns))
    agent = profile["agent"]
    return Practical(
        profile=spec["profile"],
        chats=chats,
        tolerance=agent["answer_tolerance"],
        run_timeout=agent["max_steps"] * agent["llm_timeout_seconds"],
    )


def _infrastructure(spec: Mapping[str, Any], config_dir: Path) -> Infrastructure:
    name = spec["profile"]
    profile = _profile(config_dir, name)
    controls = profile["app"]["demo_controls"]
    if not controls["enabled"]:
        raise RehearsalError(f"{name} has no demo buttons (app.demo_controls.enabled)")
    if spec["pattern"] not in controls["patterns"]:
        raise RehearsalError(f"{spec['pattern']} is not in {name}'s demo patterns")
    return Infrastructure(
        profile=name,
        pattern=spec["pattern"],
        runs=profile["replay"]["patterns"][spec["pattern"]]["runs"],
        replicas=profile["replicas"],
        kill_after_seconds=spec["kill_after_seconds"],
        replay_timeout_seconds=spec["replay_timeout_seconds"],
        ready_timeout_seconds=spec["ready_timeout_seconds"],
    )


def load_rehearsal(path: Path, config_dir: Path = REPO_ROOT / "config") -> Rehearsal:
    """The rehearsal file with its questions, files and numbers resolved; a broken one raises
    RehearsalError before anything runs."""
    spec = yaml.safe_load(path.read_text())
    if spec["rounds"] < 1:
        raise RehearsalError("rounds must be 1 or more")
    if spec["settle_seconds"] <= 0:
        raise RehearsalError("settle_seconds must be over 0")
    return Rehearsal(
        path=path,
        hash=hashlib.sha256(path.read_bytes()).hexdigest()[:12],
        ui_url=spec["ui_url"],
        rounds=spec["rounds"],
        settle_seconds=spec["settle_seconds"],
        raw_errors=_patterns(spec["raw_errors"]),
        practical=_practical(spec["practical"], path.parent, config_dir),
        infrastructure=_infrastructure(spec["infrastructure"], config_dir),
    )


def page_faults(
    lines: Sequence[str], alerts: int, fail_notices: int, patterns: Sequence[re.Pattern[str]]
) -> list[str]:
    """What fails a round on the page: each visible line a raw-error pattern matches, once, and the
    alerts and failure notices shown."""
    faults: list[str] = []
    for line in lines:
        for pattern in patterns:
            if pattern.search(line):
                fault = f"raw error {pattern.pattern!r}: {line.strip()}"
                if fault not in faults:
                    faults.append(fault)
                break
    if alerts:
        faults.append(f"{alerts} alert{'s' if alerts != 1 else ''} shown")
    if fail_notices:
        faults.append(f"{fail_notices} failure notice{'s' if fail_notices != 1 else ''} shown")
    return faults


def _spaced(text: str) -> str:
    return " ".join(text.split())


def _expected_text(turn: Turn) -> str:
    expected = turn.question.expected
    if turn.expected is not None:
        return str(turn.expected)
    if expected["kind"] == FIXED_REPLY:
        return repr(expected["reply"])
    return " | ".join(expected["texts"])


def judge_turn(
    turn: Turn, pick: str, run: Mapping[str, Any], shown: str, tolerance: float
) -> list[str]:
    """Why a turn fails: step 1's pick, the run's end, its answer as the suite scores it, and the
    page showing that answer."""
    name = turn.question.id
    failures = []
    steps = run["steps"]
    picked = steps[0]["content"] if steps else None
    if picked != pick:
        failures.append(f"{name}: step 1 picked {picked}, not {pick}")
    if run["status"] != "done":
        failures.append(f"{name}: the run ended {run['status']}")
    elif not is_correct(run, turn.question, turn.expected, tolerance):
        failures.append(
            f"{name}: wrong answer (expected {_expected_text(turn)}): {run['answer']!r}"
        )
    if _spaced(shown) != _spaced(run["answer"]):
        failures.append(f"{name}: the page shows {shown!r}, not the run's answer {run['answer']!r}")
    return failures


def judge_replay(demo: Mapping[str, Any], infra: Infrastructure, killed: str) -> list[str]:
    """Why a replay fails, from GET /demo once it ended (demo.v1): it must be done, the pattern's
    runs all finished, no call lost, and the kill of the pod pressed taken."""
    runner = demo["runner"]
    replay = runner["replay"]
    failures = []
    if replay["state"] != "done":
        failures.append(f"the replay ended {replay['state']} (exit {replay['exit_code']})")
    if replay["runs_total"] != infra.runs:
        failures.append(
            f"the replay had {replay['runs_total']} runs, {infra.pattern} has {infra.runs}"
        )
    if replay["runs_finished"] != replay["runs_total"]:
        failures.append(f"{replay['runs_finished']} of {replay['runs_total']} runs finished")
    lost = replay["calls_failed"]
    if lost:
        failures.append(f"{lost} call{'s' if lost != 1 else ''} lost")
    last = runner["last_kill"]
    if last is None or last["pod"] != killed:
        failures.append(f"no kill of {killed} taken")
    return failures


def replaced(ready: Sequence[str], killed: str, replicas: int) -> bool:
    """The killed pod is gone and the profile's pods are Ready again."""
    return killed not in ready and len(ready) >= replicas


@dataclass
class Round:
    part: str
    number: int
    failures: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def record(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "round": self.number,
            "passed": self.passed,
            "failures": self.failures,
            "steps": self.steps,
        }


def part_green(rounds: Sequence[Round], expected: int) -> bool:
    """Green when every round of the file ran and passed: a failed one ends the part early."""
    return len(rounds) == expected and all(r.passed for r in rounds)


def results_dir(rehearsal: Rehearsal, part: str, now: datetime) -> Path:
    """results/<file>-<hash>-<part>-<start> next to the file (D11)."""
    stamp = now.strftime(TIME_FORMAT)
    return (
        rehearsal.path.parent / "results" / f"{rehearsal.path.stem}-{rehearsal.hash}-{part}-{stamp}"
    )


class Watched(Protocol):
    """What a wait reads on the page at every poll."""

    def faults(self) -> list[str]: ...

    def working(self) -> int: ...


class Page(Watched, Protocol):
    """What the presenter does and the audience sees, in either part; each action raises
    RoundFailed when the UI does not let it happen."""

    def register(self, email: str, password: str) -> None: ...

    # GET of the app backend's API path through the page's session.
    def fetch(self, path: str) -> Any: ...

    def screenshot(self, name: str) -> None: ...


class ChatPage(Page, Protocol):
    """The chat, for the practical part."""

    def new_chat(self) -> None: ...

    def send(self, text: str, files: Sequence[Path]) -> None: ...

    # The answers the open chat shows, in order.
    def answers(self) -> list[str]: ...

    def chat_id(self) -> str | None: ...


class PanelPage(Page, Protocol):
    """The cluster panel's demo buttons, for the infrastructure part."""

    def start(self, pattern: str) -> None: ...

    # Presses Kill on the pod the panel offers; that pod's name.
    def kill(self) -> str: ...


class Cluster(Protocol):
    # The vLLM pods Ready and not terminating.
    def ready_pods(self) -> list[str]: ...


class Waiter:
    """Waits on the page, failing the round on a fault shown at any poll, so one seen in passing
    counts too."""

    def __init__(
        self,
        page: Watched,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        poll_seconds: float,
    ) -> None:
        self._page = page
        # The rounds time their steps by the same clock.
        self.clock = clock
        self._sleep = sleep
        self._poll = poll_seconds

    def _poll_until(self, done: Callable[[], T | None], deadline: float) -> T | None:
        while True:
            faults = self._page.faults()
            if faults:
                raise RoundFailed("the page shows: " + "; ".join(faults))
            value = done()
            if value is not None:
                return value
            if self.clock() >= deadline:
                return None
            self._sleep(self._poll)

    def until(
        self, done: Callable[[], T | None], timeout: float, what: str, since: float | None = None
    ) -> T:
        """done's first value that is not None, within timeout of since (else of now)."""
        start = self.clock() if since is None else since
        value = self._poll_until(done, start + timeout)
        if value is None:
            raise RoundFailed(f"{what} not shown after {timeout:.0f} s")
        return value

    def settle(self, seconds: float, after: str) -> None:
        """Every working horse stops within seconds, or it is a stuck spinner."""
        if self._poll_until(
            lambda: True if self._page.working() == 0 else None, self.clock() + seconds
        ):
            return
        horses = self._page.working()
        raise RoundFailed(
            f"stuck spinner: {horses} working horse{'s' if horses != 1 else ''} "
            f"{seconds:.0f} s after {after}"
        )


def _answer(page: ChatPage, index: int) -> str | None:
    """The index-th answer the chat shows, once it does."""
    shown = page.answers()
    return shown[index] if len(shown) > index else None


def practical_round(
    page: ChatPage, rehearsal: Rehearsal, waiter: Waiter, number: int, email: str, password: str
) -> Round:
    """One practical round, stopping at its first failure."""
    practical = rehearsal.practical
    result = Round(PRACTICAL, number)
    clock = waiter.clock
    try:
        page.register(email, password)
        for c, chat in enumerate(practical.chats, start=1):
            page.new_chat()
            for t, turn in enumerate(chat.turns):
                name = turn.question.id
                page.send(turn.question.text, chat.files if t == 0 else [])
                sent = clock()
                shown = waiter.until(
                    partial(_answer, page, t),
                    practical.run_timeout,
                    f"the answer to {name}",
                )
                seconds = clock() - sent
                chat_id = page.chat_id()
                if chat_id is None:
                    raise RoundFailed(f"{name}: the page is not on a chat")
                messages = page.fetch(f"/api/chats/{chat_id}")["messages"]
                run_id = [m for m in messages if m["role"] == "assistant"][t]["run_id"]
                run = page.fetch(f"/api/runs/{run_id}")
                waiter.settle(rehearsal.settle_seconds, f"the answer to {name}")
                page.screenshot(f"{c}-{t + 1}-{name}")
                steps = run["steps"]
                result.steps.append(
                    {
                        "question": name,
                        "run_id": run_id,
                        "picked": steps[0]["content"] if steps else None,
                        "steps": len(steps),
                        "seconds": round(seconds, 1),
                        "status": run["status"],
                        "answer": run["answer"],
                        "shown": shown,
                    }
                )
                failures = judge_turn(turn, chat.pick, run, shown, practical.tolerance)
                if failures:
                    result.failures.extend(failures)
                    page.screenshot("failed")
                    return result
    except RoundFailed as failed:
        result.failures.append(str(failed))
        page.screenshot("failed")
    return result


def infrastructure_round(
    page: PanelPage,
    cluster: Cluster,
    rehearsal: Rehearsal,
    waiter: Waiter,
    number: int,
    email: str,
    password: str,
) -> Round:
    """One infrastructure round, stopping at its first failure."""
    infra = rehearsal.infrastructure
    clock = waiter.clock
    result = Round(INFRASTRUCTURE, number)
    try:
        waiter.until(
            lambda: True if len(cluster.ready_pods()) >= infra.replicas else None,
            infra.ready_timeout_seconds,
            f"{infra.replicas} vLLM pods Ready before the round",
        )
        page.register(email, password)
        demo = page.fetch("/api/demo")
        if not demo["enabled"] or demo["runner"] is None:
            caption = demo["runner_error"] or f"{infra.profile} has no demo buttons"
            raise RoundFailed(f"the demo buttons cannot be used: {caption}")
        before = (demo["runner"]["replay"] or {}).get("started_at")

        def replay() -> dict[str, Any] | None:
            current: dict[str, Any] | None = page.fetch("/api/demo")["runner"]["replay"]
            return current

        page.start(infra.pattern)
        started = clock()
        waiter.until(
            lambda: True if (replay() or {}).get("started_at") not in (None, before) else None,
            rehearsal.settle_seconds,
            "the replay started",
        )
        page.screenshot("started")
        waiter.until(
            lambda: True if clock() - started >= infra.kill_after_seconds else None,
            infra.kill_after_seconds,
            "the time to kill",
            since=started,
        )
        killed = page.kill()
        killed_at = clock()
        page.screenshot("killed")
        replaced_at: list[float] = []

        def ended() -> dict[str, Any] | None:
            if not replaced_at and replaced(cluster.ready_pods(), killed, infra.replicas):
                replaced_at.append(clock())
            body: dict[str, Any] = page.fetch("/api/demo")
            return body if body["runner"]["replay"]["state"] != "running" else None

        end = waiter.until(ended, infra.replay_timeout_seconds, "the replay's end", since=started)
        if not replaced_at:
            waiter.until(
                lambda: True if replaced(cluster.ready_pods(), killed, infra.replicas) else None,
                infra.ready_timeout_seconds,
                f"{killed}'s replacement Ready",
                since=killed_at,
            )
            replaced_at.append(clock())
        waiter.settle(rehearsal.settle_seconds, "the replay's end")
        page.screenshot("done")
        result.steps.append(
            {
                "pattern": infra.pattern,
                "killed": killed,
                "kill_at_s": round(killed_at - started, 1),
                "replaced_after_s": round(replaced_at[0] - killed_at, 1),
                "replay": end["runner"]["replay"],
                "last_kill": end["runner"]["last_kill"],
            }
        )
        result.failures.extend(judge_replay(end, infra, killed))
    except RoundFailed as failed:
        result.failures.append(str(failed))
        page.screenshot("failed")
    return result


class BrowserPage:
    """The UI in a Playwright page, found by role, label and text, as the presenter sees it, so the
    UI needs no hooks of its own (D14). A Playwright error is the round's failure."""

    def __init__(self, page: PlaywrightPage, rehearsal: Rehearsal, out: Path) -> None:
        self._page = page
        self._url = rehearsal.ui_url
        self._patterns = rehearsal.raw_errors
        self._out = out
        self._shots = 0
        # What the audience does not see, kept beside the round: the console's errors, requests that
        # failed and 5xx answers.
        self.unseen: list[str] = []
        page.on("console", self._console)
        page.on("requestfailed", self._request_failed)
        page.on("response", self._response)

    def _console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.unseen.append(f"console: {message.text}")

    def _request_failed(self, request: Request) -> None:
        self.unseen.append(f"failed: {request.method} {request.url}")

    def _response(self, response: Response) -> None:
        if response.status >= httpx.codes.INTERNAL_SERVER_ERROR:
            self.unseen.append(f"HTTP {response.status}: {response.url}")

    @contextmanager
    def _step(self, what: str) -> Iterator[None]:
        try:
            yield
        except PlaywrightError as error:
            raise RoundFailed(f"{what}: {error.message.splitlines()[0]}") from error

    def register(self, email: str, password: str) -> None:
        with self._step("register"):
            self._page.goto(f"{self._url}/register")
            self._page.get_by_label("Email").fill(email)
            self._page.get_by_label("Password").fill(password)
            self._page.get_by_role("button", name="Register").click()
            self._page.wait_for_url(re.compile(r"/chats$"))

    def new_chat(self) -> None:
        with self._step("new chat"):
            self._page.get_by_role("button", name="New chat").click()
            self._page.wait_for_url(re.compile(r"/chats$"))

    def send(self, text: str, files: Sequence[Path]) -> None:
        with self._step(f"send {text!r}"):
            self._page.get_by_label("Task").fill(text)
            if files:
                self._page.locator('input[type="file"]').set_input_files(list(files))
                for path in files:
                    self._page.locator(".ag-tag", has_text=path.name).first.wait_for()
            self._page.get_by_role("button", name="Send").click()

    def answers(self) -> list[str]:
        with self._step("read the answers"):
            return self._page.locator(ANSWER).all_inner_texts()

    def chat_id(self) -> str | None:
        match = CHAT_URL.search(self._page.url)
        return match.group(1) if match else None

    def fetch(self, path: str) -> Any:
        with self._step(f"GET {path}"):
            answer = self._page.evaluate(
                "async (path) => { const r = await fetch(path);"
                " return { status: r.status, body: await r.text() }; }",
                path,
            )
        if answer["status"] != httpx.codes.OK:
            raise RoundFailed(f"GET {path}: HTTP {answer['status']}: {answer['body'][:200]}")
        return json.loads(answer["body"])

    def faults(self) -> list[str]:
        with self._step("read the page"):
            lines = self._page.locator("body").inner_text().splitlines()
            alerts = self._page.locator(ALERT).filter(visible=True).count()
            notices = self._page.locator(FAIL_NOTICE).filter(visible=True).count()
        return page_faults(lines, alerts, notices, self._patterns)

    def working(self) -> int:
        with self._step("read the horses"):
            return self._page.locator(WORKING_HORSE).filter(visible=True).count()

    def screenshot(self, name: str) -> None:
        self._shots += 1
        with self._step(f"screenshot {name}"):
            self._page.screenshot(path=self._out / f"{self._shots:02d}-{name}.png")

    def start(self, pattern: str) -> None:
        with self._step(f"start {pattern}"):
            self._page.get_by_role("radio", name=pattern, exact=True).click()
            self._page.get_by_role("button", name=re.compile(r"Start \d+ tasks")).click()

    def kill(self) -> str:
        with self._step("kill"):
            pod = self._page.get_by_label("Pod to kill").input_value()
            self._page.get_by_role("button", name="Kill pod").click()
        return pod


class KubectlCluster:
    def ready_pods(self) -> list[str]:
        listed = kubectl("get", "pods", "--selector", VLLM_PODS, "-o", POD_FIELDS)
        pods = []
        for line in listed.splitlines():
            name, ready, deleting = line.split("\t")
            if ready == "True" and not deleting:
                pods.append(name)
        return pods


def _get(url: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
    try:
        return httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.TransportError as error:
        raise RehearsalError(f"nothing answers at {url} ({error}): run make ui first") from error


def check_ready(rehearsal: Rehearsal, part: str) -> None:
    """The cluster, the host and the helpers the part needs, each refusal naming what to run."""
    profile_name = getattr(rehearsal, part).profile
    profile = _profile(REPO_ROOT / "config", profile_name)
    running = yaml.safe_load(
        kubectl("get", "configmap", "agent-config", "-o", r"jsonpath={.data.profile\.yaml}")
    )
    differs = served_differs(running["model"], profile["model"])
    if differs is not None:
        raise RehearsalError(f"{differs}: run make up PROFILE={profile_name} first")
    try:
        check_gpu_blockers(profile["host"]["gpu_blockers"])
    except ExperimentError as error:
        raise RehearsalError(str(error)) from error
    shown = _get(f"{rehearsal.ui_url}/profile.json").json()["profile"]
    if shown != profile_name:
        raise RehearsalError(
            f"make ui serves {shown}: restart it with make ui PROFILE={profile_name}"
        )
    if _get(f"{rehearsal.ui_url}/api/healthz").status_code != httpx.codes.OK:
        raise RehearsalError("the UI reaches no app backend: run make app-forward")
    if part == INFRASTRUCTURE:
        demo = _demo_as_new_user(rehearsal.ui_url)
        if demo["runner"] is None:
            raise RehearsalError(f"the demo buttons cannot be used: {demo['runner_error']}")


def _demo_as_new_user(ui_url: str) -> dict[str, Any]:
    """GET /demo as a new user, through the UI's proxy as the browser goes. The session cookie is
    Secure (app.cookie_secure), which httpx keeps for HTTPS only, so it is sent by hand, as the task
    suite does."""
    api = f"{ui_url}/api"
    credentials = {"email": _email("check"), "password": secrets.token_hex(PASSWORD_BYTES)}
    with httpx.Client(base_url=api, timeout=REQUEST_TIMEOUT_SECONDS) as http:
        http.post("/auth/register", json=credentials).raise_for_status()
        login = http.post("/auth/login", json=credentials)
        login.raise_for_status()
        cookie = f"{SESSION_COOKIE}={login.cookies[SESSION_COOKIE]}"
        answer = http.get("/demo", headers={"cookie": cookie})
        answer.raise_for_status()
        demo: dict[str, Any] = answer.json()
    return demo


def _email(kind: str) -> str:
    return f"{kind}-{secrets.token_hex(6)}@{EMAIL_DOMAIN}"


def _write_json(path: Path, body: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(body, indent=2) + "\n")


def rehearse(rehearsal: Rehearsal, part: str, headed: bool) -> bool:
    """Every round of the part, back to back, until one fails; whether the part is green."""
    started = datetime.now(UTC)
    out = results_dir(rehearsal, part, started)
    out.mkdir(parents=True)
    source = source_state(REPO_ROOT)
    rounds: list[Round] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        for number in range(1, rehearsal.rounds + 1):
            round_dir = out / f"round-{number}"
            round_dir.mkdir()
            context = browser.new_context(
                viewport=VIEWPORT, record_video_dir=round_dir, record_video_size=VIEWPORT
            )
            raw = context.new_page()
            page = BrowserPage(raw, rehearsal, round_dir)
            waiter = Waiter(page, time.monotonic, time.sleep, POLL_SECONDS)
            email, password = _email(part), secrets.token_hex(PASSWORD_BYTES)
            print(f"demo-rehearsal: {part} round {number} as {email}", flush=True)
            if part == PRACTICAL:
                result = practical_round(page, rehearsal, waiter, number, email, password)
            else:
                result = infrastructure_round(
                    page, KubectlCluster(), rehearsal, waiter, number, email, password
                )
            context.close()
            if raw.video is not None:
                raw.video.save_as(round_dir / "video.webm")
                raw.video.delete()
            _write_json(round_dir / "round.json", {**result.record(), "unseen": page.unseen})
            rounds.append(result)
            verdict = "passed" if result.passed else "FAILED: " + "; ".join(result.failures)
            print(f"demo-rehearsal: {part} round {number} {verdict}", flush=True)
            if not result.passed:
                break
        browser.close()
    green = part_green(rounds, rehearsal.rounds)
    _write_json(
        out / "summary.json",
        {
            "rehearsal": str(rehearsal.path.relative_to(REPO_ROOT)),
            "hash": rehearsal.hash,
            "part": part,
            "profile": getattr(rehearsal, part).profile,
            "started_at": started.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "source": source,
            "rounds": [r.record() for r in rounds],
            "green": green,
        },
    )
    print(f"demo-rehearsal: {part} {'green' if green else 'red'}, results in {out}")
    return green


def main() -> None:
    parser = argparse.ArgumentParser(description="The demo rehearsal (P5.4).")
    parser.add_argument("rehearsal", type=Path, help="e.g. experiments/demo-rehearsal.yaml")
    parser.add_argument("--part", choices=PARTS, required=True)
    parser.add_argument("--headed", action="store_true", help="show the browser's window")
    args = parser.parse_args()
    try:
        rehearsal = load_rehearsal(args.rehearsal.resolve())
        check_ready(rehearsal, args.part)
    except (RehearsalError, subprocess.CalledProcessError) as error:
        sys.exit(f"demo-rehearsal: {error}")
    if not rehearse(rehearsal, args.part, args.headed):
        sys.exit(1)


if __name__ == "__main__":
    main()
