"""make cuda-graphs (P4.13, card 66): the experiment file it refuses, the helm command a variant
becomes, what it reads from a pod's log and nvidia-smi, and the order it measures in. The cluster is
a fake that logs what it is asked. No network."""

import csv
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from calibrator.calibrate import WarmCall
from calibrator.config import CalibrationError, Cell
from calibrator.cuda_graphs import (
    CELL_ROW_COLUMNS,
    START_COLUMNS,
    Spike,
    Startup,
    graph_lines,
    graphs_command,
    load_spike,
    outputs,
    run_spike,
    startup_numbers,
    vram_mib,
)
from calibrator.stats import ClockReading, Measured

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_FILE = REPO_ROOT / "experiments" / "cuda-graphs.yaml"
CONFIG_DIR = REPO_ROOT / "config"
SHA = {"real": "a" * 64, "demo": "b" * 64}
CREATED = "2026-11-22T10:00:00Z"
READY = "2026-11-22T10:02:10Z"
READY_UNIX = 1795341730.0
# Lines as vLLM v0.31.0 formats them (gpu_model_runner.py, kv_cache_utils.py), behind the prefix a
# pod's log puts on each line.
LOADED = (
    "(EngineCore pid=96) INFO 11-22 10:01:40 [gpu_model_runner.py:5355] "
    "Model loading took 0.79 GiB memory and 12.345678 seconds"
)
CAPTURED = (
    "(EngineCore pid=96) INFO 11-22 10:01:58 [gpu_model_runner.py:6855] "
    "Graph capturing finished in 9 secs, took 0.06 GiB"
)
KV = (
    "(EngineCore pid=96) INFO 11-22 10:01:52 [kv_cache_utils.py:2465] GPU KV cache size: 4,384 "
    "tokens, Maximum concurrency for 4,096 tokens per request: 1.07x"
)
DISABLED = (
    "(EngineCore pid=96) INFO 11-22 10:01:30 [vllm.py:1968] Cudagraph is disabled under eager mode"
)
GRAPH_LOG = "\n".join(["INFO starting", LOADED, KV, CAPTURED, "INFO serving"])
EAGER_LOG = "\n".join(["INFO starting", DISABLED, LOADED, KV, "INFO serving"])


def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPIKE_FILE.read_text())
    return loaded


def write_spec(tmp_path: Path, changed: dict[str, Any]) -> Path:
    path = tmp_path / "cuda-graphs.yaml"
    path.write_text(yaml.safe_dump(changed))
    return path


# The experiment file.


def test_the_repos_file_measures_real_at_one_and_two_pods_and_demo_at_one() -> None:
    spike = load_spike(SPIKE_FILE, CONFIG_DIR)
    assert spike.pods == {"real": (1, 2), "demo": (1,)}
    assert [variant.name for variant in spike.variants] == ["eager", "graphs", "graphs-nocompile"]
    assert [variant.cuda_graphs for variant in spike.variants] == [
        {"mode": "eager"},
        {"mode": "compiled", "capture_sizes": [1, 2]},
        {"mode": "decode_only", "capture_sizes": [1, 2]},
    ]
    assert spike.calibration.cells == (Cell(1, 1024, "partial"), Cell(2, 1024, "partial"))
    assert spike.calibration.profiles == {"real": "real", "demo": "demo"}
    assert spike.calibration.calls_per_cell == 20
    assert spike.calibration.completion_tokens == 64


def _variant(index: int, **change: Any) -> dict[str, Any]:
    changed = spec()
    changed["variants"][index] = changed["variants"][index] | change
    return changed


def _graphs(index: int, graphs: Any) -> dict[str, Any]:
    return _variant(index, cuda_graphs=graphs)


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({**spec(), "profiles": {}}, "profiles"),
        ({**spec(), "profiles": {"nothing": [1]}}, "no profile"),
        ({**spec(), "profiles": {"sim": [1]}}, "model.cuda_graphs"),
        ({**spec(), "profiles": {"demo": [1, 2]}}, "demo"),
        ({**spec(), "profiles": {"real": [2, 1]}}, "real"),
        ({**spec(), "variants": []}, "variants"),
        (_variant(1, name="eager"), "eager"),
        (_variant(1, name=""), "name"),
        (_variant(1, warmup=True), "warmup"),
        (_graphs(1, "compiled"), "cuda_graphs"),
        (_graphs(1, {"mode": "fast", "capture_sizes": [1]}), "fast"),
        (_graphs(0, {"mode": "eager", "capture_sizes": [1, 2]}), "capture_sizes"),
        (_graphs(1, {"mode": "compiled"}), "capture_sizes"),
        (_graphs(1, {"mode": "compiled", "capture_sizes": []}), "capture_sizes"),
        (_graphs(2, {"mode": "decode_only", "capture_sizes": [2, 1]}), "capture_sizes"),
        (_graphs(2, {"mode": "decode_only", "capture_sizes": [1], "x": 1}), "x"),
        ({**spec(), "prompt_tokens": [4096]}, "prompt_tokens"),
    ],
)
def test_a_file_the_spike_cannot_run_is_refused(
    tmp_path: Path, changed: dict[str, Any], message: str
) -> None:
    with pytest.raises(CalibrationError, match=message):
        load_spike(write_spec(tmp_path, changed), CONFIG_DIR)


# What a variant becomes, and what is read from the cluster.


def test_a_variant_empties_the_pool_with_its_graphs_and_every_other_value_as_deployed() -> None:
    command = graphs_command({"mode": "compiled", "capture_sizes": [1, 2]})
    assert command == [
        "helm",
        "upgrade",
        "vllm",
        str(REPO_ROOT / "infra" / "charts" / "vllm"),
        "--reuse-values",
        "--force-conflicts",
        "--set",
        "replicas=0",
        "--set-json",
        'model.cuda_graphs={"mode": "compiled", "capture_sizes": [1, 2]}',
    ]


def test_a_graph_pods_log_gives_its_load_capture_and_kv_cache() -> None:
    assert startup_numbers(GRAPH_LOG) == Startup(
        model_load_gib=0.79,
        model_load_s=12.3,
        graph_capture_s=9,
        graph_capture_gib=0.06,
        kv_cache_tokens=4384,
    )


def test_an_eager_pods_log_has_no_capture() -> None:
    numbers = startup_numbers(EAGER_LOG)
    assert numbers.graph_capture_s is None
    assert numbers.graph_capture_gib is None
    assert numbers.kv_cache_tokens == 4384


def test_a_log_without_the_lines_gives_nothing() -> None:
    assert startup_numbers("INFO starting\n") == Startup(None, None, None, None, None)


def test_the_lines_naming_graphs_are_kept_whatever_their_case() -> None:
    log = "\n".join(["INFO starting", DISABLED, CAPTURED, "Capturing CUDA graphs (decode, FULL)"])
    assert graph_lines(log) == [DISABLED, CAPTURED, "Capturing CUDA graphs (decode, FULL)"]


def test_nvidia_smi_gives_used_and_total_mib() -> None:
    assert vram_mib("3295, 4096\n") == (3295, 4096)


@pytest.mark.parametrize("output", ["", "3295, 4096\n1000, 8192\n", "N/A, 4096\n"])
def test_nvidia_smi_output_that_is_not_one_gpus_is_refused(output: str) -> None:
    with pytest.raises(CalibrationError, match="nvidia-smi"):
        vram_mib(output)


# The run.


class FakePods:
    def __init__(self, cluster: "FakeCluster", profile: str, pods: Sequence[str]) -> None:
        self.cluster = cluster
        self.profile = profile
        self.pods = list(pods)

    def warm(self) -> dict[str, list[WarmCall]]:
        self.cluster.log.append(f"warm {self.pods}")
        return {
            pod: [WarmCall(26000, READY_UNIX + 30.0), WarmCall(900, READY_UNIX + 31.5)]
            for pod in self.pods
        }

    def cell(self, cell: Cell) -> dict[str, list[Measured]]:
        self.cluster.log.append(f"cell {cell.concurrency}")
        calls = self.cluster.calls_per_cell
        return {
            pod: [Measured(pod, 400, 400 + 63 * 150, 64, 768, cell.concurrency)] * calls
            for pod in self.pods
        }


class FakeCluster:
    """Pods are named in the order they start. fail_at = (graph mode, pod count) whose scale is
    not Ready in time."""

    def __init__(
        self, calls_per_cell: int, fail_at: tuple[str, int] | None = None, refuse: bool = False
    ) -> None:
        self.calls_per_cell = calls_per_cell
        self.fail_at = fail_at
        self.refuse = refuse
        self.log: list[str] = []
        self.started = 0
        self.pods: list[str] = []
        self.mode = ""
        self.readings = 0

    def check_host(self) -> None:
        self.log.append("host")
        if self.refuse:
            raise CalibrationError("power plan")

    def deploy(self, profile: str) -> None:
        self.log.append(f"deploy {profile}")

    def set_graphs(self, graphs: Mapping[str, Any]) -> None:
        self.log.append(f"graphs {graphs['mode']}")
        self.mode = graphs["mode"]
        self.pods = []

    def scale(self, replicas: int) -> list[str]:
        self.log.append(f"scale {replicas}")
        if (self.mode, replicas) == self.fail_at:
            raise CalibrationError("not Ready in time")
        while len(self.pods) < replicas:
            self.started += 1
            self.pods.append(f"pod-{self.started}")
        return list(self.pods)

    def pod_json(self, pod: str) -> dict[str, Any]:
        return {
            "metadata": {"name": pod, "creationTimestamp": CREATED},
            "status": {
                "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": READY}]
            },
        }

    @contextmanager
    def reach(self, model: str, pods: Sequence[str]) -> Iterator[FakePods]:
        self.log.append(f"reach {model} {list(pods)}")
        yield FakePods(self, model, pods)

    def pod_log(self, pod: str) -> str:
        return EAGER_LOG if self.mode == "eager" else GRAPH_LOG

    def failure_lines(self) -> list[str]:
        return ["pod-9: CrashLoopBackOff", "torch.OutOfMemoryError: CUDA out of memory"]

    def vram(self) -> tuple[int, int]:
        self.log.append("vram")
        return (3295, 4096)

    def clock(self) -> ClockReading:
        """Both clocks advance 10 s a reading, so the drift is 0."""
        self.readings += 1
        return ClockReading(10.0 * self.readings, 10_000 * self.readings, 10.0 * self.readings)

    def clock_jumps(self, since: float, until: float) -> int:
        return 0


def _spike() -> Spike:
    return load_spike(SPIKE_FILE, CONFIG_DIR)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def test_each_profile_runs_every_variant_from_an_empty_pool_and_real_is_back_at_the_end(
    tmp_path: Path,
) -> None:
    spike = _spike()
    cluster = FakeCluster(spike.calibration.calls_per_cell)
    assert run_spike(spike, outputs(spike, tmp_path), cluster, SHA)
    one_pod = ["warm ['pod-{a}']", "vram", "cell 1", "cell 2"]

    def real(mode: str, a: int) -> list[str]:
        return [
            f"graphs {mode}",
            "scale 1",
            f"reach real ['pod-{a}']",
            *(line.format(a=a) for line in one_pod),
            "scale 2",
            f"reach real ['pod-{a}', 'pod-{a + 1}']",
            f"warm ['pod-{a}', 'pod-{a + 1}']",
            "vram",
            "cell 1",
            "cell 2",
        ]

    def demo(mode: str, a: int) -> list[str]:
        return [f"graphs {mode}", "scale 1", f"reach demo ['pod-{a}']"] + [
            line.format(a=a) for line in one_pod
        ]

    assert cluster.log == [
        "host",
        "deploy real",
        *real("eager", 1),
        *real("compiled", 3),
        *real("decode_only", 5),
        "deploy demo",
        *demo("eager", 7),
        *demo("compiled", 8),
        *demo("decode_only", 9),
        "deploy real",
    ]


def test_every_pod_gets_a_row_per_cell_and_every_start_its_numbers(tmp_path: Path) -> None:
    spike = _spike()
    out = outputs(spike, tmp_path)
    run_spike(spike, out, FakeCluster(spike.calibration.calls_per_cell), SHA)
    cells = _rows(out.cells)
    assert list(cells[0]) == list(CELL_ROW_COLUMNS)
    # real: 1 pod x 2 cells + 2 pods x 2 cells; demo: 1 pod x 2 cells; three variants each.
    assert len(cells) == 3 * (2 + 4) + 3 * 2
    first = cells[0]
    assert (first["profile"], first["variant"], first["pods"], first["pod"]) == (
        "real",
        "eager",
        "1",
        "pod-1",
    )
    assert first["per_token_ms_p50"] == "150.0"
    assert first["cached_tokens_p50"] == "768"
    two_pods = [row for row in cells if row["variant"] == "graphs" and row["pods"] == "2"]
    assert [row["pod"] for row in two_pods] == ["pod-3", "pod-4", "pod-3", "pod-4"]
    starts = _rows(out.starts)
    assert list(starts[0]) == list(START_COLUMNS)
    assert [(row["variant"], row["case"], row["pod"]) for row in starts[:4]] == [
        ("eager", "deployed", "pod-1"),
        ("eager", "added", "pod-2"),
        ("graphs", "deployed", "pod-3"),
        ("graphs", "added", "pod-4"),
    ]
    assert len(starts) == 3 * 2 + 3
    eager, graphs = starts[0], starts[2]
    assert eager["created_to_ready_s"] == "130.0"
    assert eager["ready_to_first_answer_s"] == "30.0"
    assert eager["first_ttft_ms"] == "26000"
    assert eager["warmup_calls"] == "2"
    assert (eager["graph_capture_s"], eager["graph_capture_gib"]) == ("", "")
    assert (graphs["graph_capture_s"], graphs["graph_capture_gib"]) == ("9", "0.06")
    assert (graphs["model_load_gib"], graphs["model_load_s"]) == ("0.79", "12.3")
    assert graphs["kv_cache_tokens"] == "4384"
    assert (graphs["vram_used_mib"], graphs["vram_total_mib"]) == ("3295", "4096")


def test_a_variant_that_does_not_fit_is_recorded_and_the_next_one_still_runs(
    tmp_path: Path,
) -> None:
    spike = _spike()
    out = outputs(spike, tmp_path)
    cluster = FakeCluster(spike.calibration.calls_per_cell, fail_at=("compiled", 2))
    assert run_spike(spike, out, cluster, SHA)
    assert cluster.log.count("graphs decode_only") == 2
    assert cluster.log[-1] == "deploy real"
    variants = json.loads(out.meta.read_text())["variants"]
    failed = variants[1]
    assert (failed["profile"], failed["variant"], failed["fits"]) == ("real", "graphs", False)
    assert failed["reason"] == "not Ready in time"
    assert failed["pod_lines"] == FakeCluster(1).failure_lines()
    assert [variant["fits"] for variant in variants] == [True, False, True, True, True, True]
    # The rows measured before it stopped stay: its one-pod cells and start.
    graphs = [row for row in _rows(out.cells) if row["variant"] == "graphs"]
    assert [(row["profile"], row["pods"]) for row in graphs] == [("real", "1")] * 2 + [
        ("demo", "1")
    ] * 2


def test_the_meta_holds_the_profiles_and_each_variants_graph_lines(tmp_path: Path) -> None:
    spike = _spike()
    out = outputs(spike, tmp_path)
    run_spike(spike, out, FakeCluster(spike.calibration.calls_per_cell), SHA)
    meta = json.loads(out.meta.read_text())
    assert meta["experiment"] == "cuda-graphs"
    assert meta["hash"] == spike.calibration.hash
    assert meta["profile_sha256"] == SHA
    assert meta["drift"] == {"share": 0.0, "uncertainty": 0.0}
    eager, graphs = meta["variants"][0], meta["variants"][1]
    assert eager == {
        "profile": "real",
        "variant": "eager",
        "cuda_graphs": {"mode": "eager"},
        "fits": True,
        "graph_lines": {"pod-1": [DISABLED], "pod-2": [DISABLED]},
    }
    assert graphs["graph_lines"] == {"pod-3": [CAPTURED], "pod-4": [CAPTURED]}


def test_the_outputs_are_named_by_the_files_hash_and_no_partial_is_left(tmp_path: Path) -> None:
    spike = _spike()
    out = outputs(spike, tmp_path)
    stem = f"cuda-graphs-{spike.calibration.hash}"
    assert (out.cells.name, out.starts.name, out.meta.name) == (
        f"{stem}-cells.csv",
        f"{stem}-starts.csv",
        f"{stem}.meta.json",
    )
    run_spike(spike, out, FakeCluster(spike.calibration.calls_per_cell), SHA)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [out.cells.name, out.starts.name, out.meta.name]
    )


def test_a_rerun_after_a_finished_run_touches_nothing(tmp_path: Path) -> None:
    spike = _spike()
    out = outputs(spike, tmp_path)
    run_spike(spike, out, FakeCluster(spike.calibration.calls_per_cell), SHA)
    again = FakeCluster(spike.calibration.calls_per_cell)
    assert not run_spike(spike, out, again, SHA)
    assert again.log == []


def test_a_host_refusal_changes_nothing(tmp_path: Path) -> None:
    spike = _spike()
    out = outputs(spike, tmp_path)
    cluster = FakeCluster(spike.calibration.calls_per_cell, refuse=True)
    with pytest.raises(CalibrationError, match="power plan"):
        run_spike(spike, out, cluster, SHA)
    assert cluster.log == ["host"]
    assert list(tmp_path.iterdir()) == []


def test_a_failure_outside_a_variant_still_puts_real_back(tmp_path: Path) -> None:
    spike = _spike()
    cluster = FakeCluster(spike.calibration.calls_per_cell)

    def broken(profile: str) -> None:
        cluster.log.append(f"deploy {profile}")
        if profile == "demo":
            raise CalibrationError("helm failed")

    cluster.deploy = broken  # type: ignore[method-assign]
    with pytest.raises(CalibrationError, match="helm failed"):
        run_spike(spike, outputs(spike, tmp_path), cluster, SHA)
    assert cluster.log[-2:] == ["deploy demo", "deploy real"]


def test_a_scale_that_starts_no_new_pod_does_not_fit(tmp_path: Path) -> None:
    spike = _spike()
    out = outputs(spike, tmp_path)
    cluster = FakeCluster(spike.calibration.calls_per_cell)
    real_scale = cluster.scale

    def stuck(replicas: int) -> list[str]:
        pods = real_scale(replicas)
        return pods[:1] if replicas == 2 else pods

    cluster.scale = stuck  # type: ignore[method-assign]
    run_spike(spike, out, cluster, SHA)
    variants = json.loads(out.meta.read_text())["variants"]
    assert [variant["fits"] for variant in variants] == [False, False, False, True, True, True]
    assert "scaling to 2 started []" in variants[0]["reason"]
