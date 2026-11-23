"""model.cuda_graphs as the vLLM chart renders it (P4.13, card 66): eager is --enforce-eager, and
the two graph modes change that one flag and nothing else. The profiles run compiled since P4.13b
(card 67)."""

from typing import Any

import pytest
from test_drain_chart import CHARTS, chart_values, pod_spec, profile, render

CHART = CHARTS / "vllm"
VLLM_PROFILES = ("real", "real-plain", "demo")
SIZES = ["--cudagraph-capture-sizes", "1", "2"]
EAGER = {"mode": "eager"}
# What real, real-plain and demo run since P4.13b (card 67).
ADOPTED = {"mode": "compiled", "capture_sizes": [1, 2, 4, 8]}
DECODE_ONLY = '--compilation-config={"mode":0,"cudagraph_mode":"FULL_DECODE_ONLY"}'


def args_with(name: str, graphs: dict[str, Any] | None) -> list[str]:
    """The vLLM container's args from the profile, with model.cuda_graphs replaced unless None.
    Eager's args are P4.13's, the same as before the key existed (card 66)."""
    values = chart_values(name)
    if graphs is not None:
        values["model"] = values["model"] | {"cuda_graphs": graphs}
    [server] = pod_spec(CHART, values)["containers"]
    args: list[str] = server["args"]
    return args


def with_in_place_of_eager(args: list[str], flags: list[str]) -> list[str]:
    at = args.index("--enforce-eager")
    return args[:at] + flags + args[at + 1 :]


@pytest.mark.parametrize("name", VLLM_PROFILES)
def test_eager_renders_enforce_eager_and_no_graph_flags(name: str) -> None:
    args = args_with(name, EAGER)
    assert args.count("--enforce-eager") == 1
    assert args[args.index("--enforce-eager") - 1] == "--enable-prefix-caching"
    assert not any(arg.startswith(("--cudagraph", "--compilation-config")) for arg in args)


@pytest.mark.parametrize("name", VLLM_PROFILES)
def test_every_vllm_profile_runs_compiled_graphs_up_to_8_in_flight(name: str) -> None:
    assert profile(name)["model"]["cuda_graphs"] == ADOPTED
    expected = ["--cudagraph-capture-sizes", "1", "2", "4", "8"]
    assert args_with(name, None) == with_in_place_of_eager(args_with(name, EAGER), expected)


@pytest.mark.parametrize("name", VLLM_PROFILES)
def test_compiled_captures_graphs_at_the_given_sizes_in_place_of_eager(name: str) -> None:
    eager = args_with(name, EAGER)
    compiled = args_with(name, {"mode": "compiled", "capture_sizes": [1, 2]})
    assert compiled == with_in_place_of_eager(eager, SIZES)


@pytest.mark.parametrize("name", VLLM_PROFILES)
def test_decode_only_also_turns_torch_compile_off(name: str) -> None:
    eager = args_with(name, EAGER)
    decode_only = args_with(name, {"mode": "decode_only", "capture_sizes": [1, 2]})
    assert decode_only == with_in_place_of_eager(eager, [*SIZES, DECODE_ONLY])


def test_capture_sizes_are_rendered_as_given() -> None:
    args = args_with("real", {"mode": "compiled", "capture_sizes": [1, 2, 4]})
    at = args.index("--cudagraph-capture-sizes")
    assert args[at + 1 : at + 4] == ["1", "2", "4"]


def test_eager_ignores_capture_sizes_left_over_from_an_earlier_value() -> None:
    """helm upgrade --reuse-values merges maps, so a release that ran a graph mode keeps its
    capture_sizes when set back to eager."""
    assert args_with("real", {"mode": "eager", "capture_sizes": [1, 2]}) == args_with("real", EAGER)


@pytest.mark.parametrize(
    "graphs",
    [
        {},
        {"mode": None},
        {"mode": "fast"},
        {"mode": "compiled"},
        {"mode": "compiled", "capture_sizes": []},
        {"mode": "compiled", "capture_sizes": "1 2"},
        {"mode": "compiled", "capture_sizes": [2, 1]},
        {"mode": "compiled", "capture_sizes": [1, 1]},
        {"mode": "compiled", "capture_sizes": [0, 1]},
        {"mode": "compiled", "capture_sizes": [1.5]},
        {"mode": "decode_only", "capture_sizes": ["1"]},
    ],
)
def test_the_chart_refuses_graphs_it_cannot_run(graphs: dict[str, Any]) -> None:
    values = chart_values("real")
    values["model"] = values["model"] | {"cuda_graphs": graphs}
    done = render(CHART, values)
    assert done.returncode != 0
    assert "model.cuda_graphs" in done.stderr


def test_the_chart_refuses_a_profile_without_the_key() -> None:
    values = chart_values("real")
    values["model"] = {key: value for key, value in values["model"].items() if key != "cuda_graphs"}
    done = render(CHART, values)
    assert done.returncode != 0
    assert "model.cuda_graphs" in done.stderr
