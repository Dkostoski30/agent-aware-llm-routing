"""Priority through the gateway, shown live (P4.1, docs/task-cards/57-P4.1-priority.md).

Scales vLLM to one pod, fills it with long first-step calls ("fillers", step 1 of 10) until calls
wait for KV blocks, then sends one last-step call ("the finisher", step 10 of 10) of the same size.
One pod, since two received their fillers up to 50 s apart and one pod's queue had emptied before
the other's formed (2026-11-19, experiments/log.md). With the picker's priority rule on, the
finisher's first token must come before every filler still waiting when it was sent; with the rule
off (the control, plugin still on) after all of them. Both modes run through the gateway, so the
check also shows the picker's rewritten body reaches vLLM. The profile's routing and replicas are
put back at the end, whatever happened.

Usage: priority_check.py <profile>   (make priority-check PROFILE=real)
"""

import argparse
import http.client
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_SERVICE = "inference-gateway"
VLLM_SERVICE = "vllm"
VLLM_PORT = 8000
# Two fillers fit a pod's KV cache at once and a third does not: each prompt is 2/5 of it, so
# three need 6/5, while two plus their answers stay under it while MAX_TOKENS is at most 1/10.
PROMPT_SHARE_OF_KV = 0.4
# Long enough that a filler holds its blocks for several seconds of decode (~0.3 s per token on
# two pods with the adapter, P2.8a), short enough that a mode takes minutes, not tens of them.
MAX_TOKENS = 150
# Eight fillers on the one pod: two running, six waiting for KV blocks.
FILLERS = 8
# The fillers' headers: the first of ten steps, so the most slack the rule gives a run's call.
FILLER_STEP, FILLER_EXPECTED = 1, 10
# How long the fillers may take to reach the engines and queue before the check gives up. On
# 2026-11-19 a pod fresh from make up counted the fillers sent to it only ~50 s after they were sent
# (experiments/log.md, P4.1), while the other pod took 12 s; 180 s covers that three times over.
QUEUE_TIMEOUT_SECONDS = 180
# How often the pods' own /metrics are read while waiting: often against a filler's ~45 s of decode,
# so the finisher goes within a second of the queue forming.
QUEUE_POLL_SECONDS = 0.5
# Covers a waiting filler's whole wait: two waves of MAX_TOKENS on a pod at ~0.3 s per token.
REQUEST_TIMEOUT_SECONDS = 300
# Every prompt starts with a unique tag, so no two calls share a cached prefix and each needs its
# own blocks; the rest repeats this sentence.
SENTENCE = " The quick brown fox jumps over the lazy dog."
CACHE_INFO = re.compile(r"^vllm:cache_config_info\{(?P<labels>[^}]*)\}", re.MULTILINE)
# vLLM's engine counts, one series per model name (the base model and the adapter).
ENGINE_COUNT = re.compile(
    r"^vllm:num_requests_(?P<state>running|waiting)(?:\{[^}]*\})? (?P<value>\S+)$", re.MULTILINE
)
VLLM_SELECTOR = "app.kubernetes.io/name=vllm"
VLLM_DEPLOYMENT = "deployment/vllm"
# As make experiment's (loadgen/load_generator/experiment.py): a pod loading the model on one GPU
# and disk, as VLLM_DEPLOY_TIMEOUT in the Makefile; scaling down waits for the pod's 30 s grace.
SCALE_TIMEOUT = "900s"


@dataclass(frozen=True)
class CallTiming:
    name: str
    finisher: bool
    pod: str
    # Seconds on this process's monotonic clock.
    sent: float
    first_byte: float


@dataclass(frozen=True)
class ModeVerdict:
    # The finisher's pod.
    pod: str
    # Fillers on that pod that had no first byte yet when the finisher was sent.
    waiting: tuple[str, ...]
    # Those of them whose first byte came after the finisher's.
    overtaken: tuple[str, ...]


def kv_capacity_tokens(metrics: str) -> int:
    """A pod's KV cache in tokens, from vLLM's cache_config_info: num_gpu_blocks x block_size."""
    match = CACHE_INFO.search(metrics)
    if match is None:
        raise ValueError("no vllm:cache_config_info line in the pod's metrics")
    labels = dict(re.findall(r'(\w+)="([^"]*)"', match.group("labels")))
    return int(labels["num_gpu_blocks"]) * int(labels["block_size"])


def engine_counts(metrics: str) -> tuple[int, int]:
    """A pod's calls running and waiting in its vLLM engine, over every model name it serves."""
    totals: dict[str, float] = {}
    for match in ENGINE_COUNT.finditer(metrics):
        totals[match["state"]] = totals.get(match["state"], 0.0) + float(match["value"])
    for state in ("running", "waiting"):
        if state not in totals:
            raise ValueError(f"no vllm:num_requests_{state} in the pod's metrics")
    return int(totals["running"]), int(totals["waiting"])


def queue_formed(
    pods: Mapping[str, tuple[int, int]], fillers: int, started: int, finished: int
) -> bool:
    """Whether the finisher may go: every pod has a call waiting, every filler is in an engine's
    counts (running or waiting) or has finished, and every running filler has its first token. Then
    a filler with no first token is one vLLM holds in its queue, which is what the verdict must
    read; before it, a filler may still be in its prefill or on its way, and only look waiting
    (2026-11-19: a cold pod's engine saw its fillers and the finisher in one scrape)."""
    running = sum(count[0] for count in pods.values())
    waiting = sum(count[1] for count in pods.values())
    return (
        all(count[1] >= 1 for count in pods.values())
        and running + waiting + finished == fillers
        and started == running + finished
    )


def prompt_sentences(target_tokens: int, base_tokens: int, sentence_tokens: int) -> int:
    """How many sentences bring a prompt of base_tokens up to target_tokens, not past it."""
    if sentence_tokens <= 0:
        raise ValueError(f"a sentence of {sentence_tokens} tokens cannot fill a prompt")
    return max(target_tokens - base_tokens, 0) // sentence_tokens


def parse_chunk(line: bytes) -> tuple[str | None, int | None] | None:
    """The pod (system_fingerprint, the pod's name since P2.2a) and the prompt tokens of one line of
    a vLLM stream, each None when the chunk lacks it; None for a line that holds no chunk. vLLM
    v0.31.0 puts both on the last chunk only, the usage chunk (chat_completion/serving.py)."""
    text = line.decode().strip()
    if not text.startswith("data: ") or text == "data: [DONE]":
        return None
    chunk = json.loads(text.removeprefix("data: "))
    usage = chunk.get("usage")
    return chunk.get("system_fingerprint"), None if usage is None else usage["prompt_tokens"]


def read_stream(
    lines: Iterable[bytes], clock: Callable[[], float]
) -> tuple[float, str, int | None]:
    """The time of a stream's first chunk on clock, which vLLM sends with the first token, and the
    pod and prompt tokens of its last chunk, the only one that carries them. A stream with no
    chunk, or whose last chunk names no pod, is refused."""
    first_byte: float | None = None
    pod: str | None = None
    prompt_tokens: int | None = None
    for line in lines:
        parsed = parse_chunk(line)
        if parsed is None:
            continue
        if first_byte is None:
            first_byte = clock()
        pod, prompt_tokens = parsed
    if first_byte is None:
        raise ValueError("the stream ended with no chunk")
    if pod is None:
        raise ValueError("the stream named no pod in any chunk")
    return first_byte, pod, prompt_tokens


def verdict(timings: Sequence[CallTiming]) -> ModeVerdict:
    """Which fillers on the finisher's pod were waiting when it was sent, and which of them it
    overtook. A filler on the other pod never competed with it."""
    finishers = [timing for timing in timings if timing.finisher]
    if len(finishers) != 1:
        raise ValueError(f"want one finisher, got {len(finishers)}")
    (last,) = finishers
    waiting = [
        timing
        for timing in timings
        if not timing.finisher and timing.pod == last.pod and timing.first_byte > last.sent
    ]
    return ModeVerdict(
        pod=last.pod,
        waiting=tuple(timing.name for timing in waiting),
        overtaken=tuple(timing.name for timing in waiting if timing.first_byte > last.first_byte),
    )


def judge(on: ModeVerdict, off: ModeVerdict) -> tuple[bool, list[str]]:
    """Passed when, with a queue on the finisher's pod in both modes, the finisher overtook every
    waiting filler with the rule on and none with it off."""
    lines = []
    for mode, result in (("priority on", on), ("priority off", off)):
        lines.append(
            f"{mode}: finisher on {result.pod}, fillers waiting there at its send "
            f"{list(result.waiting)}, of them served after it {list(result.overtaken)}"
        )
    failures = []
    for mode, result in (("priority on", on), ("priority off", off)):
        if not result.waiting:
            failures.append(f"{mode}: no filler waiting on the finisher's pod, nothing to overtake")
    if on.waiting and on.overtaken != on.waiting:
        failures.append("priority on: the finisher did not go ahead of every waiting filler")
    if off.waiting and off.overtaken:
        failures.append("priority off: the finisher went ahead without the rule")
    lines.extend(failures)
    lines.append("priority-check: passed" if not failures else "priority-check: FAIL")
    return not failures, lines


def kubectl_service_ip(name: str) -> str:
    done = subprocess.run(
        ["kubectl", "get", "service", name, "-o", "jsonpath={.spec.clusterIP}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def post_json(host: str, port: int, path: str, body: dict[str, Any]) -> dict[str, Any]:
    connection = http.client.HTTPConnection(host, port, timeout=REQUEST_TIMEOUT_SECONDS)
    connection.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    response = connection.getresponse()
    payload = response.read()
    if response.status != 200:
        raise RuntimeError(f"POST {path}: {response.status} {payload[:300]!r}")
    result: dict[str, Any] = json.loads(payload)
    return result


def get_text(host: str, port: int, path: str) -> str:
    connection = http.client.HTTPConnection(host, port, timeout=REQUEST_TIMEOUT_SECONDS)
    connection.request("GET", path)
    response = connection.getresponse()
    if response.status != 200:
        raise RuntimeError(f"GET {path}: {response.status}")
    return response.read().decode()


def profile_config(profile: str) -> dict[str, Any]:
    config: dict[str, Any] = yaml.safe_load((REPO_ROOT / "config" / f"{profile}.yaml").read_text())
    return config


def model_name(profile: str) -> str:
    """The adapter's name when the profile serves one, as the agent's calls name it; else the
    model's."""
    model = profile_config(profile)["model"]
    name: str = model["lora"]["name"] if "lora" in model else model["name"]
    return name


def messages(tag: str, sentences: int) -> list[dict[str, str]]:
    return [{"role": "user", "content": f"Call {tag}." + SENTENCE * sentences}]


def sized_sentences(vllm_ip: str, model: str, target: int) -> int:
    """Sentences for a prompt of about target tokens, counted by the pods' own tokenizer."""

    def count(sentences: int) -> int:
        body = {"model": model, "messages": messages("sizing", sentences)}
        tokens: int = post_json(vllm_ip, VLLM_PORT, "/tokenize", body)["count"]
        return tokens

    base = count(0)
    return prompt_sentences(target, base, count(1) - base)


class FillerProgress:
    """How many fillers have their first token and how many have ended, counted by their threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = 0
        self._finished = 0

    def first_token(self) -> float:
        """The clock a filler's stream reads at its first chunk, which also counts it started."""
        with self._lock:
            self._started += 1
        return time.monotonic()

    def finished(self) -> None:
        with self._lock:
            self._finished += 1

    def counts(self) -> tuple[int, int]:
        with self._lock:
            return self._started, self._finished


def ready_pod_ips() -> list[str]:
    """The IPs of the Ready vLLM pods, whose own /metrics say what their engines hold."""
    done = subprocess.run(
        [
            "kubectl",
            "get",
            "pods",
            "-l",
            VLLM_SELECTOR,
            "-o",
            "jsonpath={range .items[?(@.status.containerStatuses[0].ready==true)]}"
            '{.status.podIP}{"\\n"}{end}',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.split()


def wait_for_queue(
    pod_ips: Sequence[str], progress: FillerProgress, fillers: Sequence[Future[CallTiming]]
) -> None:
    """Returns once queue_formed holds on the pods' own counts; a filler that failed meanwhile
    raises its error here, and a queue that never forms within QUEUE_TIMEOUT_SECONDS stops the
    check."""
    deadline = time.monotonic() + QUEUE_TIMEOUT_SECONDS
    while True:
        pods = {ip: engine_counts(get_text(ip, VLLM_PORT, "/metrics")) for ip in pod_ips}
        started, finished = progress.counts()
        if queue_formed(pods, FILLERS, started, finished):
            print(
                f"priority-check: queue formed, (running, waiting) per pod {pods}, "
                f"{started} fillers with a first token, {finished} ended"
            )
            return
        for future in fillers:
            if future.done():
                future.result()
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"no queue formed in {QUEUE_TIMEOUT_SECONDS} s: (running, waiting) per pod "
                f"{pods}, {started} fillers with a first token, {finished} ended"
            )
        time.sleep(QUEUE_POLL_SECONDS)


def stream_call(
    gateway_ip: str,
    model: str,
    name: str,
    step: int,
    expected: int,
    sentences: int,
    progress: FillerProgress | None,
) -> CallTiming:
    """One streamed call through the gateway; its first byte is its first chunk, which vLLM sends
    with the first token. A filler reports its first token and its end to progress."""
    body = {
        "model": model,
        "messages": messages(name, sentences),
        "max_tokens": MAX_TOKENS,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Content-Type": "application/json",
        "x-run-id": f"priority-check-{name}",
        "x-step": str(step),
        "x-expected-steps": str(expected),
    }
    connection = http.client.HTTPConnection(gateway_ip, 80, timeout=REQUEST_TIMEOUT_SECONDS)
    sent = time.monotonic()
    connection.request("POST", "/v1/chat/completions", json.dumps(body), headers)
    response = connection.getresponse()
    if response.status != 200:
        raise RuntimeError(f"{name}: {response.status} {response.read()[:300]!r}")
    clock = time.monotonic if progress is None else progress.first_token
    try:
        first_byte, pod, prompt_tokens = read_stream(response, clock)
    except ValueError as error:
        raise RuntimeError(f"{name}: {error}") from error
    if progress is not None:
        progress.finished()
    print(
        f"priority-check: {name} on {pod}, {prompt_tokens} prompt tokens, first byte "
        f"{first_byte - sent:.1f} s after its send"
    )
    return CallTiming(
        name=name, finisher=name == "finisher", pod=pod, sent=sent, first_byte=first_byte
    )


def run_mode(gateway_ip: str, model: str, sentences: int) -> ModeVerdict:
    pod_ips = ready_pod_ips()
    progress = FillerProgress()
    with ThreadPoolExecutor(max_workers=FILLERS + 1) as pool:
        fillers = [
            pool.submit(
                stream_call,
                gateway_ip,
                model,
                f"filler-{n}",
                FILLER_STEP,
                FILLER_EXPECTED,
                sentences,
                progress,
            )
            for n in range(1, FILLERS + 1)
        ]
        wait_for_queue(pod_ips, progress, fillers)
        last = pool.submit(
            stream_call,
            gateway_ip,
            model,
            "finisher",
            FILLER_EXPECTED,
            FILLER_EXPECTED,
            sentences,
            None,
        )
        timings = [future.result() for future in [*fillers, last]]
    return verdict(timings)


def kubectl(*args: str) -> None:
    subprocess.run(["kubectl", *args], check=True)


def scale(replicas: int) -> None:
    """As make experiment's: status.replicas leaves out pods being deleted, so once it is the count
    the ReplicaSet has chosen which to delete; they are waited for, since a real pod holds the GPU
    until gone."""
    timeout = f"--timeout={SCALE_TIMEOUT}"
    kubectl("scale", VLLM_DEPLOYMENT, f"--replicas={replicas}")
    kubectl("wait", VLLM_DEPLOYMENT, f"--for=jsonpath={{.status.replicas}}={replicas}", timeout)
    done = subprocess.run(
        ["kubectl", "get", "pods", "-l", VLLM_SELECTOR, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    for pod in json.loads(done.stdout)["items"]:
        if "deletionTimestamp" in pod["metadata"]:
            kubectl("wait", f"pod/{pod['metadata']['name']}", "--for=delete", timeout)
    kubectl("rollout", "status", VLLM_DEPLOYMENT, timeout)


def set_routing(profile: str, rules: dict[str, bool] | None) -> None:
    """infra/set-routing.sh with rules laid over the profile's, or the profile's own for None."""
    if rules is None:
        subprocess.run(["bash", "infra/set-routing.sh", profile], cwd=REPO_ROOT, check=True)
        return
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as file:
        yaml.safe_dump({"routing": rules}, file)
        file.flush()
        subprocess.run(
            ["bash", "infra/set-routing.sh", profile, file.name], cwd=REPO_ROOT, check=True
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile")
    profile = parser.parse_args().profile
    gateway_ip = kubectl_service_ip(GATEWAY_SERVICE)
    vllm_ip = kubectl_service_ip(VLLM_SERVICE)
    model = model_name(profile)
    capacity = kv_capacity_tokens(get_text(vllm_ip, VLLM_PORT, "/metrics"))
    sentences = sized_sentences(vllm_ip, model, int(capacity * PROMPT_SHARE_OF_KV))
    print(f"priority-check: KV cache {capacity} tokens per pod, prompts of {sentences} sentences")
    scale(1)
    try:
        try:
            set_routing(profile, {"plugin": True, "priority": True})
            print("priority-check: priority on")
            on = run_mode(gateway_ip, model, sentences)
            set_routing(profile, {"plugin": True, "priority": False})
            print("priority-check: priority off")
            off = run_mode(gateway_ip, model, sentences)
        finally:
            set_routing(profile, None)
    finally:
        scale(profile_config(profile)["replicas"])
    passed, lines = judge(on, off)
    print("\n".join(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
