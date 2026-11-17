"""The agent runtime as the app backend calls it (P3.12a): start a run, read how it ended.

Sync httpx: the chat endpoints are sync and run in FastAPI's threadpool, like the sqlite3 calls
beside them. The client is built by from_env with AGENT_URL and app.runtime_timeout_seconds; tests
build it on an httpx.MockTransport.
"""

from dataclasses import dataclass
from typing import Any, Literal

import httpx

RunStatus = Literal["running", "done", "failed"]


class RuntimeUnavailable(Exception):
    """No answer the app can use: the connection failed, timed out, or the runtime answered 5xx."""


class RunRefused(Exception):
    """The runtime answered 4xx to a run request; the message is its detail, e.g. which file."""


@dataclass(frozen=True)
class RunState:
    status: RunStatus
    answer: str | None
    error: str | None


class RuntimeClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._http.request(method, path, json=body)
        except httpx.HTTPError as error:
            raise RuntimeUnavailable(f"{method} {path}: {error!r}") from error
        if response.is_server_error:
            raise RuntimeUnavailable(f"{method} {path}: {response.status_code}")
        return response

    def start_run(self, request: dict[str, Any]) -> str:
        """POST /runs with a run-request.v1 body; the new run's id."""
        response = self._send("POST", "/runs", request)
        if response.is_client_error:
            raise RunRefused(response.json()["detail"])
        response.raise_for_status()
        run_id: str = response.json()["run_id"]
        return run_id

    def get_run(self, run_id: str) -> RunState | None:
        """GET /runs/{id}; None when the runtime no longer has the run (its Redis TTL ran out)."""
        response = self._send("GET", f"/runs/{run_id}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        run = response.json()
        return RunState(status=run["status"], answer=run["answer"], error=run["error"])
