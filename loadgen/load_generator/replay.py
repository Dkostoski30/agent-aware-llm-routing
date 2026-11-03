"""make replay (P2.3): a trace's LLM calls straight to the gateway, as new trace.v1 lines.

Reproduces each run's call sequence, prompt and reply lengths and tool-time gaps (D9), with the
runs arriving by a pattern from config/ under a fixed seed (load_generator.plan). One asyncio task
per run. Times come from the event loop's monotonic clock; ts_start alone is wall clock, since the
wall clock moved seconds against it under WSL2 (P2.2a).

A replay never retries, so a call with no full reply ends its run and the others go on: a replay
runs under overload on purpose and P3.4 and P4.4 count the runs that complete. A reply that is not
the planned length or not the expected shape is not a full reply. That call's line holds the planned
tokens and no duration_ms. The output is written once, after the last run, and never written over;
until then each run's lines go to <output>.partial as it ends, so a replay that dies keeps its
finished runs. Decided with the user, docs/task-cards/26-P2.3-replayer.md.

Usage: python -m load_generator.replay TRACE PATTERN [--profile real] [--seed N] [--out FILE]
"""

import argparse
import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from load_generator.plan import (
    Pattern,
    PlannedRun,
    PlannedStep,
    ReplayError,
    SharedPrefix,
    TraceRun,
    build_plan,
    call_headers,
    chat_request,
    check_trace_line,
    load_replay_config,
    load_trace,
    named_pattern,
    served_model,
    tokenize_request,
)
from load_generator.record import (
    REPO_ROOT,
    SCHEMA_VERSION,
    kubectl,
    trace_validator,
    write_trace,
)

# The Gateway every LLM call goes through (infra/manifests/inference-gateway.yaml). The host is the
# k3s node, so a ClusterIP is reachable from here, as in infra/checks/gateway_check.sh.
GATEWAY_SERVICE = "inference-gateway"
GATEWAY_PORT = 80
CHAT_PATH = "/v1/chat/completions"
# The check of the prompts goes to the pods' own Service: the gateway's route matches only /v1, and
# the picker must not learn these prompts in its prefix index before the replay sends them.
VLLM_SERVICE = "vllm"
VLLM_PORT = 8000
TOKENIZE_PATH = "/tokenize"
# httpx queues a request over 100 connections by default, and that wait would count as the gateway's
# latency, so the client sets no limit: what is in flight is what the pattern asks for.
NO_CONNECTION_LIMIT = httpx.Limits(max_connections=None, max_keepalive_connections=None)
# The parts of a stream's chunk a replay reads, so that a reply of another shape fails its own call
# instead of stopping every run with a TypeError. The rest of a chunk is left alone.
CHUNK_VALIDATOR = Draft202012Validator(
    {
        "type": "object",
        "properties": {
            "choices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "delta": {
                            "type": "object",
                            "properties": {"content": {"type": ["string", "null"]}},
                        }
                    },
                },
            },
            "usage": {
                "type": ["object", "null"],
                "required": ["prompt_tokens", "completion_tokens"],
                "properties": {
                    "prompt_tokens": {"type": "integer"},
                    "completion_tokens": {"type": "integer"},
                    "prompt_tokens_details": {
                        "type": ["object", "null"],
                        "properties": {"cached_tokens": {"type": ["integer", "null"]}},
                    },
                },
            },
            "system_fingerprint": {"type": ["string", "null"]},
        },
    }
)


class ReplyError(Exception):
    """A 2xx answer that is not a full streamed reply."""


@dataclass(frozen=True)
class Reply:
    # On the event loop's clock.
    ended: float
    ttft_ms: int | None
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None
    pod: str | None


def _milliseconds(seconds: float) -> int:
    return round(seconds * 1000)


async def _lines_of(response: httpx.Response) -> AsyncIterator[bytes]:
    """The stream's lines, split on newlines alone. response.aiter_lines also splits on U+2028,
    U+2029 and U+0085, which a token can hold raw, and the pieces would not be JSON. A newline byte
    is never part of a longer UTF-8 character, so splitting before decoding is safe."""
    rest = b""
    async for piece in response.aiter_bytes():
        *lines, rest = (rest + piece).split(b"\n")
        for line in lines:
            yield line
    yield rest


def _chunk_of(line: bytes) -> dict[str, Any] | None:
    """A server-sent line's JSON; None for what holds none: blank lines and the [DONE] mark."""
    data = line.strip()
    if not data.startswith(b"data: ") or data == b"data: [DONE]":
        return None
    try:
        chunk: dict[str, Any] = json.loads(data.removeprefix(b"data: "))
    except ValueError as error:
        raise ReplyError(f"a chunk that is not JSON: {error}") from error
    violation = best_match(CHUNK_VALIDATOR.iter_errors(chunk))
    if violation is not None:
        raise ReplyError(f"a chunk of another shape: {violation.message}")
    if chunk.get("error"):
        raise ReplyError(f"an error chunk: {chunk['error']}")
    return chunk


def _has_content(chunk: Mapping[str, Any]) -> bool:
    """vLLM's first chunk holds only the role, with empty content, so it is not yet a token."""
    return any(choice.get("delta", {}).get("content") for choice in chunk.get("choices", []))


async def _stream_reply(
    client: httpx.AsyncClient,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
    started: float,
) -> Reply:
    """Reads the whole stream. A non-2xx answer or a broken connection raises httpx.HTTPError; a
    chunk of another shape, a stream that ends without the token counts or a reply of another length
    than max_tokens, ReplyError."""
    loop = asyncio.get_running_loop()
    first_token: float | None = None
    usage: Mapping[str, Any] | None = None
    pod: str | None = None
    async with client.stream("POST", CHAT_PATH, json=body, headers=headers) as response:
        response.raise_for_status()
        async for line in _lines_of(response):
            chunk = _chunk_of(line)
            if chunk is None:
                continue
            if first_token is None and _has_content(chunk):
                first_token = loop.time()
            usage = chunk.get("usage") or usage
            pod = chunk.get("system_fingerprint") or pod
    ended = loop.time()
    if usage is None:
        raise ReplyError("the stream ended without the token counts")
    if usage["completion_tokens"] != body["max_tokens"]:
        raise ReplyError(
            f"a reply of {usage['completion_tokens']} tokens, the plan has {body['max_tokens']}"
        )
    return Reply(
        ended=ended,
        ttft_ms=None if first_token is None else _milliseconds(first_token - started),
        duration_ms=_milliseconds(ended - started),
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        pod=pod,
    )


def _optional_fields(reply: Reply) -> dict[str, Any]:
    """A measurement a reply lacks is left out of the line, as trace.v1 has it."""
    fields = {"ttft_ms": reply.ttft_ms, "cached_tokens": reply.cached_tokens, "pod": reply.pod}
    return {name: value for name, value in fields.items() if value is not None}


async def _call(
    client: httpx.AsyncClient,
    model: str,
    run: PlannedRun,
    step: PlannedStep,
    previous_end: float | None,
    deadline_seconds: float,
) -> tuple[dict[str, Any], float | None]:
    """One call's trace line and when its reply ended; None when it got no full reply. A real
    agent's call fails after agent.llm_timeout_seconds because its answer arrives whole
    (agent_runtime.llm), while a read timeout on a stream restarts with each chunk, so here the
    deadline is on the whole call."""
    started = asyncio.get_running_loop().time()
    line: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.run_id,
        "task_type": run.task_type,
        "step": step.step,
        "ts_start": round(time.time() * 1000),
        "gap_before_ms": 0 if previous_end is None else _milliseconds(started - previous_end),
        "expected_steps": step.expected_steps,
        "prompt_tokens": step.prompt_tokens,
        "completion_tokens": step.completion_tokens,
    }
    if step.parallel_group is not None:
        line["parallel_group"] = step.parallel_group
    body = chat_request(model, step.content, step.completion_tokens)
    try:
        async with asyncio.timeout(deadline_seconds):
            reply = await _stream_reply(client, body, call_headers(run.run_id, step), started)
    except (httpx.HTTPError, ReplyError, TimeoutError) as error:
        print(f"replay: {run.run_id} step {step.step}: no full reply: {error!r}", flush=True)
        return line, None
    line.update(
        duration_ms=reply.duration_ms,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        **_optional_fields(reply),
    )
    return line, reply.ended


async def _replay_run(
    client: httpx.AsyncClient, model: str, run: PlannedRun, zero: float, deadline_seconds: float
) -> list[dict[str, Any]]:
    """A run from its start time to its last reply, or to its first failed call. Each gap starts at
    the end of the step before (D9), so a slow pod delays the rest of the run, as with a real agent;
    a fanned-out step ends with its last reply."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(max(0.0, zero + run.start_seconds - loop.time()))
    lines: list[dict[str, Any]] = []
    previous_end: float | None = None
    for step in run.steps:
        if previous_end is not None:
            await asyncio.sleep(step.gap_before_ms / 1000)
        results = await asyncio.gather(
            *(
                _call(client, model, run, step, previous_end, deadline_seconds)
                for _ in range(step.calls)
            )
        )
        lines.extend(line for line, _ in results)
        ends = [ended for _, ended in results if ended is not None]
        if len(ends) < len(results):
            break
        previous_end = max(ends)
    return lines


async def replay_runs(
    client: httpx.AsyncClient,
    model: str,
    plan: Sequence[PlannedRun],
    deadline_seconds: float,
    keep_run: Callable[[list[dict[str, Any]]], None],
) -> list[dict[str, Any]]:
    """Every run's lines in the plan's order, whichever run ended first, so that two replays of one
    seed can be compared line by line. keep_run gets a run's lines as it ends."""
    zero = asyncio.get_running_loop().time()
    finished = 0

    async def replay_one(run: PlannedRun) -> list[dict[str, Any]]:
        nonlocal finished
        lines = await _replay_run(client, model, run, zero, deadline_seconds)
        keep_run(lines)
        finished += 1
        pods = sorted({line["pod"] for line in lines if "pod" in line})
        print(
            f"replay: {finished}/{len(plan)} {run.run_id} {run.task_type}: {len(lines)} calls, "
            f"pods {pods}",
            flush=True,
        )
        return lines

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(replay_one(run)) for run in plan]
    return [line for task in tasks for line in task.result()]


async def _count_tokens(client: httpx.AsyncClient, model: str, content: str) -> int:
    response = await client.post(TOKENIZE_PATH, json=tokenize_request(model, content))
    response.raise_for_status()
    count: int = response.json()["count"]
    return count


async def measure_template_and_plan(
    tokenizer: httpx.AsyncClient,
    model: str,
    runs: Sequence[TraceRun],
    pattern: Pattern,
    shared: SharedPrefix,
    seed: int,
) -> list[PlannedRun]:
    """The plan, once the server has counted what the chat template adds and every planned prompt:
    a prompt of another length would not be the trace's load, so the replay does not start."""
    template_tokens = await _count_tokens(tokenizer, model, "")
    plan = build_plan(runs, pattern, shared, seed, template_tokens)
    for run in plan:
        for step in run.steps:
            count = await _count_tokens(tokenizer, model, step.content)
            if count != step.prompt_tokens:
                raise ReplayError(
                    f"{run.run_id} step {step.step}: the prompt counts {count} tokens on the "
                    f"server, the trace has {step.prompt_tokens}"
                )
    return plan


def check_lines(validator: Draft202012Validator, lines: Iterable[Mapping[str, Any]]) -> None:
    """A reply that breaks the contract (D12) fails the replay after its last call: the output is
    not written and the partial file stays."""
    for line in lines:
        check_trace_line(validator, line, f"{line.get('run_id')} step {line.get('step')}")


def failed_calls(lines: Iterable[Mapping[str, Any]]) -> int:
    return sum("duration_ms" not in line for line in lines)


def output_path(trace: Path, pattern: str, seed: int) -> Path:
    name = f"{trace.stem}-{pattern}-{seed}.jsonl"
    return REPO_ROOT / "experiments" / "results" / "replays" / name


def _append_lines(path: Path, lines: Iterable[Mapping[str, Any]]) -> None:
    """Opened per run, so that nothing stays in a buffer if the replay dies."""
    with path.open("a") as out:
        out.writelines(json.dumps(line) + "\n" for line in lines)


def _service_url(service: str, port: int) -> str:
    ip = kubectl("get", "service", service, "-o", "jsonpath={.spec.clusterIP}")
    return f"http://{ip}:{port}"


async def _replay(
    model: str,
    runs: Sequence[TraceRun],
    pattern: Pattern,
    shared: SharedPrefix,
    seed: int,
    timeout: float,
    keep_run: Callable[[list[dict[str, Any]]], None],
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        base_url=_service_url(VLLM_SERVICE, VLLM_PORT), timeout=timeout
    ) as tokenizer:
        plan = await measure_template_and_plan(tokenizer, model, runs, pattern, shared, seed)
    async with httpx.AsyncClient(
        base_url=_service_url(GATEWAY_SERVICE, GATEWAY_PORT),
        # Each call has its own deadline, which covers connecting too.
        timeout=None,
        limits=NO_CONNECTION_LIMIT,
    ) as gateway:
        return await replay_runs(gateway, model, plan, timeout, keep_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.3: replay a trace through the gateway.")
    parser.add_argument("trace", type=Path, help="a trace.v1 file, e.g. from make record")
    parser.add_argument("pattern", help="a name in replay.patterns of the profile")
    parser.add_argument("--profile", default="real", help="config/<profile>.yaml")
    parser.add_argument("--seed", type=int, help="default: replay.seed of the profile")
    parser.add_argument("--out", type=Path, help="default: experiments/results/replays/...")
    args = parser.parse_args()

    profile = yaml.safe_load((REPO_ROOT / "config" / f"{args.profile}.yaml").read_text())
    config = load_replay_config(profile["replay"])
    pattern = named_pattern(config, args.pattern)
    seed = config.seed if args.seed is None else args.seed
    out = args.out or output_path(args.trace, args.pattern, seed)
    if out.exists():
        raise ReplayError(f"{out} exists: delete it, or name another file with OUT=")
    partial = out.with_name(f"{out.name}.partial")
    if partial.exists():
        raise ReplayError(
            f"{partial} is left by a replay that stopped and holds its finished runs: keep it "
            "under another name or delete it"
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = asyncio.run(
        _replay(
            served_model(profile["model"]),
            load_trace(args.trace),
            pattern,
            config.shared_prefix,
            seed,
            # A replayed call fails when a real agent's would.
            profile["agent"]["llm_timeout_seconds"],
            lambda run_lines: _append_lines(partial, run_lines),
        )
    )
    check_lines(trace_validator(), lines)
    write_trace(out, lines)
    partial.unlink()
    failed = failed_calls(lines)
    print(f"replay: {len(lines)} calls, {failed} failed")
    print(f"replay: wrote {out}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
