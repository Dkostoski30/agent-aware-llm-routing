"""GET /demo, POST /demo/replay and POST /demo/kill (P4.6a): the cluster panel's two demo buttons,
as schemas/demo.v1.json.

The buttons' commands run on the host, in make demo-runner (loadgen/load_generator/demo_runner.py),
as every replay always has: the replayer finds the cluster's Services with kubectl and Prometheus
scrapes it at the node's IP (P3.8). This module only passes a press on to that runner, which the
cluster reaches as a Service with no selector at the node's IP (infra/manifests/demo-runner.yaml),
with the token of the Secret demo-runner-token, and reports what the runner says.

A profile without the buttons (app.demo_controls.enabled false) gets no runner: GET says so and
the presses are 404. Sync httpx, like the runtime client: the endpoints run in FastAPI's
threadpool.
"""

import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app_backend import logs
from app_backend.config import AppConfig, DemoPattern

# The captions the UI shows when the runner gives no status, each naming what to do.
RUNNER_DOWN = "The demo runner is not running: make demo-runner"
# The Secret is made again by make up after make down, so a runner started before holds an old
# token until it is restarted.
TOKEN_REFUSED = "The demo runner refused the app backend's token: restart make demo-runner"

logger = logging.getLogger(__name__)


class RunnerUnavailable(Exception):
    """The runner gave no answer to use; the message is the UI's caption."""


class Replay(BaseModel):
    pattern: str
    command: str
    started_at: str
    state: Literal["running", "done", "failed"]
    runs_total: int
    runs_finished: int
    calls_failed: int
    exit_code: int | None


class Kill(BaseModel):
    pod: str
    at: str


class RunnerStatus(BaseModel):
    replay: Replay | None
    last_kill: Kill | None


class PatternRuns(BaseModel):
    name: str
    runs: int


class Demo(BaseModel):
    enabled: bool
    poll_seconds: int
    patterns: list[PatternRuns]
    runner: RunnerStatus | None
    runner_error: str | None


class ReplayPress(BaseModel):
    pattern: str


class KillPress(BaseModel):
    pod: str


class RunnerClient:
    def __init__(self, http: httpx.Client, token: str) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}

    def _send(self, method: str, path: str, body: dict[str, str] | None) -> httpx.Response:
        """The runner's answer. No connection, a timeout or a refused token is RunnerUnavailable;
        any other answer, a refusal included, is the caller's to pass on."""
        try:
            response = self._http.request(method, path, json=body, headers=self._headers)
        except httpx.HTTPError as error:
            logs.log(logger, logging.WARNING, "demo runner unavailable", error=repr(error))
            raise RunnerUnavailable(RUNNER_DOWN) from error
        if response.status_code == 401:
            logs.log(logger, logging.WARNING, "demo runner refused the token")
            raise RunnerUnavailable(TOKEN_REFUSED)
        return response

    def status(self) -> RunnerStatus:
        response = self._send("GET", "/status", None)
        response.raise_for_status()
        return RunnerStatus.model_validate(response.json())

    def press(self, path: str, body: dict[str, str]) -> RunnerStatus | JSONResponse:
        """The runner's new status when it took the press, else its refusal as it gave it."""
        response = self._send("POST", path, body)
        if response.is_success:
            return RunnerStatus.model_validate(response.json())
        return JSONResponse(status_code=response.status_code, content=response.json())


@dataclass(frozen=True)
class DemoControls:
    patterns: tuple[DemoPattern, ...]
    runner: RunnerClient


def router(controls: DemoControls | None, config: AppConfig, use_user_id: Any) -> APIRouter:
    """The demo endpoints for logged-in users; controls None is a profile without the buttons.
    Every user may press them, as every user sees the same cluster: a local demo (P4.6a)."""
    demo = APIRouter()
    UserId = Annotated[int, use_user_id]

    @demo.get("/demo", response_model=Demo)
    def read_demo(_: UserId) -> Demo:
        if controls is None:
            return Demo(
                enabled=False,
                poll_seconds=config.cluster_poll_seconds,
                patterns=[],
                runner=None,
                runner_error=None,
            )
        patterns = [PatternRuns(name=p.name, runs=p.runs) for p in controls.patterns]
        try:
            runner, runner_error = controls.runner.status(), None
        except RunnerUnavailable as error:
            runner, runner_error = None, str(error)
        return Demo(
            enabled=True,
            poll_seconds=config.cluster_poll_seconds,
            patterns=patterns,
            runner=runner,
            runner_error=runner_error,
        )

    def press(path: str, body: dict[str, str]) -> RunnerStatus | JSONResponse:
        if controls is None:
            raise HTTPException(status_code=404, detail="this profile has no demo buttons")
        try:
            answer = controls.runner.press(path, body)
        except RunnerUnavailable as error:
            return JSONResponse(status_code=503, content={"detail": str(error)})
        status = answer.status_code if isinstance(answer, JSONResponse) else 202
        logs.log(logger, logging.INFO, "demo press", path=path, status=status, **body)
        return answer

    @demo.post("/demo/replay", status_code=202, response_model=RunnerStatus)
    def start_replay(pressed: ReplayPress, _: UserId) -> RunnerStatus | JSONResponse:
        return press("/replay", {"pattern": pressed.pattern})

    @demo.post("/demo/kill", status_code=202, response_model=RunnerStatus)
    def kill_pod(pressed: KillPress, _: UserId) -> RunnerStatus | JSONResponse:
        return press("/kill", {"pod": pressed.pod})

    return demo
