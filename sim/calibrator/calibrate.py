"""make calibrate (P2.8a): streaming calls straight to real vLLM pods, as the table P2.8b sets
config/sim.yaml from. docs/task-cards/31-P2.8-calibration.md has what was decided with the user.

Per model, the profile is deployed, then each pod count's cells and the cold starts are measured,
each into its own file (calibrator.config.outputs). A file is written under .partial, a row per
cell as it ends, and renamed once whole, so a rerun skips finished files and redoes the rest from
their first cell. Every pod is reached through its own kubectl port-forward, so a call's pod is
known; each reply must name that pod. A call with no full reply stops the run: a table with a gap
would not say what it seems to. Whatever happened, real is deployed again at the end, as the agent
expects it, unless the host check refused before anything changed.

Usage: python -m calibrator.calibrate [EXPERIMENT]
"""

import argparse
import asyncio
import csv
import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, AsyncExitStack, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

import httpx
import yaml
from load_generator.experiment import VLLM_SELECTOR, check_journal_readable, count_clock_jumps
from load_generator.plan import chat_request, served_model, tokenize_request
from load_generator.record import REPO_ROOT, kubectl
from load_generator.replay import (
    NO_CONNECTION_LIMIT,
    TOKENIZE_PATH,
    VLLM_PORT,
    ReplyError,
    stream_reply,
)

from calibrator.config import (
    MODELS,
    Calibration,
    CalibrationError,
    Cell,
    Output,
    Warmup,
    load_calibration,
    outputs,
    pending,
)
from calibrator.prompts import CallPrompt, call_prompt, prompt_words, warmup_prompt
from calibrator.stats import (
    CELL_COLUMNS,
    COLD_START_COLUMNS,
    ClockReading,
    Measured,
    cell_row,
    check_host,
    cold_start_row,
    cold_start_times,
    drift,
    settled,
)

VLLM_DEPLOYMENT = "deployment/vllm"
# The chart make vllm-deploy installs; make sim-deploy's (sim) has no GPU behind it.
REAL_CHART = "vllm"
# Both pods load the model at once on one GPU and disk, as VLLM_DEPLOY_TIMEOUT in the Makefile.
WAIT_TIMEOUT = "900s"
FORWARDING = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+) -> ")
# A primer only has to leave its prompt in the prefix cache.
PRIMER_TOKENS = 1
# The model whose profile is deployed again at the end: the agent runs on the one with the adapter.
RESTORED_MODEL = "lora"


@dataclass(frozen=True)
class Answer:
    """A full reply from the pod that was called, its prompt the cell's length."""

    ttft_ms: int
    duration_ms: int
    completion_tokens: int
    cached_tokens: int


@dataclass(frozen=True)
class WarmCall:
    ttft_ms: int
    # Wall clock, to set against Kubernetes' Ready time.
    ended_unix: float


class Pods(Protocol):
    """The pods of one file, reached and checked."""

    def warm(self) -> dict[str, list[WarmCall]]: ...

    def cell(self, cell: Cell) -> dict[str, list[Measured]]: ...


class Cluster(Protocol):
    """What a calibration asks of the cluster; LiveCluster is the real one."""

    def check_host(self) -> None: ...

    def deploy(self, profile: str) -> None: ...

    # The Ready pods once the Deployment has replicas pods and no other.
    def scale(self, replicas: int) -> list[str]: ...

    # Every pod deleted and started again at once; the new Ready pods.
    def restart(self) -> list[str]: ...

    def pod_json(self, pod: str) -> dict[str, Any]: ...

    def reach(self, model: str, pods: Sequence[str]) -> AbstractContextManager[Pods]: ...

    def clock(self) -> ClockReading: ...

    def clock_jumps(self, since: float, until: float) -> int: ...


Keep = Callable[[Mapping[str, Any]], None]


def _measure_cells(
    calibration: Calibration, out: Output, cluster: Cluster, keep: Keep
) -> dict[str, Any]:
    assert out.pods is not None
    pods = cluster.scale(out.pods)
    with cluster.reach(out.model, pods) as reached:
        warm = reached.warm()
        for number, cell in enumerate(calibration.cells, start=1):
            since = time.time()
            measured = reached.cell(cell)
            jumps = cluster.clock_jumps(since, time.time())
            for pod in pods:
                keep(cell_row(pod, cell, measured[pod], jumps))
            print(f"calibrate: {out.path.name} cell {number}/{len(calibration.cells)}", flush=True)
    return {
        "pods": pods,
        "warmup_ttft_ms": {pod: [call.ttft_ms for call in calls] for pod, calls in warm.items()},
    }


def _time_starts(
    cluster: Cluster, model: str, restart: int, case: str, pods: Sequence[str], keep: Keep
) -> None:
    """Rows for pods that just started. Ready to first answer runs to the end of the first warm-up
    reply, so it also holds kubectl's wait, the port-forward and reach's two small calls."""
    with cluster.reach(model, pods) as reached:
        warm = reached.warm()
    for pod in pods:
        times = cold_start_times(cluster.pod_json(pod), warm[pod][0].ended_unix)
        keep(cold_start_row(restart, case, pod, times, [call.ttft_ms for call in warm[pod]]))
    print(f"calibrate: {model} cold start {case} {restart}: {list(pods)}", flush=True)


def _measure_cold_starts(
    calibration: Calibration, out: Output, cluster: Cluster, keep: Keep
) -> dict[str, Any]:
    for restart in range(1, calibration.cold_start.alone + 1):
        running = cluster.scale(calibration.replicas - 1)
        started = [pod for pod in cluster.scale(calibration.replicas) if pod not in running]
        if len(started) != 1:
            raise CalibrationError(
                f"scaling to {calibration.replicas} added {len(started)} pods, not 1"
            )
        _time_starts(cluster, out.model, restart, "alone", started, keep)
    for restart in range(1, calibration.cold_start.both + 1):
        _time_starts(cluster, out.model, restart, "both", cluster.restart(), keep)
    return {}


def _write(
    calibration: Calibration,
    out: Output,
    cluster: Cluster,
    profile_sha256: str,
) -> None:
    """The file under .partial, a row as each one is measured, then its meta, then the rename."""
    columns, measure = (
        (CELL_COLUMNS, _measure_cells)
        if out.kind == "cells"
        else (COLD_START_COLUMNS, _measure_cold_starts)
    )
    partial = out.path.with_name(f"{out.path.name}.partial")
    started = cluster.clock()
    started_unix = time.time()
    with partial.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()

        def keep(row: Mapping[str, Any]) -> None:
            writer.writerow(row)
            file.flush()

        details = measure(calibration, out, cluster, keep)
    measured = drift(started, cluster.clock())
    meta = {
        "calibration": calibration.name,
        "hash": calibration.hash,
        "model": out.model,
        "profile": calibration.profiles[out.model],
        # The hash covers the calibration file only, and the pods' flags come from the profile.
        "profile_sha256": profile_sha256,
        "started_unix": round(started_unix),
        "ended_unix": round(time.time()),
        # Not corrected for: P2.5's drift was about -2%, so durations here may read that much low.
        "drift": {"share": measured.share, "uncertainty": measured.uncertainty},
        **details,
    }
    out.path.with_name(f"{out.path.stem}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    partial.rename(out.path)
    print(f"calibrate: wrote {out.path}", flush=True)


def run_calibration(
    calibration: Calibration,
    planned: Sequence[Output],
    cluster: Cluster,
    profile_sha256: Mapping[str, str],
) -> list[Output]:
    """Writes the files a run still has to write, in planned order; returns them."""
    left = pending(planned)
    if not left:
        print(f"calibrate: every file is finished ({len(planned)})")
        return []
    cluster.check_host()
    try:
        for model in MODELS:
            mine = [out for out in left if out.model == model]
            if mine:
                cluster.deploy(calibration.profiles[model])
            for out in mine:
                _write(calibration, out, cluster, profile_sha256[model])
    finally:
        cluster.deploy(calibration.profiles[RESTORED_MODEL])
    return left


# The calls.


async def closed_loop[T](
    call: Callable[[int], Awaitable[T]], concurrency: int, calls: int
) -> list[tuple[T, int]]:
    """Calls 0..calls-1 from concurrency streams, each sending its next call as its last one ends.
    Each result comes with the streams still at work when it ended, its own included."""
    taken = 0
    working = concurrency
    results: list[tuple[T, int] | None] = [None] * calls

    async def stream() -> None:
        nonlocal taken, working
        while taken < calls:
            index = taken
            taken += 1
            result = await call(index)
            results[index] = (result, working)
        working -= 1

    async with asyncio.TaskGroup() as group:
        for _ in range(concurrency):
            group.create_task(stream())
    return [result for result in results if result is not None]


async def warm_pod(call: Callable[[int], Awaitable[WarmCall]], warmup: Warmup) -> list[WarmCall]:
    calls: list[WarmCall] = []
    while not settled([made.ttft_ms for made in calls], warmup):
        calls.append(await call(len(calls)))
    return calls


async def measured_call(
    client: httpx.AsyncClient,
    model: str,
    pod: str,
    prompt: CallPrompt,
    completion_tokens: int,
    prompt_tokens: int,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> Answer:
    """The primer, if any, then the call. The deadline covers both: a read timeout on a stream
    restarts with each chunk. The reply must come from pod and have a measured length."""
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(timeout_seconds):
            if prompt.primer is not None:
                primer = chat_request(model, prompt.primer, PRIMER_TOKENS)
                await stream_reply(client, primer, headers, loop.time())
            body = chat_request(model, prompt.prompt, completion_tokens)
            reply = await stream_reply(client, body, headers, loop.time())
    except (httpx.HTTPError, ReplyError, TimeoutError) as error:
        raise CalibrationError(f"{pod}: no full reply: {error!r}") from error
    if reply.pod != pod:
        raise CalibrationError(f"a call to {pod} was answered by {reply.pod}")
    if reply.ttft_ms is None or reply.cached_tokens is None:
        raise CalibrationError(f"{pod}: a reply without its first token or cached_tokens")
    if reply.prompt_tokens != prompt_tokens:
        raise CalibrationError(
            f"{pod}: a prompt of {reply.prompt_tokens} tokens on the server, the cell has "
            f"{prompt_tokens}"
        )
    return Answer(reply.ttft_ms, reply.duration_ms, reply.completion_tokens, reply.cached_tokens)


def _headers(calibration: Calibration, model: str, step: int) -> dict[str, str]:
    """The D1 headers: one run per model, a step per call of a cell."""
    return {
        "x-run-id": f"calibrate-{calibration.hash}-{model}",
        "x-step": str(step + 1),
        "x-expected-steps": str(calibration.calls_per_cell),
    }


class LivePods:
    def __init__(
        self,
        calibration: Calibration,
        model: str,
        served: str,
        ports: Mapping[str, int],
        template_tokens: int,
    ) -> None:
        self.calibration = calibration
        self.model = model
        self.served = served
        self.ports = ports
        self.template_tokens = template_tokens

    def _client(self, pod: str) -> httpx.AsyncClient:
        # Each call has its own deadline, which covers connecting too.
        return httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.ports[pod]}",
            timeout=None,
            limits=NO_CONNECTION_LIMIT,
        )

    def warm(self) -> dict[str, list[WarmCall]]:
        return asyncio.run(self._warm())

    async def _warm(self) -> dict[str, list[WarmCall]]:
        warmup = self.calibration.warmup
        words = prompt_words(warmup.prompt_tokens, self.template_tokens)

        async def warm_one(pod: str) -> list[WarmCall]:
            async with self._client(pod) as client:

                async def call(index: int) -> WarmCall:
                    reply = await measured_call(
                        client,
                        self.served,
                        pod,
                        CallPrompt(None, warmup_prompt(pod, index, words, self.calibration.seed)),
                        self.calibration.completion_tokens,
                        warmup.prompt_tokens,
                        _headers(self.calibration, self.model, index),
                        self.calibration.call_timeout_seconds,
                    )
                    return WarmCall(reply.ttft_ms, time.time())

                calls = await warm_pod(call, warmup)
            ttfts = [made.ttft_ms for made in calls]
            print(f"calibrate: warmed {pod}, ms to first token {ttfts}", flush=True)
            return calls

        pods = list(self.ports)
        return dict(zip(pods, await asyncio.gather(*map(warm_one, pods)), strict=True))

    def cell(self, cell: Cell) -> dict[str, list[Measured]]:
        return asyncio.run(self._cell(cell))

    async def _cell(self, cell: Cell) -> dict[str, list[Measured]]:
        """Every pod's calls at once, as two pods sharing the GPU see it."""
        calibration = self.calibration
        words = prompt_words(cell.prompt_tokens, self.template_tokens)

        async def one_pod(pod: str, client: httpx.AsyncClient) -> list[Measured]:
            async def call(index: int) -> Answer:
                prompt = call_prompt(
                    cell, pod, index, words, calibration.partial_share, calibration.seed
                )
                return await measured_call(
                    client,
                    self.served,
                    pod,
                    prompt,
                    calibration.completion_tokens,
                    cell.prompt_tokens,
                    _headers(calibration, self.model, index),
                    calibration.call_timeout_seconds,
                )

            results = await closed_loop(call, cell.concurrency, calibration.calls_per_cell)
            return [
                Measured(
                    pod=pod,
                    ttft_ms=answer.ttft_ms,
                    duration_ms=answer.duration_ms,
                    completion_tokens=answer.completion_tokens,
                    cached_tokens=answer.cached_tokens,
                    in_flight_at_end=working,
                )
                for answer, working in results
            ]

        async with AsyncExitStack() as stack:
            clients = {
                pod: await stack.enter_async_context(self._client(pod)) for pod in self.ports
            }
            measured = await asyncio.gather(*(one_pod(pod, clients[pod]) for pod in clients))
        return dict(zip(clients, measured, strict=True))


# The cluster.


def ready_pods(pods: Mapping[str, Any], count: int) -> list[str]:
    """The Ready pods not being deleted, from kubectl get pods -o json; anything but count of them
    means the Deployment has not settled."""
    names = sorted(
        pod["metadata"]["name"]
        for pod in pods["items"]
        if "deletionTimestamp" not in pod["metadata"]
        and any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in pod["status"].get("conditions", [])
        )
    )
    if len(names) != count:
        raise CalibrationError(f"{names} are Ready, the Deployment should have replicas {count}")
    return names


def forwarded_port(line: str) -> int:
    """The local port from kubectl port-forward's first line."""
    found = FORWARDING.match(line)
    if found is None:
        raise CalibrationError(f"kubectl port-forward did not forward: {line!r}")
    return int(found.group(1))


def expected_models(model: Mapping[str, Any]) -> set[str]:
    """What a pod of the profile lists at /v1/models: the base model, and the adapter if served."""
    return {model["name"], *([model["lora"]["name"]] if "lora" in model else [])}


def _discard(stream: IO[str]) -> None:
    """kubectl prints a line per connection, and a pipe nobody reads would stop it once full."""
    for _ in stream:
        pass


@contextmanager
def _forward(pod: str) -> Iterator[int]:
    """A local port to the pod's vLLM until the block ends. kubectl's errors go to the terminal."""
    process = subprocess.Popen(
        ["kubectl", "port-forward", f"pod/{pod}", f":{VLLM_PORT}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        port = forwarded_port(process.stdout.readline())
        threading.Thread(target=_discard, args=(process.stdout,), daemon=True).start()
        yield port
    finally:
        process.terminate()
        process.wait()


async def _check_pod(port: int, pod: str, model: Mapping[str, Any]) -> int:
    """The pod serves the profile's models; returns the tokens its chat template adds, as the
    replayer measures them (load_generator.replay.measure_template_and_plan)."""
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        response = await client.get("/v1/models")
        response.raise_for_status()
        served = {entry["id"] for entry in response.json()["data"]}
        if served != expected_models(model):
            raise CalibrationError(
                f"{pod} serves {sorted(served)}, the profile has {sorted(expected_models(model))}"
            )
        empty = tokenize_request(served_model(model), "")
        counted = await client.post(TOKENIZE_PATH, json=empty)
        counted.raise_for_status()
        template: int = counted.json()["count"]
    return template


def _windows(*command: str) -> str:
    return subprocess.run(list(command), stdout=subprocess.PIPE, text=True, check=True).stdout


class LiveCluster:
    def __init__(self, calibration: Calibration, profiles: Mapping[str, Mapping[str, Any]]) -> None:
        self.calibration = calibration
        self.profiles = profiles

    def check_host(self) -> None:
        host = self.calibration.host
        check_host(
            _windows("powercfg.exe", "/getactivescheme"),
            {
                alias: _windows("powercfg.exe", "/query", "SCHEME_CURRENT", "SUB_SLEEP", alias)
                for alias in host.sleep_settings
            },
            host,
        )
        chart = json.loads(
            subprocess.run(
                ["helm", "get", "metadata", "vllm", "-o", "json"],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout
        )["chart"]
        if chart != REAL_CHART:
            raise CalibrationError(f"the pods are chart {chart}'s: make down, then make up")
        check_journal_readable()

    def deploy(self, profile: str) -> None:
        subprocess.run(["make", "vllm-deploy", f"PROFILE={profile}"], cwd=REPO_ROOT, check=True)

    def _ready(self, count: int) -> list[str]:
        return ready_pods(
            json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json")), count
        )

    def scale(self, replicas: int) -> list[str]:
        """status.replicas leaves out pods being deleted, so once it is replicas the ReplicaSet has
        chosen which to delete; they are waited for, since they hold the GPU until gone."""
        timeout = f"--timeout={WAIT_TIMEOUT}"
        kubectl("scale", VLLM_DEPLOYMENT, f"--replicas={replicas}")
        kubectl("wait", VLLM_DEPLOYMENT, f"--for=jsonpath={{.status.replicas}}={replicas}", timeout)
        pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
        for pod in pods["items"]:
            if "deletionTimestamp" in pod["metadata"]:
                kubectl("wait", f"pod/{pod['metadata']['name']}", "--for=delete", timeout)
        kubectl("rollout", "status", VLLM_DEPLOYMENT, timeout)
        return self._ready(replicas)

    def restart(self) -> list[str]:
        """The Deployment's strategy is Recreate: the old pods are gone before the new start."""
        subprocess.run(["bash", str(REPO_ROOT / "infra" / "restart-vllm.sh")], check=True)
        return self._ready(self.calibration.replicas)

    def pod_json(self, pod: str) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(kubectl("get", "pod", pod, "-o", "json"))
        return found

    @contextmanager
    def reach(self, model: str, pods: Sequence[str]) -> Iterator[LivePods]:
        profile_model = self.profiles[model]["model"]
        with ExitStack() as stack:
            ports = {pod: stack.enter_context(_forward(pod)) for pod in pods}
            counts = {
                asyncio.run(_check_pod(port, pod, profile_model)) for pod, port in ports.items()
            }
            if len(counts) != 1:
                raise CalibrationError(f"the pods' chat templates add {sorted(counts)} tokens")
            (template,) = counts
            yield LivePods(self.calibration, model, served_model(profile_model), ports, template)

    def clock(self) -> ClockReading:
        before = time.monotonic()
        windows = _windows(
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()",
        )
        return ClockReading(before, int(windows), time.monotonic())

    def clock_jumps(self, since: float, until: float) -> int:
        return count_clock_jumps(since, until)


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.8a: measure real vLLM pods for the simulator.")
    parser.add_argument(
        "experiment", type=Path, nargs="?", default=REPO_ROOT / "experiments" / "calibrate.yaml"
    )
    args = parser.parse_args()

    config_dir = REPO_ROOT / "config"
    calibration = load_calibration(args.experiment, config_dir)
    paths = {model: config_dir / f"{name}.yaml" for model, name in calibration.profiles.items()}
    profiles = {model: yaml.safe_load(path.read_text()) for model, path in paths.items()}
    sha256 = {model: hashlib.sha256(path.read_bytes()).hexdigest() for model, path in paths.items()}
    planned = outputs(calibration, REPO_ROOT / "sim")
    run_calibration(calibration, planned, LiveCluster(calibration, profiles), sha256)


if __name__ == "__main__":
    main()
