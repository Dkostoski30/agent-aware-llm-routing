"""The agent runtime as the app backend calls it: start a run, read how it ended (P3.12a), pass
a run's detail, calls and events on as the runtime sent them (P3.12b).

Sync httpx for everything but the events: those endpoints are sync and run in FastAPI's threadpool,
like the sqlite3 calls beside them. The events stream is async, so a browser that leaves cancels it
and closes the runtime's stream at once, where a sync stream would hold a threadpool thread until
the runtime's next byte. Both clients are built by from_env with AGENT_URL and
app.runtime_timeout_seconds; tests build them on an httpx.MockTransport.
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


@dataclass(frozen=True)
class Forwarded:
    """A runtime answer as it came: its status, body and content type."""

    status: int
    body: bytes
    content_type: str


class RuntimeClient:
    def __init__(self, http: httpx.Client, events_http: httpx.AsyncClient) -> None:
        self._http = http
        self._events_http = events_http

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

    def forward(self, path: str) -> Forwarded:
        """GET path on the runtime, answered as it came, a 404 included."""
        response = self._send("GET", path)
        return Forwarded(
            status=response.status_code,
            body=response.content,
            content_type=response.headers["content-type"],
        )

    async def open_events(self, run_id: str) -> httpx.Response:
        """GET /runs/{id}/events, opened but not read: the caller reads the stream and closes it.
        RuntimeUnavailable like _send, the stream closed first."""
        path = f"/runs/{run_id}/events"
        request = self._events_http.build_request("GET", path)
        try:
            response = await self._events_http.send(request, stream=True)
        except httpx.HTTPError as error:
            raise RuntimeUnavailable(f"GET {path}: {error!r}") from error
        if response.is_server_error:
            await response.aclose()
            raise RuntimeUnavailable(f"GET {path}: {response.status_code}")
        return response
