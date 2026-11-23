"""model.compile_cache as the vLLM chart renders it (P4.13b, card 67): on, the pod keeps what
torch.compile makes in a host folder, so a later pod loads it instead of compiling again; off, the
pod is as before."""

from typing import Any

import pytest
from test_drain_chart import CHARTS, chart_values, pod_spec, profile, render

CHART = CHARTS / "vllm"
VLLM_PROFILES = ("real", "real-plain", "demo")
MOUNT = {"name": "compile-cache", "mountPath": "/vllm-cache"}
ENV = {"name": "VLLM_CACHE_ROOT", "value": "/vllm-cache"}


def spec_with(name: str, cache: Any) -> dict[str, Any]:
    values = chart_values(name)
    values["model"] = values["model"] | {"compile_cache": cache}
    return pod_spec(CHART, values)


@pytest.mark.parametrize("name", VLLM_PROFILES)
def test_every_vllm_profile_keeps_its_compile_cache(name: str) -> None:
    assert profile(name)["model"]["compile_cache"] is True


@pytest.mark.parametrize("name", VLLM_PROFILES)
def test_on_the_pod_writes_vllms_cache_to_the_host_folder(name: str) -> None:
    spec = pod_spec(CHART, chart_values(name))
    [server] = spec["containers"]
    assert MOUNT in server["volumeMounts"]
    assert ENV in server["env"]
    host_path = {"path": "/home/user/.cache/thesis/vllm-compile", "type": "Directory"}
    assert {"name": "compile-cache", "hostPath": host_path} in spec["volumes"]


@pytest.mark.parametrize("name", VLLM_PROFILES)
def test_off_the_pod_has_no_cache_mount_and_needs_no_folder(name: str) -> None:
    values = chart_values(name)
    values["model"] = values["model"] | {"compile_cache": False}
    del values["compileCacheHostPath"]
    spec = pod_spec(CHART, values)
    [server] = spec["containers"]
    assert MOUNT not in server["volumeMounts"]
    assert all(variable["name"] != "VLLM_CACHE_ROOT" for variable in server["env"])
    assert all(volume["name"] != "compile-cache" for volume in spec["volumes"])


def test_on_and_off_differ_only_in_the_cache() -> None:
    on, off = spec_with("real", True), spec_with("real", False)
    [server_on], [server_off] = on["containers"], off["containers"]
    assert server_on["args"] == server_off["args"]
    assert [m for m in server_on["volumeMounts"] if m != MOUNT] == server_off["volumeMounts"]
    assert [e for e in server_on["env"] if e != ENV] == server_off["env"]
    assert [v for v in on["volumes"] if v["name"] != "compile-cache"] == off["volumes"]


@pytest.mark.parametrize("cache", [None, "yes", 1])
def test_the_chart_refuses_a_cache_it_cannot_read(cache: Any) -> None:
    values = chart_values("real")
    values["model"] = values["model"] | {"compile_cache": cache}
    done = render(CHART, values)
    assert done.returncode != 0
    assert "model.compile_cache" in done.stderr


def test_the_chart_refuses_a_profile_without_the_key() -> None:
    values = chart_values("real")
    del values["model"]["compile_cache"]
    done = render(CHART, values)
    assert done.returncode != 0
    assert "model.compile_cache" in done.stderr


def test_on_the_chart_refuses_a_missing_host_folder() -> None:
    values = chart_values("real")
    del values["compileCacheHostPath"]
    done = render(CHART, values)
    assert done.returncode != 0
    assert "compileCacheHostPath" in done.stderr
