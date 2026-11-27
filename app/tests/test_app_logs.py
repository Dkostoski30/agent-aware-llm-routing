"""The app backend's log lines (P2.10a, D12): JSON, and every line of a request carries its
request-id, the same one the response returns in x-request-id."""

import json
import logging
from pathlib import Path

import httpx
import pytest
from app_backend.app import create_app
from app_backend.cluster import ClusterReader, PodLister
from app_backend.config import AppConfig, ScryptParams
from app_backend.logs import JsonFormatter
from app_backend.runtime import RuntimeClient
from fastapi.testclient import TestClient

EMAIL = "ana@example.com"
PASSWORD = "a long enough password"


def no_prometheus() -> ClusterReader:
    """Only GET /cluster asks Prometheus (test_cluster.py); a query here would fail the test."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"asked Prometheus or Kubernetes: {request.url}")

    return ClusterReader(
        httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://prometheus"),
        PodLister(
            httpx.Client(transport=httpx.MockTransport(refuse), base_url="https://kubernetes"),
            lambda: "token",
        ),
    )


def make_client(tmp_path: Path) -> TestClient:
    config = AppConfig(
        session_ttl_seconds=60,
        scrypt=ScryptParams(n=2, r=1, p=1),
        min_password_length=15,
        cookie_secure=True,
        max_files=5,
        runtime_timeout_seconds=5.0,
        cluster_poll_seconds=5,
        kv_cache_tokens=4384,
        pressure_warn=1.0,
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"auth called the runtime: {request.method} {request.url}")

    # Auth never calls the agent runtime; a call would fail the test.
    runtime = RuntimeClient(
        httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://agent"),
        httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://agent"),
    )
    app = create_app(config, tmp_path / "app.db", runtime, no_prometheus(), lambda: 1000.0)
    return TestClient(app, base_url="https://testserver")


def test_every_line_of_a_request_carries_its_request_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(tmp_path)
    credentials = {"email": EMAIL, "password": PASSWORD}
    for path, body in [
        ("/auth/register", credentials),
        ("/auth/login", credentials),
        ("/auth/login", {"email": EMAIL, "password": "a wrong password here"}),
    ]:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app_backend"):
            response = client.post(path, json=body)
        request_id = response.headers["x-request-id"]
        lines = [r for r in caplog.records if r.name.startswith("app_backend")]
        # An event line (registered, logged in, login refused) and the access line.
        assert len(lines) == 2, [r.getMessage() for r in lines]
        assert {getattr(r, "request-id") for r in lines} == {request_id}


def test_two_requests_get_two_request_ids(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    first = client.get("/healthz").headers["x-request-id"]
    second = client.get("/healthz").headers["x-request-id"]
    assert first != second


def test_lines_are_json_and_hold_no_email_or_password(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(tmp_path)
    credentials = {"email": EMAIL, "password": PASSWORD}
    with caplog.at_level(logging.INFO, logger="app_backend"):
        client.post("/auth/register", json=credentials)
        client.post("/auth/login", json=credentials)
        client.get("/auth/me")
        client.post("/auth/logout")
    formatter = JsonFormatter()
    lines = [formatter.format(r) for r in caplog.records if r.name.startswith("app_backend")]
    assert lines
    for line in lines:
        assert "\n" not in line
        fields = json.loads(line)
        assert isinstance(fields["request-id"], str)
        assert EMAIL not in line
        assert PASSWORD not in line
