"""make cuda-graphs (P4.13, docs/task-cards/66-P4.13-cuda-graphs-spike.md): a measuring-only spike
on vLLM pods with CUDA graphs against today's --enforce-eager.

Per profile of the experiment file, the profile is deployed, then each variant empties the pool
with its model.cuda_graphs (helm upgrade --reuse-values, every other value as deployed) and scales
it up one pod count at a time, so every variant's first pod starts alone on an empty GPU and its
second beside it, as autoscaling adds one. After each scale the pods are warmed and the GPU's memory
read, every started pod's start timed and its log read, then the cells measured on every pod at
once, with make calibrate's calls (calibrator.calibrate). A variant whose pods are not Ready in time
or whose calls fail does not fit: that is recorded with the pods' last log lines, its rows so far
stay, and the next variant runs. Whatever happened, real is deployed again at the end.

The rows go to .partial files, renamed once the run ends; the meta is written last, so a meta
means a finished run, and a rerun with one touches nothing.

Usage: python -m calibrator.cuda_graphs [EXPERIMENT]
"""

import argparse
import csv
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from load_generator.experiment import VLLM_SELECTOR
from load_generator.record import REPO_ROOT, experiment_hash, kubectl

from calibrator.calibrate import WAIT_TIMEOUT, LiveCluster, Pods
from calibrator.config import (
    Calibration,
    CalibrationError,
    Cell,
    ColdStart,
    Host,
    Warmup,
    _cache,
    _count,
    _levels,
)
from calibrator.stats import CELL_COLUMNS, ClockReading, cell_row, cold_start_times, drift

# The vLLM chart's modes (infra/charts/vllm/templates/deployment.yaml); only eager takes no sizes.
GRAPH_MODES = ("eager", "compiled", "decode_only")
CHART = REPO_ROOT / "infra" / "charts" / "vllm"
# The profile deployed again at the end: the agent runs on it.
RESTORED_PROFILE = "real"
# Lines kept from each pod's log when a variant does not fit: vLLM's traceback of an out-of-memory
# start is about 30 lines, the last ones naming what failed.
FAILURE_TAIL_LINES = 40
CELL_ROW_COLUMNS = ("profile", "variant", "pods", *CELL_COLUMNS)
START_COLUMNS = (
    "profile",
    "variant",
    # deployed: the variant's first pod, alone on the GPU; added: one more beside it.
    "case",
    "pod",
    # From Kubernetes' timestamps, whole seconds of the wall clock (calibrator.stats).
    "created_to_ready_s",
    "ready_to_first_answer_s",
    "first_ttft_ms",
    "warmup_calls",
    # From the pod's log; empty when vLLM did not print the line (no capture when eager).
    "model_load_gib",
    "model_load_s",
    "graph_capture_s",
    "graph_capture_gib",
    "kv_cache_tokens",
    # nvidia-smi once every pod is warm: the whole GPU, the Windows desktop included.
    "vram_used_mib",
    "vram_total_mib",
)
# vLLM v0.31.0's own lines (v1/worker/gpu_model_runner.py, v1/core/kv_cache_utils.py).
MODEL_LOADING = re.compile(r"Model loading took ([\d.]+) GiB memory and ([\d.]+) seconds")
GRAPH_CAPTURE = re.compile(r"Graph capturing finished in (\d+) secs, took ([\d.]+) GiB")
KV_CACHE = re.compile(r"GPU KV cache size: ([\d,]+) tokens")
# What vLLM says about graphs at startup, kept whole in the meta: the mode it settled on, a mode it
# overrode, the capture.
NAMES_GRAPHS = re.compile(r"cudagraph|cuda graph|graph captur", re.IGNORECASE)
VRAM_LINE = re.compile(r"(\d+), (\d+)")


@dataclass(frozen=True)
class Variant:
    name: str
    cuda_graphs: Mapping[str, Any]


@dataclass(frozen=True)
class Spike:
    # make calibrate's settings for the calls: cells, warm-up, host, seed. Its profiles map each
    # profile's name to itself, as the calls name the profile they measure.
    calibration: Calibration
    # The pod counts each profile is measured at, in order.
    pods: Mapping[str, tuple[int, ...]]
    variants: tuple[Variant, ...]


@dataclass(frozen=True)
class Outputs:
    cells: Path
    starts: Path
    meta: Path


@dataclass(frozen=True)
class Startup:
    model_load_gib: float | None
    model_load_s: float | None
    graph_capture_s: int | None
    graph_capture_gib: float | None
    kv_cache_tokens: int | None


# The experiment file.


def _vllm_profile(config_dir: Path, name: str) -> dict[str, Any]:
    path = config_dir / f"{name}.yaml"
    if not path.exists():
        raise CalibrationError(f"profiles: no profile {path}")
    profile: dict[str, Any] = yaml.safe_load(path.read_text())
    if "cuda_graphs" not in profile["model"]:
        raise CalibrationError(f"profiles: {name} has no model.cuda_graphs, so no vLLM pods")
    return profile


def _graphs(name: str, graphs: Any) -> dict[str, Any]:
    if not isinstance(graphs, dict) or graphs.get("mode") not in GRAPH_MODES:
        raise CalibrationError(
            f"variant {name}: cuda_graphs.mode must be one of {GRAPH_MODES}: {graphs!r}"
        )
    keys = {"mode"} if graphs["mode"] == "eager" else {"mode", "capture_sizes"}
    if set(graphs) != keys:
        raise CalibrationError(f"variant {name}: cuda_graphs must have {sorted(keys)}: {graphs!r}")
    if graphs["mode"] == "eager":
        return {"mode": "eager"}
    return {"mode": graphs["mode"], "capture_sizes": list(_levels(graphs, "capture_sizes"))}


def _variants(spec: Any) -> tuple[Variant, ...]:
    if not isinstance(spec, list) or not spec:
        raise CalibrationError(f"variants must be a list of at least one: {spec!r}")
    found: list[Variant] = []
    for variant in spec:
        if not isinstance(variant, dict) or set(variant) != {"name", "cuda_graphs"}:
            raise CalibrationError(f"a variant has exactly name and cuda_graphs: {variant!r}")
        name = variant["name"]
        if not isinstance(name, str) or not name:
            raise CalibrationError(f"a variant's name must be a word: {name!r}")
        if name in {made.name for made in found}:
            raise CalibrationError(f"variant {name} is named twice")
        found.append(Variant(name, _graphs(name, variant["cuda_graphs"])))
    return tuple(found)


def load_spike(path: Path, config_dir: Path) -> Spike:
    """config_dir holds the profiles the file names. A missing key raises KeyError."""
    spec = yaml.safe_load(path.read_text())
    named = spec["profiles"]
    if not isinstance(named, dict) or not named:
        raise CalibrationError(f"profiles must name at least one profile: {named!r}")
    profiles = {name: _vllm_profile(config_dir, name) for name in named}
    pods = {name: _levels(named, name, most=profiles[name]["replicas"]) for name in named}
    max_model_len = min(profile["model"]["max_model_len"] for profile in profiles.values())
    completion = _count(spec, "completion_tokens", least=2)
    longest = max_model_len - completion
    modes, share = _cache(spec["cache"])
    warmup = spec["warmup"]
    warmup_prompt = _count(warmup, "warmup.prompt_tokens")
    if warmup_prompt > longest:
        raise CalibrationError(
            f"warmup.prompt_tokens {warmup_prompt} + completion_tokens {completion} is over "
            f"max_model_len {max_model_len}"
        )
    host = spec["host"]
    if not host["sleep_settings"]:
        raise CalibrationError("host.sleep_settings must name at least one setting")
    variants = _variants(spec["variants"])
    calibration = Calibration(
        name=path.stem,
        hash=experiment_hash(path),
        profiles={name: name for name in named},
        replicas=min(profile["replicas"] for profile in profiles.values()),
        pods=tuple(sorted({count for counts in pods.values() for count in counts})),
        cells=tuple(
            Cell(concurrency, prompt_tokens, cache)
            for concurrency in _levels(spec, "concurrency")
            for prompt_tokens in _levels(spec, "prompt_tokens", most=longest)
            for cache in modes
        ),
        completion_tokens=completion,
        calls_per_cell=_count(spec, "calls_per_cell"),
        partial_share=share,
        warmup=Warmup(
            prompt_tokens=warmup_prompt,
            settled_ttft_ms=_count(warmup, "warmup.settled_ttft_ms"),
            max_calls=_count(warmup, "warmup.max_calls"),
        ),
        # Not make calibrate's restarts: the spike times every pod it starts.
        cold_start=ColdStart(alone=0, both=0),
        host=Host(power_scheme=host["power_scheme"], sleep_settings=tuple(host["sleep_settings"])),
        seed=_count(spec, "seed", least=0),
        call_timeout_seconds=_count(spec, "call_timeout_seconds"),
    )
    return Spike(calibration, pods, variants)


def outputs(spike: Spike, directory: Path) -> Outputs:
    stem = f"{spike.calibration.name}-{spike.calibration.hash}"
    return Outputs(
        directory / f"{stem}-cells.csv",
        directory / f"{stem}-starts.csv",
        directory / f"{stem}.meta.json",
    )


# What a variant becomes, and what is read from the cluster.


def graphs_command(graphs: Mapping[str, Any]) -> list[str]:
    """The pool's release with no pods and the variant's graphs, every other value as deployed.
    --force-conflicts: kubectl scale owns .spec.replicas afterwards, as in make vllm-deploy."""
    return [
        "helm",
        "upgrade",
        "vllm",
        str(CHART),
        "--reuse-values",
        "--force-conflicts",
        "--set",
        "replicas=0",
        "--set-json",
        f"model.cuda_graphs={json.dumps(dict(graphs))}",
    ]


def startup_numbers(log: str) -> Startup:
    """From the log of the pod's running container, which starts vLLM once."""
    loading = MODEL_LOADING.search(log)
    capture = GRAPH_CAPTURE.search(log)
    kv = KV_CACHE.search(log)
    return Startup(
        model_load_gib=float(loading.group(1)) if loading else None,
        # vLLM prints microseconds; whole tenths are finer than two starts differ by.
        model_load_s=round(float(loading.group(2)), 1) if loading else None,
        graph_capture_s=int(capture.group(1)) if capture else None,
        graph_capture_gib=float(capture.group(2)) if capture else None,
        kv_cache_tokens=int(kv.group(1).replace(",", "")) if kv else None,
    )


def graph_lines(log: str) -> list[str]:
    return [line for line in log.splitlines() if NAMES_GRAPHS.search(line)]


def vram_mib(output: str) -> tuple[int, int]:
    """Used and total MiB from nvidia-smi --query-gpu=memory.used,memory.total
    --format=csv,noheader,nounits, for the one GPU there is."""
    lines = output.splitlines()
    found = VRAM_LINE.fullmatch(lines[0]) if len(lines) == 1 else None
    if found is None:
        raise CalibrationError(f"nvidia-smi did not give one GPU's memory: {output!r}")
    return int(found.group(1)), int(found.group(2))


# The run.


class SpikeCluster(Protocol):
    """What the spike asks of the cluster; LiveSpikeCluster is the real one."""

    def check_host(self) -> None: ...

    def deploy(self, profile: str) -> None: ...

    # No pods left, the release holding the graphs.
    def set_graphs(self, graphs: Mapping[str, Any]) -> None: ...

    def scale(self, replicas: int) -> list[str]: ...

    def pod_json(self, pod: str) -> dict[str, Any]: ...

    def reach(self, model: str, pods: Sequence[str]) -> AbstractContextManager[Pods]: ...

    def pod_log(self, pod: str) -> str: ...

    # Each vLLM pod's state and its log's last lines, for a variant that did not fit.
    def failure_lines(self) -> list[str]: ...

    def vram(self) -> tuple[int, int]: ...

    def clock(self) -> ClockReading: ...

    def clock_jumps(self, since: float, until: float) -> int: ...


Keep = Callable[[Mapping[str, Any]], None]


def _start_row(
    profile: str,
    variant: str,
    case: str,
    pod: str,
    times: tuple[float, float],
    warmup_ttft_ms: Sequence[int],
    startup: Startup,
    vram: tuple[int, int],
) -> dict[str, Any]:
    return {
        "profile": profile,
        "variant": variant,
        "case": case,
        "pod": pod,
        "created_to_ready_s": times[0],
        "ready_to_first_answer_s": round(times[1], 1),
        "first_ttft_ms": warmup_ttft_ms[0],
        "warmup_calls": len(warmup_ttft_ms),
        "model_load_gib": startup.model_load_gib,
        "model_load_s": startup.model_load_s,
        "graph_capture_s": startup.graph_capture_s,
        "graph_capture_gib": startup.graph_capture_gib,
        "kv_cache_tokens": startup.kv_cache_tokens,
        "vram_used_mib": vram[0],
        "vram_total_mib": vram[1],
    }


def _measure_variant(
    spike: Spike,
    profile: str,
    variant: Variant,
    cluster: SpikeCluster,
    keep_cell: Keep,
    keep_start: Keep,
    lines: dict[str, list[str]],
) -> None:
    """Every pod count of the profile, from an empty pool; lines gets each started pod's graph
    lines."""
    cluster.set_graphs(variant.cuda_graphs)
    pods: list[str] = []
    for count in spike.pods[profile]:
        before = pods
        pods = cluster.scale(count)
        started = [pod for pod in pods if pod not in before]
        if len(started) != count - len(before):
            raise CalibrationError(f"scaling to {count} started {started} beside {before}")
        case = "added" if before else "deployed"
        with cluster.reach(profile, pods) as reached:
            warm = reached.warm()
            vram = cluster.vram()
            for pod in started:
                log = cluster.pod_log(pod)
                lines[pod] = graph_lines(log)
                times = cold_start_times(cluster.pod_json(pod), warm[pod][0].ended_unix)
                ttfts = [call.ttft_ms for call in warm[pod]]
                keep_start(
                    _start_row(
                        profile, variant.name, case, pod, times, ttfts, startup_numbers(log), vram
                    )
                )
            for cell in spike.calibration.cells:
                since = time.time()
                measured = reached.cell(cell)
                jumps = cluster.clock_jumps(since, time.time())
                for pod in pods:
                    row = cell_row(pod, cell, measured[pod], jumps)
                    keep_cell({"profile": profile, "variant": variant.name, "pods": count, **row})
        print(f"cuda-graphs: {profile} {variant.name} at {count} pods measured", flush=True)


def _run_variant(
    spike: Spike,
    profile: str,
    variant: Variant,
    cluster: SpikeCluster,
    keep_cell: Keep,
    keep_start: Keep,
) -> dict[str, Any]:
    """The variant's entry in the meta. A pod that is not Ready in time fails kubectl's wait
    (CalledProcessError); a call that fails, or a pod still slow after the warm-up, raises
    CalibrationError: either way the variant does not fit."""
    lines: dict[str, list[str]] = {}
    outcome: dict[str, Any] = {
        "profile": profile,
        "variant": variant.name,
        "cuda_graphs": dict(variant.cuda_graphs),
    }
    try:
        _measure_variant(spike, profile, variant, cluster, keep_cell, keep_start, lines)
    except (CalibrationError, subprocess.CalledProcessError) as error:
        print(f"cuda-graphs: {profile} {variant.name} does not fit: {error}", flush=True)
        return outcome | {
            "fits": False,
            "reason": str(error),
            "pod_lines": cluster.failure_lines(),
            "graph_lines": lines,
        }
    return outcome | {"fits": True, "graph_lines": lines}


def run_spike(
    spike: Spike, out: Outputs, cluster: SpikeCluster, profile_sha256: Mapping[str, str]
) -> bool:
    """Measures every profile and variant; False if the run's meta exists, so it was done."""
    if out.meta.exists():
        print(f"cuda-graphs: {out.meta} exists, the run is finished")
        return False
    cluster.check_host()
    started = cluster.clock()
    started_unix = time.time()
    partial = {path: path.with_name(f"{path.name}.partial") for path in (out.cells, out.starts)}
    variants: list[dict[str, Any]] = []
    with (
        partial[out.cells].open("w", newline="") as cells_file,
        partial[out.starts].open("w", newline="") as starts_file,
    ):
        cells = csv.DictWriter(cells_file, fieldnames=CELL_ROW_COLUMNS)
        starts = csv.DictWriter(starts_file, fieldnames=START_COLUMNS)
        cells.writeheader()
        starts.writeheader()

        def keep_cell(row: Mapping[str, Any]) -> None:
            cells.writerow(row)
            cells_file.flush()

        def keep_start(row: Mapping[str, Any]) -> None:
            starts.writerow(row)
            starts_file.flush()

        try:
            for profile in spike.pods:
                cluster.deploy(profile)
                for variant in spike.variants:
                    variants.append(
                        _run_variant(spike, profile, variant, cluster, keep_cell, keep_start)
                    )
        finally:
            cluster.deploy(RESTORED_PROFILE)
    measured = drift(started, cluster.clock())
    for path, written in partial.items():
        written.rename(path)
    meta = {
        "experiment": spike.calibration.name,
        "hash": spike.calibration.hash,
        # The hash covers the experiment file only; the pods' other flags come from the profiles.
        "profile_sha256": dict(profile_sha256),
        "started_unix": round(started_unix),
        "ended_unix": round(time.time()),
        # Not corrected for, as in make calibrate: durations may read about this share off.
        "drift": {"share": measured.share, "uncertainty": measured.uncertainty},
        "variants": variants,
    }
    out.meta.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"cuda-graphs: wrote {out.cells}, {out.starts}, {out.meta}", flush=True)
    return True


class LiveSpikeCluster(LiveCluster):
    """make calibrate's cluster, with the variant's graphs, the pods' logs and the GPU's memory."""

    def set_graphs(self, graphs: Mapping[str, Any]) -> None:
        """The release's pods are deleted (Recreate) and waited for, since they hold the GPU."""
        subprocess.run(graphs_command(graphs), cwd=REPO_ROOT, check=True)
        pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
        for pod in pods["items"]:
            name = pod["metadata"]["name"]
            kubectl("wait", f"pod/{name}", "--for=delete", f"--timeout={WAIT_TIMEOUT}")

    def pod_log(self, pod: str) -> str:
        return kubectl("logs", f"pod/{pod}", "-c", "vllm")

    def failure_lines(self) -> list[str]:
        """kubectl's own error stands in for a log it cannot give (a container never started)."""
        pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
        lines: list[str] = []
        for pod in pods["items"]:
            name = pod["metadata"]["name"]
            statuses = pod["status"].get("containerStatuses", [])
            lines.append(f"{name}: {json.dumps([status['state'] for status in statuses])}")
            done = subprocess.run(
                ["kubectl", "logs", f"pod/{name}", "-c", "vllm", f"--tail={FAILURE_TAIL_LINES}"],
                capture_output=True,
                text=True,
                check=False,
            )
            lines.extend((done.stdout or done.stderr).splitlines())
        return lines

    def vram(self) -> tuple[int, int]:
        return vram_mib(
            subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="P4.13: vLLM pods with and without CUDA graphs.")
    parser.add_argument(
        "experiment", type=Path, nargs="?", default=REPO_ROOT / "experiments" / "cuda-graphs.yaml"
    )
    args = parser.parse_args()

    config_dir = REPO_ROOT / "config"
    spike = load_spike(args.experiment, config_dir)
    paths = {name: config_dir / f"{name}.yaml" for name in spike.pods}
    profiles = {name: yaml.safe_load(path.read_text()) for name, path in paths.items()}
    sha256 = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    out = outputs(spike, REPO_ROOT / "experiments" / "results")
    run_spike(spike, out, LiveSpikeCluster(spike.calibration, profiles), sha256)


if __name__ == "__main__":
    main()
