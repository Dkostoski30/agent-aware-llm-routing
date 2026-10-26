"""Load check for two vLLM replicas on one GPU (P0.4).

Sends chat requests to every port at once for a fixed time, samples GPU memory and container RAM
every 30 s, and prints per-port request counts, errors, p50/p95 latency and the peaks.
Exit 1 on any error.

Usage: concurrency_check.py --ports 8001 8002 --concurrency 4 --minutes 10
"""

import argparse
import http.client
import json
import math
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# vllm-pair.sh names its containers like this; their RAM is sampled with docker stats.
DEFAULT_CONTAINERS = ["vllm-1", "vllm-2"]
# The card's sampling interval: often enough to catch a peak over a 10-minute run,
# rare enough not to add load.
SAMPLE_INTERVAL_SECONDS = 30
# Longer than any answer of MAX_TOKENS below, even with both replicas saturated;
# a slower answer is an error.
REQUEST_TIMEOUT_SECONDS = 120
# Bounds each answer, so one request takes seconds and a 10-minute run sends hundreds of them.
MAX_TOKENS = 128
PROMPT = "Explain in a short paragraph how a Kubernetes Service picks a pod."

UNIT_BYTES = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
DOCKER_MEM_PATTERN = re.compile(r"^\s*([0-9.]+)\s*(B|KiB|MiB|GiB)\s*/")


@dataclass(frozen=True)
class RequestResult:
    port: int
    ok: bool
    seconds: float


@dataclass(frozen=True)
class PortSummary:
    requests: int
    errors: int
    p50_seconds: float | None
    p95_seconds: float | None


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least q% of the values at or below it."""
    if not values:
        raise ValueError("percentile of no values")
    if not 0 < q <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {q}")
    ordered = sorted(values)
    rank = math.ceil(q * len(ordered) / 100)
    return ordered[rank - 1]


def summarize(results: Sequence[RequestResult], ports: Sequence[int]) -> dict[int, PortSummary]:
    """Per-port counts and latency; latency uses successful requests only."""
    summary = {}
    for port in ports:
        on_port = [r for r in results if r.port == port]
        latencies = [r.seconds for r in on_port if r.ok]
        summary[port] = PortSummary(
            requests=len(on_port),
            errors=sum(1 for r in on_port if not r.ok),
            p50_seconds=percentile(latencies, 50) if latencies else None,
            p95_seconds=percentile(latencies, 95) if latencies else None,
        )
    return summary


def peak_per_key(samples: Sequence[dict[str, int]]) -> dict[str, int]:
    peaks: dict[str, int] = {}
    for sample in samples:
        for key, value in sample.items():
            peaks[key] = max(peaks.get(key, value), value)
    return peaks


def parse_docker_mem(usage: str) -> int:
    """Bytes used from a docker stats MemUsage field such as "2.5GiB / 9.7GiB"."""
    match = DOCKER_MEM_PATTERN.match(usage)
    if match is None:
        raise ValueError(f"unrecognized docker memory usage {usage!r}")
    return int(float(match.group(1)) * UNIT_BYTES[match.group(2)])


def parse_nvidia_smi_mib(output: str) -> int:
    """MiB used on the first GPU, from nvidia-smi --query-gpu=memory.used (csv, no header/units)."""
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    if not first_line.isdigit():
        raise ValueError(f"unrecognized nvidia-smi output {output!r}")
    return int(first_line)


def model_on(port: int) -> str:
    with urllib.request.urlopen(
        f"http://localhost:{port}/v1/models", timeout=REQUEST_TIMEOUT_SECONDS
    ) as response:
        model: str = json.load(response)["data"][0]["id"]
    return model


def send_chat(port: int, model: str) -> RequestResult:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": MAX_TOKENS,
            # Thinking mode is off for every call in the thesis (CLAUDE.md).
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode()
    request = urllib.request.Request(
        f"http://localhost:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            ok = bool(json.load(response)["choices"][0]["message"]["content"])
    # OSError covers refused connections, timeouts and HTTP error statuses (urllib's HTTPError);
    # HTTPException covers a server that drops the connection mid-response.
    except (OSError, http.client.HTTPException) as error:
        print(f"port {port}: request failed: {error}", file=sys.stderr)
        ok = False
    return RequestResult(port=port, ok=ok, seconds=time.monotonic() - start)


def worker(port: int, model: str, deadline: float, results: list[RequestResult]) -> None:
    while time.monotonic() < deadline:
        results.append(send_chat(port, model))


def sample_gpu_mib() -> int:
    output = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return parse_nvidia_smi_mib(output)


def sample_container_bytes(containers: Sequence[str]) -> dict[str, int]:
    output = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}", *containers],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    sample = {}
    for line in output.strip().splitlines():
        name, usage = line.split("\t")
        sample[name] = parse_docker_mem(usage)
    return sample


def sampler(
    containers: Sequence[str],
    stop: threading.Event,
    gpu_samples: list[dict[str, int]],
    ram_samples: list[dict[str, int]],
) -> None:
    while True:
        gpu_samples.append({"gpu": sample_gpu_mib()})
        ram_samples.append(sample_container_bytes(containers))
        print(f"sample: GPU {gpu_samples[-1]['gpu']} MiB, RAM {format_ram(ram_samples[-1])}")
        if stop.wait(SAMPLE_INTERVAL_SECONDS):
            return


def format_ram(sample: dict[str, int]) -> str:
    return ", ".join(f"{name} {value / 1024**3:.2f} GiB" for name, value in sorted(sample.items()))


def format_seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ports", type=int, nargs="+", required=True)
    parser.add_argument(
        "--concurrency", type=int, required=True, help="requests in flight per port"
    )
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--containers", nargs="+", default=DEFAULT_CONTAINERS)
    args = parser.parse_args()

    models = {port: model_on(port) for port in args.ports}
    deadline = time.monotonic() + args.minutes * 60
    results: list[RequestResult] = []
    gpu_samples: list[dict[str, int]] = []
    ram_samples: list[dict[str, int]] = []
    stop = threading.Event()
    sampling = threading.Thread(
        target=sampler, args=(args.containers, stop, gpu_samples, ram_samples)
    )
    sampling.start()
    print(
        f"sending to ports {args.ports}, {args.concurrency} in flight per port, "
        f"for {args.minutes} min"
    )
    with ThreadPoolExecutor(max_workers=args.concurrency * len(args.ports)) as pool:
        futures = [
            pool.submit(worker, port, models[port], deadline, results)
            for port in args.ports
            for _ in range(args.concurrency)
        ]
    stop.set()
    # A worker that crashed on something other than a failed request (e.g. a malformed response)
    # re-raises here.
    for future in futures:
        future.result()
    sampling.join()

    summary = summarize(results, args.ports)
    for port, s in summary.items():
        print(
            f"port {port}: {s.requests} requests, {s.errors} errors, "
            f"p50 {format_seconds(s.p50_seconds)}, p95 {format_seconds(s.p95_seconds)}"
        )
    print(f"peak GPU memory used: {peak_per_key(gpu_samples)['gpu']} MiB (whole GPU)")
    print(f"peak container RAM: {format_ram(peak_per_key(ram_samples))}")
    total_errors = sum(s.errors for s in summary.values())
    if total_errors or any(s.requests == 0 for s in summary.values()):
        print(f"concurrency-check: FAIL ({total_errors} errors)", file=sys.stderr)
        return 1
    print("concurrency-check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
