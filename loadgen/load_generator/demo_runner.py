"""make demo-runner (P4.6a): the cluster panel's two demo buttons, run on the host.

Start runs the same make replay a person would type, on the profile's recorded demo trace with the
pattern pressed, as a child process writing to this terminal; Kill force-deletes a vLLM pod with
kubectl, as P4.4's checks do, so its calls fail and are sent again elsewhere. Both run here, not in
the cluster, since the replayer has always run on the host: it finds the cluster's Services with
kubectl, and Prometheus scrapes its run tracker at the node's IP (P3.8). The app backend asks this
server through a Service with no selector at the node's IP (infra/manifests/demo-runner.yaml);
every request carries the token of the Secret demo-runner-token, since anything in the cluster
can reach that address and Kill deletes pods.

One replay at a time: the replay's run tracker takes replay.tracker_port. Each press writes a new
file, named by its start time, so presses never meet the replay's refusal of an existing file.
Ctrl+C interrupts a running replay before the runner exits, so nothing goes on replaying unseen;
the replay keeps its finished runs in its .partial file, as on any interrupt.

Usage: python -m load_generator.demo_runner [--profile real]
"""

import argparse
import base64
import contextlib
import hmac
import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import yaml

from load_generator.record import REPO_ROOT
from load_generator.replay import failed_calls

# The Secret infra/deploy-app.sh makes, and its key.
TOKEN_SECRET = "demo-runner-token"
# The vLLM Deployment's pods (infra/charts/vllm), the only pods Kill may delete.
VLLM_PODS = "app.kubernetes.io/name=vllm"
REPLAYS_DIR = "experiments/results/replays"
# Every address of the host: the app backend comes in on the node's IP, which a WSL restart
# changes (infra/deploy-app.sh writes the current one into the Endpoints).
ALL_INTERFACES = ""
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FILE_TIME_FORMAT = "%Y%m%dT%H%M%S"


class DemoRunnerError(Exception):
    """The runner must not start: its settings or the cluster are not what it needs."""


class Busy(Exception):
    """A replay is running."""


class UnknownPattern(Exception):
    """The pattern is not one of app.demo_controls.patterns."""


class NoSuchPod(Exception):
    """No vLLM pod has that name."""


@dataclass(frozen=True)
class DemoSettings:
    enabled: bool
    port: int
    # Relative to the repo, as make replay is run from there.
    trace: str
    # Each pattern Start offers, in the profile's order, with the runs replay.patterns gives it.
    patterns: dict[str, int]


def demo_settings(profile: Mapping[str, Any]) -> DemoSettings:
    controls = profile["app"]["demo_controls"]
    replay_patterns = profile["replay"]["patterns"]
    missing = [name for name in controls["patterns"] if name not in replay_patterns]
    if missing:
        raise DemoRunnerError(
            f"app.demo_controls.patterns names {', '.join(missing)}, not in replay.patterns"
        )
    return DemoSettings(
        enabled=controls["enabled"],
        port=controls["runner_port"],
        trace=controls["trace"],
        patterns={name: replay_patterns[name]["runs"] for name in controls["patterns"]},
    )


def require_enabled(settings: DemoSettings, profile_name: str) -> None:
    if not settings.enabled:
        raise DemoRunnerError(
            f"{profile_name}: app.demo_controls.enabled is false, so the panel shows no buttons"
        )


Kubectl = Callable[..., str]


def read_token(kubectl: Kubectl) -> str:
    try:
        encoded = kubectl("get", "secret", TOKEN_SECRET, "-o", "jsonpath={.data.token}")
    except subprocess.CalledProcessError as error:
        raise DemoRunnerError(
            f"{(error.stderr or '').strip()}: make up makes the secret"
        ) from error
    if not encoded:
        raise DemoRunnerError(f"secret {TOKEN_SECRET} has no token: make up makes it")
    return base64.b64decode(encoded).decode()


class ReplayCounts(NamedTuple):
    runs_finished: int
    calls_failed: int


def replay_counts(out: Path) -> ReplayCounts:
    """The runs a replay has written, and the calls among them with no full reply by the replay's
    own rule (load_generator.replay.failed_calls): from the output once it is there, else from the
    .partial file it appends each run's lines to as the run ends."""
    partial = out.with_name(f"{out.name}.partial")
    path = out if out.exists() else partial
    if not path.exists():
        return ReplayCounts(0, 0)
    with path.open() as text:
        lines = [json.loads(line) for line in text]
    return ReplayCounts(len({line["run_id"] for line in lines}), failed_calls(lines))


class Child(Protocol):
    def poll(self) -> int | None: ...

    def interrupt(self) -> None: ...

    def wait(self) -> int: ...


Launch = Callable[[list[str]], Child]
Clock = Callable[[], datetime]


@dataclass
class _Replay:
    pattern: str
    command: list[str]
    out: Path
    started: datetime
    runs_total: int
    child: Child

    def status(self) -> dict[str, Any]:
        """Done once the replay ended with its output written, which it writes after its last
        run even with calls lost; make then exits 2, as for a crash, so the exit code alone
        cannot tell the two apart (the user, 2026-11-21)."""
        code = self.child.poll()
        state = "running" if code is None else "done" if self.out.exists() else "failed"
        counts = replay_counts(self.out)
        return {
            "pattern": self.pattern,
            "command": " ".join(self.command),
            "started_at": self.started.strftime(TIME_FORMAT),
            "state": state,
            "runs_total": self.runs_total,
            "runs_finished": counts.runs_finished,
            "calls_failed": counts.calls_failed,
            "exit_code": code,
        }


class Runner:
    """The latest replay and kill since the runner started, behind one lock: the server answers
    each request on a thread of its own. root is where make runs and the replay writes."""

    def __init__(
        self,
        settings: DemoSettings,
        profile_name: str,
        launch: Launch,
        kubectl: Kubectl,
        clock: Clock,
        root: Path = REPO_ROOT,
    ) -> None:
        self._settings = settings
        self._profile_name = profile_name
        self._launch = launch
        self._kubectl = kubectl
        self._clock = clock
        self._root = root
        self._lock = threading.Lock()
        self._replay: _Replay | None = None
        self._last_kill: dict[str, str] | None = None

    def _status(self) -> dict[str, Any]:
        return {
            "replay": None if self._replay is None else self._replay.status(),
            "last_kill": self._last_kill,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status()

    def start(self, pattern: str) -> dict[str, Any]:
        with self._lock:
            if pattern not in self._settings.patterns:
                raise UnknownPattern(
                    f"{pattern} is not one of {', '.join(self._settings.patterns)}"
                )
            if self._replay is not None and self._replay.child.poll() is None:
                raise Busy(f"a replay is running: {' '.join(self._replay.command)}")
            started = self._clock()
            out = f"{REPLAYS_DIR}/demo-{pattern}-{started.strftime(FILE_TIME_FORMAT)}.jsonl"
            command = [
                "make",
                "replay",
                f"TRACE={self._settings.trace}",
                f"PATTERN={pattern}",
                f"PROFILE={self._profile_name}",
                f"OUT={out}",
            ]
            print(f"demo-runner: {' '.join(command)}", flush=True)
            self._replay = _Replay(
                pattern=pattern,
                command=command,
                out=self._root / out,
                started=started,
                runs_total=self._settings.patterns[pattern],
                child=self._launch(command),
            )
            return self._status()

    def kill(self, pod: str) -> dict[str, Any]:
        """Deletes pod with no grace, if the vLLM Deployment has it. A kubectl that fails raises
        CalledProcessError, and the kill is not recorded."""
        with self._lock:
            names = self._kubectl(
                "get", "pods", "-l", VLLM_PODS, "-o", "jsonpath={.items[*].metadata.name}"
            ).split()
            if pod not in names:
                raise NoSuchPod(f"no vLLM pod {pod}")
            self._kubectl("delete", "pod", pod, "--grace-period=0", "--force", "--wait=false")
            print(f"demo-runner: killed {pod}", flush=True)
            self._last_kill = {"pod": pod, "at": self._clock().strftime(TIME_FORMAT)}
            return self._status()

    def stop(self) -> None:
        with self._lock:
            if self._replay is not None and self._replay.child.poll() is None:
                print("demo-runner: interrupting the replay", flush=True)
                self._replay.child.interrupt()
                self._replay.child.wait()


@contextlib.contextmanager
def serve(runner: Runner, token: str, port: int) -> Iterator[int]:
    """GET /status, POST /replay {pattern} and POST /kill {pod} on port, from a thread of its own,
    until the block ends; yields the port it listens on (0 lets the system pick one)."""
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, code: int, body: Mapping[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "").encode()
            if hmac.compare_digest(given, expected):
                return True
            self._answer(401, {"detail": "the token is missing or wrong"})
            return False

        def _field(self, name: str) -> str | None:
            """The body's string field name, or None after answering 400."""
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                body = None
            value = body.get(name) if isinstance(body, dict) else None
            if isinstance(value, str):
                return value
            self._answer(400, {"detail": f"the body needs a string field {name}"})
            return None

        def do_GET(self) -> None:
            if not self._authorized():
                return
            if self.path != "/status":
                self._answer(404, {"detail": f"no route GET {self.path}"})
                return
            self._answer(200, runner.status())

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self.path == "/replay":
                self._replay()
            elif self.path == "/kill":
                self._kill()
            else:
                self._answer(404, {"detail": f"no route POST {self.path}"})

        def _replay(self) -> None:
            pattern = self._field("pattern")
            if pattern is None:
                return
            try:
                self._answer(202, runner.start(pattern))
            except UnknownPattern as error:
                self._answer(422, {"detail": str(error)})
            except Busy as error:
                self._answer(409, {"detail": str(error)})

        def _kill(self) -> None:
            pod = self._field("pod")
            if pod is None:
                return
            try:
                self._answer(202, runner.kill(pod))
            except NoSuchPod as error:
                self._answer(404, {"detail": str(error)})
            except subprocess.CalledProcessError as error:
                message = (error.stderr or "").strip() or f"exit {error.returncode}"
                self._answer(502, {"detail": f"kubectl {error.cmd[1]} failed: {message}"})

        def log_message(self, format: str, *args: Any) -> None:
            """Silent: the panel asks every scrape interval, and a line each would bury the
            replay's own lines in this terminal."""

    server = ThreadingHTTPServer((ALL_INTERFACES, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="demo-runner", daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class _Process:
    """make replay in a session of its own, so the terminal's Ctrl+C reaches the runner alone and
    the runner interrupts make and the replay under it together."""

    def __init__(self, command: list[str]) -> None:
        self._popen = subprocess.Popen(command, cwd=REPO_ROOT, start_new_session=True)

    def poll(self) -> int | None:
        return self._popen.poll()

    def interrupt(self) -> None:
        os.killpg(self._popen.pid, signal.SIGINT)

    def wait(self) -> int:
        return self._popen.wait()


def kubectl(*args: str) -> str:
    """kubectl's output, its message kept on the error, so the panel can show why it failed."""
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser(description="P4.6a: the demo buttons' commands on the host.")
    parser.add_argument("--profile", default="real", help="config/<profile>.yaml")
    args = parser.parse_args()

    profile = yaml.safe_load((REPO_ROOT / "config" / f"{args.profile}.yaml").read_text())
    try:
        settings = demo_settings(profile)
        require_enabled(settings, args.profile)
        token = read_token(kubectl)
    except DemoRunnerError as error:
        raise SystemExit(f"demo-runner: {error}") from error
    runner = Runner(settings, args.profile, _Process, kubectl, lambda: datetime.now(UTC))
    with serve(runner, token, settings.port) as port:
        print(f"demo-runner: listening on port {port} until Ctrl+C", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            runner.stop()


if __name__ == "__main__":
    main()
