"""The drain of P4.3 as the vLLM and sim charts render it from a profile (card 59)."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTS = REPO_ROOT / "infra" / "charts"
PROFILES = ("real", "real-plain", "demo", "sim")
# What make vllm-deploy and make sim-deploy hand the charts besides the profile: the picker's
# image, which carries the waiter (infra/picker-image.sh --tag), and the vLLM chart's host paths.
IMAGE = "thesis/picker:0123456789ab"
METRICS = "--metrics=http://127.0.0.1:8000/metrics"


def profile(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / "config" / f"{name}.yaml").read_text())
    return loaded


def chart_values(name: str) -> dict[str, Any]:
    """The values make vllm-deploy or make sim-deploy renders from the profile."""
    loaded = profile(name)
    if name == "sim":
        keys = ("replicas", "model", "simulator", "monitoring", "drain")
        return {key: loaded[key] for key in keys} | {"drainImage": IMAGE}
    keys = ("replicas", "model", "pod", "monitoring", "drain")
    return {key: loaded[key] for key in keys} | {
        "drainImage": IMAGE,
        "hfCacheHostPath": "/home/user/.cache/huggingface",
        "loraHostPath": "/home/user/adapters",
    }


def chart_of(name: str) -> Path:
    return CHARTS / ("sim" if name == "sim" else "vllm")


def render(chart: Path, values: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "vllm", str(chart), "--values", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
        check=False,
    )


def pod_spec(chart: Path, values: dict[str, Any]) -> dict[str, Any]:
    done = render(chart, values)
    assert done.returncode == 0, done.stderr
    for document in yaml.safe_load_all(done.stdout):
        if document and document["kind"] == "Deployment":
            spec: dict[str, Any] = document["spec"]["template"]["spec"]
            return spec
    raise AssertionError("no Deployment rendered")


def margin(chart: Path) -> int:
    defaults: dict[str, Any] = yaml.safe_load((chart / "values.yaml").read_text())
    value: int = defaults["drainExitMarginSeconds"]
    return value


@pytest.mark.parametrize("name", PROFILES)
def test_every_profile_drains_at_least_as_long_as_a_call_may_last(name: str) -> None:
    loaded = profile(name)
    drain = loaded["drain"]
    assert drain["enabled"] is True
    assert drain["timeout_seconds"] >= loaded["agent"]["llm_timeout_seconds"]
    assert isinstance(drain["quiet_seconds"], int)
    assert 0 <= drain["quiet_seconds"] < drain["timeout_seconds"]


@pytest.mark.parametrize("name", PROFILES)
def test_a_draining_pod_runs_the_waiter_before_its_server_stops(name: str) -> None:
    chart = chart_of(name)
    drain = profile(name)["drain"]
    spec = pod_spec(chart, chart_values(name))
    [init] = spec["initContainers"]
    assert init["image"] == IMAGE
    assert init["imagePullPolicy"] == "Never"
    assert init["command"] == ["/app/drainwait", "install", "/drain/drainwait"]
    assert init["volumeMounts"] == [{"name": "drain", "mountPath": "/drain"}]
    [server] = spec["containers"]
    assert server["lifecycle"]["preStop"]["exec"]["command"] == [
        "/drain/drainwait",
        "wait",
        METRICS,
        f"--timeout={drain['timeout_seconds']}s",
        f"--quiet={drain['quiet_seconds']}s",
    ]
    assert {"name": "drain", "mountPath": "/drain", "readOnly": True} in server["volumeMounts"]
    assert {"name": "drain", "emptyDir": {}} in spec["volumes"]
    # Kubernetes counts the hook inside the grace period, so the server's exit needs the margin.
    assert spec["terminationGracePeriodSeconds"] == drain["timeout_seconds"] + margin(chart)


@pytest.mark.parametrize("name", PROFILES)
def test_with_the_drain_off_the_pod_is_as_before(name: str) -> None:
    chart = chart_of(name)
    values = chart_values(name)
    values["drain"] = values["drain"] | {"enabled": False}
    spec = pod_spec(chart, values)
    assert "initContainers" not in spec
    assert "terminationGracePeriodSeconds" not in spec
    [server] = spec["containers"]
    assert "lifecycle" not in server
    assert all(mount["name"] != "drain" for mount in server.get("volumeMounts") or [])
    assert all(volume["name"] != "drain" for volume in spec.get("volumes") or [])


@pytest.mark.parametrize("name", ["real", "sim"])
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"enabled": None}, "drain.enabled"),
        ({"enabled": "yes"}, "drain.enabled"),
        ({"timeout_seconds": None}, "drain.timeout_seconds"),
        ({"timeout_seconds": 0}, "drain.timeout_seconds"),
        ({"timeout_seconds": 120.5}, "drain.timeout_seconds"),
        ({"timeout_seconds": "120"}, "drain.timeout_seconds"),
        ({"quiet_seconds": None}, "drain.quiet_seconds"),
        ({"quiet_seconds": -1}, "drain.quiet_seconds"),
        ({"quiet_seconds": 1.5}, "drain.quiet_seconds"),
    ],
)
def test_the_chart_refuses_a_drain_it_cannot_run(
    name: str, change: dict[str, Any], message: str
) -> None:
    values = chart_values(name)
    values["drain"] = values["drain"] | change
    done = render(chart_of(name), values)
    assert done.returncode != 0
    assert message in done.stderr


@pytest.mark.parametrize("name", ["real", "sim"])
def test_the_chart_refuses_a_drain_without_the_waiters_image(name: str) -> None:
    values = chart_values(name)
    del values["drainImage"]
    done = render(chart_of(name), values)
    assert done.returncode != 0
    assert "drainImage" in done.stderr


@pytest.mark.parametrize("name", ["real", "sim"])
def test_with_the_drain_off_no_image_is_needed(name: str) -> None:
    values = chart_values(name)
    del values["drainImage"]
    values["drain"] = values["drain"] | {"enabled": False}
    assert render(chart_of(name), values).returncode == 0


def test_a_quiet_time_of_zero_is_allowed() -> None:
    values = chart_values("sim")
    values["drain"] = values["drain"] | {"quiet_seconds": 0}
    [server] = pod_spec(chart_of("sim"), values)["containers"]
    assert "--quiet=0s" in server["lifecycle"]["preStop"]["exec"]["command"]
