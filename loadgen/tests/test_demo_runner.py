"""make demo-runner (P4.6a): the commands the cluster panel's buttons run on the host, and the HTTP
API the app backend asks, on a fake launcher, kubectl and clock.

The live path (a real replay and a real kill from the UI) is the card's done-when; here the
processes and the cluster are replaced by recordings of what they were asked.
"""

import base64
import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator
from load_generator.demo_runner import (
    TOKEN_SECRET,
    VLLM_PODS,
    Busy,
    DemoRunnerError,
    DemoSettings,
    NoSuchPod,
    Runner,
    UnknownPattern,
    demo_settings,
    read_token,
    replay_counts,
    require_enabled,
    serve,
)
from load_generator.record import REPO_ROOT

PROFILES = ["real", "real-plain", "demo", "sim"]
TOKEN = "f" * 64
STARTED = datetime(2026, 11, 20, 21, 5, 3, 250_000, tzinfo=UTC)
TRACE = "experiments/results/traces/record-demo-e1044feb534c-d86ed436c5b2-5554fcf81b09.jsonl"
OUT = "experiments/results/replays/demo-steady-20261120T210503.jsonl"
POD_A = "vllm-568fc4b669-c9kb2"
POD_B = "vllm-568fc4b669-fgdgm"
SETTINGS = DemoSettings(enabled=True, port=9401, trace=TRACE, patterns={"steady": 20, "burst": 30})
DEMO_SCHEMA = json.loads((REPO_ROOT / "schemas" / "demo.v1.json").read_text())
# The runner's GET /status is demo.v1's runner object.
STATUS_VALIDATOR = Draft202012Validator({"$defs": DEMO_SCHEMA["$defs"], "$ref": "#/$defs/runner"})


def profile(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / "config" / f"{name}.yaml").read_text())
    return loaded


class FakeChild:
    """A replay process: running until a test sets code; interrupt ends it as Ctrl+C would."""

    def __init__(self) -> None:
        self.code: int | None = None
        self.interrupted = False

    def poll(self) -> int | None:
        return self.code

    def interrupt(self) -> None:
        self.interrupted = True
        self.code = 130

    def wait(self) -> int:
        assert self.code is not None, "waited on a child nobody ended"
        return self.code


class FakeLauncher:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.children: list[FakeChild] = []

    def __call__(self, command: list[str]) -> FakeChild:
        self.commands.append(command)
        child = FakeChild()
        self.children.append(child)
        return child


class FakeKubectl:
    """Answers the pod list with pods; records every call."""

    def __init__(self, pods: list[str]) -> None:
        self.pods = pods
        self.calls: list[tuple[str, ...]] = []
        self.fail_delete = False

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[:2] == ("get", "pods"):
            return " ".join(self.pods)
        if args[:2] == ("delete", "pod") and self.fail_delete:
            raise subprocess.CalledProcessError(1, ["kubectl", *args], stderr="Forbidden")
        if args[:2] == ("delete", "pod"):
            return ""
        raise AssertionError(f"unexpected kubectl {args}")


@pytest.fixture
def launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def kubectl() -> FakeKubectl:
    return FakeKubectl([POD_A, POD_B])


@pytest.fixture
def runner(tmp_path: Path, launcher: FakeLauncher, kubectl: FakeKubectl) -> Runner:
    return Runner(SETTINGS, "real", launcher, kubectl, lambda: STARTED, tmp_path)


def write_runs(path: Path, run_ids: list[str], failed: tuple[int, ...] = ()) -> None:
    """Trace lines as the replay writes them, a run's calls in a row; only run_id and duration_ms
    are read. The lines at the indexes in failed are calls with no full reply: no duration_ms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        for index, run_id in enumerate(run_ids):
            line: dict[str, Any] = {"run_id": run_id, "step": 1}
            if index not in failed:
                line["duration_ms"] = 900
            out.write(json.dumps(line) + "\n")


# --- settings from the profile


@pytest.mark.parametrize("name", PROFILES)
def test_every_profile_has_the_demo_controls(name: str) -> None:
    settings = demo_settings(profile(name))
    assert settings.enabled is (name in ("real", "real-plain"))
    assert settings.patterns
    assert (REPO_ROOT / settings.trace).is_file()


def test_the_settings_carry_each_patterns_runs_from_replay_patterns() -> None:
    real = profile("real")
    settings = demo_settings(real)
    assert settings.port == real["app"]["demo_controls"]["runner_port"]
    assert settings.patterns == {
        name: real["replay"]["patterns"][name]["runs"]
        for name in real["app"]["demo_controls"]["patterns"]
    }
    assert list(settings.patterns) == real["app"]["demo_controls"]["patterns"]


def test_the_runner_port_is_not_the_replay_trackers() -> None:
    real = profile("real")
    assert demo_settings(real).port != real["replay"]["tracker_port"]


def test_a_pattern_that_is_not_a_replay_pattern_is_refused() -> None:
    real = profile("real")
    real["app"]["demo_controls"]["patterns"] = ["steady", "tidal"]
    with pytest.raises(DemoRunnerError, match="tidal"):
        demo_settings(real)


def test_a_profile_without_the_buttons_is_refused() -> None:
    require_enabled(demo_settings(profile("real")), "real")
    with pytest.raises(DemoRunnerError, match=r"demo: app\.demo_controls\.enabled is false"):
        require_enabled(demo_settings(profile("demo")), "demo")


def test_the_token_is_read_from_the_secret() -> None:
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str) -> str:
        calls.append(args)
        return base64.b64encode(TOKEN.encode()).decode()

    assert read_token(kubectl) == TOKEN
    assert calls == [("get", "secret", TOKEN_SECRET, "-o", "jsonpath={.data.token}")]


def test_a_secret_without_a_token_is_refused() -> None:
    with pytest.raises(DemoRunnerError, match=TOKEN_SECRET):
        read_token(lambda *args: "")


def test_a_missing_secret_is_refused_with_kubectls_message() -> None:
    def kubectl(*args: str) -> str:
        raise subprocess.CalledProcessError(
            1, ["kubectl", *args], stderr='secrets "demo-runner-token" not found'
        )

    with pytest.raises(DemoRunnerError, match="not found"):
        read_token(kubectl)


# --- the replay


def test_start_runs_make_replay_with_the_pattern_and_a_file_named_by_the_time(
    runner: Runner, launcher: FakeLauncher
) -> None:
    status = runner.start("steady")
    assert launcher.commands == [
        [
            "make",
            "replay",
            f"TRACE={TRACE}",
            "PATTERN=steady",
            "PROFILE=real",
            f"OUT={OUT}",
        ]
    ]
    assert status["replay"] == {
        "pattern": "steady",
        "command": " ".join(launcher.commands[0]),
        "started_at": "2026-11-20T21:05:03Z",
        "state": "running",
        "runs_total": 20,
        "runs_finished": 0,
        "calls_failed": 0,
        "exit_code": None,
    }
    assert STATUS_VALIDATOR.is_valid(status)


def test_status_before_anything_ran_is_empty(runner: Runner) -> None:
    status = runner.status()
    assert status == {"replay": None, "last_kill": None}
    assert STATUS_VALIDATOR.is_valid(status)


def test_a_second_start_while_one_runs_is_refused(runner: Runner, launcher: FakeLauncher) -> None:
    runner.start("steady")
    with pytest.raises(Busy):
        runner.start("burst")
    assert len(launcher.commands) == 1


def test_a_start_after_the_replay_ended_runs_again(
    tmp_path: Path, launcher: FakeLauncher, kubectl: FakeKubectl
) -> None:
    times = iter([STARTED, datetime(2026, 11, 20, 21, 20, 0, tzinfo=UTC)])
    runner = Runner(SETTINGS, "real", launcher, kubectl, lambda: next(times), tmp_path)
    runner.start("steady")
    launcher.children[0].code = 0
    status = runner.start("burst")
    assert launcher.commands[1][3] == "PATTERN=burst"
    assert (
        launcher.commands[1][5]
        == "OUT=experiments/results/replays/demo-burst-20261120T212000.jsonl"
    )
    assert status["replay"]["runs_total"] == 30


def test_an_unknown_pattern_is_refused_and_nothing_runs(
    runner: Runner, launcher: FakeLauncher
) -> None:
    with pytest.raises(UnknownPattern):
        runner.start("steady-long")
    assert launcher.commands == []


@pytest.mark.parametrize("code", [0, 2])
def test_a_replay_that_wrote_its_file_is_done_whatever_its_exit_code(
    tmp_path: Path, runner: Runner, launcher: FakeLauncher, code: int
) -> None:
    """make exits 2 for a replay with a lost call as for one that crashed, so the exit code
    cannot tell them apart; the file can, since the replay writes it after its last run either
    way (the user, 2026-11-21: name the lost calls, not "failed")."""
    runner.start("steady")
    write_runs(tmp_path / OUT, ["run-a", "run-b"], failed=(1,))
    launcher.children[0].code = code
    replay = runner.status()["replay"]
    assert (replay["state"], replay["exit_code"], replay["calls_failed"]) == ("done", code, 1)
    assert STATUS_VALIDATOR.is_valid(runner.status())


@pytest.mark.parametrize("code", [0, 2])
def test_a_replay_that_ended_without_its_file_failed(
    tmp_path: Path, runner: Runner, launcher: FakeLauncher, code: int
) -> None:
    runner.start("steady")
    write_runs(tmp_path / f"{OUT}.partial", ["run-a"])
    launcher.children[0].code = code
    replay = runner.status()["replay"]
    assert (replay["state"], replay["exit_code"], replay["runs_finished"]) == ("failed", code, 1)


def test_a_running_replay_is_running_even_once_its_file_is_there(
    tmp_path: Path, runner: Runner
) -> None:
    runner.start("steady")
    write_runs(tmp_path / OUT, ["run-a"])
    assert runner.status()["replay"]["state"] == "running"


def test_finished_runs_are_counted_from_the_partial_file_then_the_written_one(
    tmp_path: Path, runner: Runner, launcher: FakeLauncher
) -> None:
    runner.start("steady")
    partial = tmp_path / f"{OUT}.partial"
    write_runs(partial, ["run-a", "run-a", "run-a"])
    write_runs(partial, ["run-b", "run-b"])
    assert runner.status()["replay"]["runs_finished"] == 2
    write_runs(tmp_path / OUT, ["run-a", "run-b", "run-c"])
    partial.unlink()
    launcher.children[0].code = 0
    assert runner.status()["replay"]["runs_finished"] == 3


def test_lost_calls_are_counted_as_the_runs_are(tmp_path: Path, runner: Runner) -> None:
    runner.start("steady")
    partial = tmp_path / f"{OUT}.partial"
    write_runs(partial, ["run-a", "run-a", "run-b", "run-c"], failed=(0, 2))
    replay = runner.status()["replay"]
    assert (replay["runs_finished"], replay["calls_failed"]) == (3, 2)


def test_no_file_yet_is_no_run_finished_and_no_call_lost(tmp_path: Path) -> None:
    assert replay_counts(tmp_path / OUT) == (0, 0)


def test_the_written_file_wins_over_a_partial_one_left_beside_it(tmp_path: Path) -> None:
    write_runs(tmp_path / f"{OUT}.partial", ["run-a"], failed=(0,))
    write_runs(tmp_path / OUT, ["run-a", "run-b"])
    assert replay_counts(tmp_path / OUT) == (2, 0)


def test_stop_interrupts_a_running_replay(runner: Runner, launcher: FakeLauncher) -> None:
    runner.start("steady")
    runner.stop()
    assert launcher.children[0].interrupted


def test_stop_leaves_an_ended_replay_alone(runner: Runner, launcher: FakeLauncher) -> None:
    runner.start("steady")
    launcher.children[0].code = 0
    runner.stop()
    assert not launcher.children[0].interrupted


def test_stop_with_nothing_started_does_nothing(runner: Runner) -> None:
    runner.stop()


# --- the kill


def test_kill_force_deletes_an_existing_vllm_pod(runner: Runner, kubectl: FakeKubectl) -> None:
    status = runner.kill(POD_B)
    assert kubectl.calls == [
        ("get", "pods", "-l", VLLM_PODS, "-o", "jsonpath={.items[*].metadata.name}"),
        ("delete", "pod", POD_B, "--grace-period=0", "--force", "--wait=false"),
    ]
    assert status["last_kill"] == {"pod": POD_B, "at": "2026-11-20T21:05:03Z"}
    assert STATUS_VALIDATOR.is_valid(status)


@pytest.mark.parametrize("pod", ["vllm-568fc4b669-xxxxx", "redis-0", "", f"{POD_A} {POD_B}"])
def test_kill_of_a_pod_that_is_not_a_vllm_pod_deletes_nothing(
    runner: Runner, kubectl: FakeKubectl, pod: str
) -> None:
    with pytest.raises(NoSuchPod):
        runner.kill(pod)
    assert all(call[0] != "delete" for call in kubectl.calls)
    assert runner.status()["last_kill"] is None


def test_a_failed_delete_is_not_recorded_as_a_kill(runner: Runner, kubectl: FakeKubectl) -> None:
    kubectl.fail_delete = True
    with pytest.raises(subprocess.CalledProcessError):
        runner.kill(POD_A)
    assert runner.status()["last_kill"] is None


def test_a_kill_does_not_touch_the_replay(runner: Runner, launcher: FakeLauncher) -> None:
    runner.start("steady")
    status = runner.kill(POD_A)
    assert status["replay"]["state"] == "running"
    assert not launcher.children[0].interrupted


# --- the HTTP API


@pytest.fixture
def api(runner: Runner) -> Iterator[httpx.Client]:
    with (
        serve(runner, TOKEN, 0) as port,
        httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {TOKEN}"}
        ) as client,
    ):
        yield client


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic x"}],
)
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/status", None),
        ("POST", "/replay", {"pattern": "steady"}),
        ("POST", "/kill", {"pod": POD_A}),
    ],
)
def test_every_request_needs_the_token(
    runner: Runner,
    launcher: FakeLauncher,
    kubectl: FakeKubectl,
    headers: dict[str, str],
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    with serve(runner, TOKEN, 0) as port:
        response = httpx.request(
            method, f"http://127.0.0.1:{port}{path}", headers=headers, json=body
        )
    assert response.status_code == 401
    assert launcher.commands == []
    assert kubectl.calls == []


def test_get_status_answers_the_runners_status(api: httpx.Client) -> None:
    response = api.get("/status")
    assert response.status_code == 200
    assert response.json() == {"replay": None, "last_kill": None}


def test_post_replay_starts_one_and_answers_its_status(
    api: httpx.Client, launcher: FakeLauncher
) -> None:
    response = api.post("/replay", json={"pattern": "burst"})
    assert response.status_code == 202
    assert response.json()["replay"]["pattern"] == "burst"
    assert len(launcher.commands) == 1


def test_post_replay_while_one_runs_is_409(api: httpx.Client, launcher: FakeLauncher) -> None:
    api.post("/replay", json={"pattern": "steady"})
    response = api.post("/replay", json={"pattern": "steady"})
    assert response.status_code == 409
    assert "a replay is running" in response.json()["detail"]
    assert len(launcher.commands) == 1


def test_post_replay_of_an_unknown_pattern_is_422(api: httpx.Client) -> None:
    response = api.post("/replay", json={"pattern": "steady-long"})
    assert response.status_code == 422
    assert "steady-long" in response.json()["detail"]


def test_post_kill_deletes_the_pod_and_answers_202(api: httpx.Client, kubectl: FakeKubectl) -> None:
    response = api.post("/kill", json={"pod": POD_A})
    assert response.status_code == 202
    assert response.json()["last_kill"]["pod"] == POD_A
    assert kubectl.calls[-1][:3] == ("delete", "pod", POD_A)


def test_post_kill_of_an_unknown_pod_is_404(api: httpx.Client) -> None:
    response = api.post("/kill", json={"pod": "vllm-gone"})
    assert response.status_code == 404
    assert "vllm-gone" in response.json()["detail"]


def test_a_kubectl_failure_is_502_with_its_message(api: httpx.Client, kubectl: FakeKubectl) -> None:
    kubectl.fail_delete = True
    response = api.post("/kill", json={"pod": POD_A})
    assert response.status_code == 502
    assert "Forbidden" in response.json()["detail"]


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("/replay", b"not json"),
        ("/replay", b"{}"),
        ("/replay", b'{"pattern": 3}'),
        ("/kill", b'{"name": "vllm-a"}'),
        ("/kill", b"[]"),
    ],
)
def test_a_body_without_its_field_is_400(api: httpx.Client, path: str, content: bytes) -> None:
    response = api.post(path, content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/replay"), ("POST", "/status"), ("GET", "/")]
)
def test_any_other_route_is_404(api: httpx.Client, method: str, path: str) -> None:
    assert api.request(method, path).status_code == 404
