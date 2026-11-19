"""Sending a replay (P2.3): timing, measurements, failures and the output file.

The gateway is httpx.MockTransport with timed chunks, and the clock is an event loop whose time
jumps to the next timer when nothing is ready, so a run of minutes takes milliseconds and every time
here is exact. The production code only uses asyncio.sleep and loop.time. make replay's done-when
runs it against the real gateway. The schemas are the repo's own, so these are also trace.v1's and
headers.v1's contract tests for the replayer (D12).
"""

import asyncio
import contextlib
import json
import selectors
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from agent_runtime.run_tracker import METRICS_CONTENT_TYPE, RunTracker
from jsonschema import Draft202012Validator
from load_generator.plan import (
    FanOut,
    Pattern,
    PlannedRun,
    ReplayError,
    SharedPrefix,
    TraceCall,
    TraceRun,
    build_plan,
    load_trace,
)
from load_generator.replay import (
    check_lines,
    failed_calls,
    main,
    measure_template_and_plan,
    output_path,
    replay_runs,
    serve_metrics,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_SCHEMA = REPO_ROOT / "schemas" / "trace.v1.json"
HEADERS_SCHEMA = REPO_ROOT / "schemas" / "headers.v1.json"
TEMPLATE_TOKENS = 12
SHARED = SharedPrefix(all_types=8, by_type={"csv": 20})
MODEL = "thesis-adapter"
POD = "vllm-pod-1"
# Far longer than any call here, except in the tests of the deadline.
DEADLINE_SECONDS = 100_000.0


class _JumpingSelector(selectors.DefaultSelector):
    """Instead of waiting for the next timer, moves the time to it."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = super().select(0)
        if ready:
            return ready
        if timeout is None:
            raise RuntimeError("nothing is ready and no timer is set: the test can never finish")
        self.now += timeout
        return []


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self._jumping = _JumpingSelector()
        super().__init__(self._jumping)

    def time(self) -> float:
        return self._jumping.now


def run_virtual[T](coroutine: Coroutine[Any, Any, T]) -> T:
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        return runner.run(coroutine)


@dataclass
class Seen:
    at: float
    body: dict[str, Any]
    headers: dict[str, str]


def event(chunk: dict[str, Any] | str) -> bytes:
    text = chunk if isinstance(chunk, str) else json.dumps(chunk)
    return f"data: {text}\n\n".encode()


def content_chunk(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


ROLE_CHUNK = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}


def usage_chunk(body: dict[str, Any], cached: int | None = 16, pod: str | None = POD) -> bytes:
    usage: dict[str, Any] = {
        "prompt_tokens": len(body["messages"][0]["content"].split(" ")) + TEMPLATE_TOKENS,
        "completion_tokens": body["max_tokens"],
    }
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    chunk: dict[str, Any] = {"choices": [], "usage": usage}
    if pod is not None:
        chunk["system_fingerprint"] = pod
    return event(chunk)


@dataclass
class FakeGateway:
    """Answers each call after ttft seconds, then ends the reply body_seconds later."""

    ttft: float = 2.0
    body_seconds: float = 3.0
    seen: list[Seen] = field(default_factory=list)
    in_flight: int = 0
    most_in_flight: int = 0
    # The answer to use instead of a stream, for the calls it returns one for.
    fail: Callable[[Seen], httpx.Response | Exception | None] | None = None
    # A reply's whole stream, instead of the default one.
    stream: Callable[[dict[str, Any]], AsyncIterator[bytes]] | None = None
    # A reply's response headers.
    reply_headers: dict[str, str] = field(default_factory=dict)

    async def _stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        await asyncio.sleep(self.ttft)
        yield event(ROLE_CHUNK)
        yield event(content_chunk("hello"))
        await asyncio.sleep(self.body_seconds)
        yield event(content_chunk(" world"))
        yield usage_chunk(body)
        yield event("[DONE]")
        self.in_flight -= 1

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen = Seen(
            at=asyncio.get_running_loop().time(),
            body=body,
            headers={k: v for k, v in request.headers.items() if k.startswith("x-")},
        )
        self.seen.append(seen)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        answer = None if self.fail is None else self.fail(seen)
        if answer is not None:
            self.in_flight -= 1
            if isinstance(answer, Exception):
                raise answer
            return answer
        stream = self._stream if self.stream is None else self.stream
        return httpx.Response(200, content=stream(body), headers=self.reply_headers)


def client_of(handler: Callable[..., Any]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler))


def trace_run(
    run_id: str = "run-a", prompts: tuple[int, ...] = (60, 80), gap_ms: int = 700
) -> TraceRun:
    calls = [
        TraceCall(
            step=step,
            expected_steps=2,
            gap_before_ms=0 if step == 1 else gap_ms,
            prompt_tokens=prompt,
            completion_tokens=5,
        )
        for step, prompt in enumerate(prompts, start=1)
    ]
    return TraceRun(run_id=run_id, task_type="csv", calls=calls)


def plan_of(
    runs: list[TraceRun] | None = None,
    count: int = 1,
    fan_out: FanOut | None = None,
    rate: float = 0.05,
) -> list[PlannedRun]:
    return build_plan(
        runs if runs is not None else [trace_run()],
        Pattern(count, rate, None, fan_out),
        SHARED,
        1,
        TEMPLATE_TOKENS,
    )


def ignore_run(_: list[dict[str, Any]]) -> None:
    pass


def replay(
    plan: list[PlannedRun], gateway: FakeGateway, deadline: float = DEADLINE_SECONDS
) -> list[dict[str, Any]]:
    async def go() -> list[dict[str, Any]]:
        async with client_of(gateway) as client:
            return await replay_runs(client, MODEL, plan, deadline, ignore_run)

    return run_virtual(go())


# What a call sends.


def test_a_call_goes_to_chat_completions_with_the_plans_body_and_headers() -> None:
    plan = plan_of()
    paths: list[str] = []
    gateway = FakeGateway()

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return await gateway(request)

    async def go() -> None:
        async with client_of(handler) as client:
            await replay_runs(client, MODEL, plan, DEADLINE_SECONDS, ignore_run)

    run_virtual(go())
    assert paths == ["/v1/chat/completions"] * 2
    first = gateway.seen[0]
    assert first.body["model"] == MODEL
    assert first.body["stream"] is True
    assert first.body["max_tokens"] == 5
    assert first.body["ignore_eos"] is True
    assert first.body["messages"][0]["content"] == plan[0].steps[0].content
    assert first.headers == {"x-run-id": plan[0].run_id, "x-step": "1", "x-expected-steps": "2"}
    Draft202012Validator(json.loads(HEADERS_SCHEMA.read_text())).validate(first.headers)


# Timing.


def test_a_gap_starts_at_the_previous_replys_end_not_at_a_fixed_offset() -> None:
    gateway = FakeGateway(ttft=2.0, body_seconds=3.0)
    replay(plan_of(), gateway)
    # Step 1 starts at 0 and ends at 5.0; 0.7 s of tool time later, step 2.
    assert gateway.seen[1].at == pytest.approx(5.7)


def test_a_slow_pod_delays_the_rest_of_the_run() -> None:
    gateway = FakeGateway(ttft=20.0, body_seconds=30.0)
    replay(plan_of(), gateway)
    assert gateway.seen[1].at == pytest.approx(50.7)


def test_runs_start_at_the_plans_times_whatever_the_others_are_doing() -> None:
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2)
    gateway = FakeGateway(ttft=500.0, body_seconds=500.0)
    replay(plan, gateway)
    first_calls = [seen.at for seen in gateway.seen if seen.headers["x-step"] == "1"]
    assert first_calls == pytest.approx([run.start_seconds for run in plan])


# Measurements.


def test_a_line_holds_the_replys_measurements() -> None:
    plan = plan_of()
    first, second = replay(plan, FakeGateway(ttft=2.0, body_seconds=3.0))
    assert first["schema_version"] == 1
    assert first["run_id"] == plan[0].run_id
    assert first["task_type"] == "csv"
    assert (first["step"], first["expected_steps"]) == (1, 2)
    assert first["gap_before_ms"] == 0
    assert first["ttft_ms"] == 2000
    assert first["duration_ms"] == 5000
    assert first["prompt_tokens"] == 60
    assert first["completion_tokens"] == 5
    assert first["cached_tokens"] == 16
    assert first["pod"] == POD
    assert first["ts_start"] > 0
    assert second["gap_before_ms"] == 700
    assert second["prompt_tokens"] == 80
    assert "parallel_group" not in first


def test_the_prompt_tokens_on_a_line_are_the_servers_not_the_plans() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield event(
            {
                "choices": [],
                "usage": {"prompt_tokens": 61, "completion_tokens": body["max_tokens"]},
            },
        )

    gateway = FakeGateway(stream=stream)
    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), gateway)[0]
    assert (line["prompt_tokens"], line["completion_tokens"]) == (61, 5)
    assert "duration_ms" in line
    assert "cached_tokens" not in line
    assert "pod" not in line


def test_a_gap_on_a_line_is_the_measured_one_not_the_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sleep = asyncio.sleep

    async def oversleeping(seconds: float) -> None:
        await real_sleep(seconds + 0.05)

    monkeypatch.setattr(asyncio, "sleep", oversleeping)
    lines = replay(plan_of(), FakeGateway())
    assert lines[1]["gap_before_ms"] == 750


def test_the_empty_role_chunk_is_not_the_first_token() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(ROLE_CHUNK)
        await asyncio.sleep(1.5)
        yield event(content_chunk("hello"))
        yield usage_chunk(body)

    gateway = FakeGateway(stream=stream)
    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), gateway)[0]
    assert line["ttft_ms"] == 1500


def test_a_reply_with_no_content_has_no_ttft_and_still_counts() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(ROLE_CHUNK)
        yield usage_chunk(body)

    gateway = FakeGateway(stream=stream)
    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), gateway)[0]
    assert "ttft_ms" not in line
    assert "duration_ms" in line


def test_every_line_is_valid_trace_v1_with_and_without_fan_out() -> None:
    validator = Draft202012Validator(json.loads(TRACE_SCHEMA.read_text()))
    for fan_out in (None, FanOut(1.0, 2)):
        for line in replay(plan_of(fan_out=fan_out), FakeGateway()):
            validator.validate(line)


# Order.


def test_lines_follow_the_plan_not_the_order_replies_ended() -> None:
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2)

    class EarlierEndsLater(FakeGateway):
        async def _stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
            # The first run's calls take far longer, so the second run finishes first.
            if body["messages"][0]["content"].startswith(plan[0].steps[0].content):
                await asyncio.sleep(1000.0)
            yield event(content_chunk("hello"))
            yield usage_chunk(body)

    lines = replay(plan, EarlierEndsLater())
    assert [(line["run_id"], line["step"]) for line in lines] == [
        (plan[0].run_id, 1),
        (plan[0].run_id, 2),
        (plan[1].run_id, 1),
        (plan[1].run_id, 2),
    ]


# Fan-out.


def test_fan_out_sends_k_calls_at_once_with_one_group_and_marks_them() -> None:
    gateway = FakeGateway()
    plan = plan_of(fan_out=FanOut(1.0, 3))
    lines = replay(plan, gateway)
    assert gateway.most_in_flight == 3
    second_step = [seen for seen in gateway.seen if seen.headers["x-step"] == "2"]
    assert len(second_step) == 3
    assert {seen.headers["x-parallel-group"] for seen in second_step} == {"step-2"}
    assert len({seen.at for seen in second_step}) == 1
    marked = [line for line in lines if line["step"] == 2]
    assert [line["parallel_group"] for line in marked] == ["step-2"] * 3
    assert all("parallel_group" not in line for line in lines if line["step"] == 1)


def test_the_next_step_waits_for_the_last_reply_of_a_group() -> None:
    class OneSlowBranch(FakeGateway):
        branch = 0

        async def _stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
            if body["messages"][0]["content"].count(" ") + 1 + TEMPLATE_TOKENS == 80:
                self.branch += 1
                await asyncio.sleep(100.0 if self.branch == 2 else 1.0)
            yield event(content_chunk("hello"))
            yield usage_chunk(body)

    gateway = OneSlowBranch()
    lines = replay(plan_of(runs=[trace_run(prompts=(60, 80, 90))], fan_out=FanOut(1.0, 2)), gateway)
    # Step 2 starts at 0.7 and its slowest branch ends 100 s later; 0.7 s after that, step 3.
    third_step = [seen for seen in gateway.seen if seen.headers["x-step"] == "3"]
    assert third_step[0].at == pytest.approx(101.4)
    # Its gap is measured from that last reply too, not from the branch that ended first.
    assert {line["gap_before_ms"] for line in lines if line["step"] == 3} == {700}


# Failures.


FAILURES: list[tuple[str, httpx.Response | Exception]] = [
    ("server error", httpx.Response(500)),
    ("timeout", httpx.ReadTimeout("no bytes")),
    ("no connection", httpx.ConnectError("refused")),
]


@pytest.mark.parametrize(("name", "answer"), FAILURES, ids=[name for name, _ in FAILURES])
def test_a_failed_call_ends_its_run_and_the_others_go_on(
    name: str, answer: httpx.Response | Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2)
    failing = plan[0].run_id

    def fail(seen: Seen) -> httpx.Response | Exception | None:
        return answer if seen.headers["x-run-id"] == failing else None

    gateway = FakeGateway(fail=fail)
    lines = replay(plan, gateway)
    assert [line["step"] for line in lines if line["run_id"] == failing] == [1]
    assert [line["step"] for line in lines if line["run_id"] == plan[1].run_id] == [1, 2]
    assert failed_calls(lines) == 1
    assert failing in capsys.readouterr().out


def test_a_failed_calls_line_holds_the_planned_tokens_and_no_duration() -> None:
    gateway = FakeGateway(fail=lambda seen: httpx.Response(503))
    line = replay(plan_of(), gateway)[0]
    assert (line["prompt_tokens"], line["completion_tokens"]) == (60, 5)
    assert "duration_ms" not in line
    assert "ttft_ms" not in line
    assert "cached_tokens" not in line
    assert "pod" not in line
    Draft202012Validator(json.loads(TRACE_SCHEMA.read_text())).validate(line)


def test_a_stream_cut_off_mid_reply_is_a_failed_call() -> None:

    async def stream(_: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        raise httpx.RemoteProtocolError("peer closed connection")

    gateway = FakeGateway(stream=stream)
    lines = replay(plan_of(), gateway)
    assert [line["step"] for line in lines] == [1]
    assert failed_calls(lines) == 1


@pytest.mark.parametrize(
    "last",
    [
        event("[DONE]"),
        event({"choices": [], "usage": {"completion_tokens": 3}}),
        event({"error": {"message": "the model crashed"}}),
        b"data: {not json\n\n",
    ],
    ids=["no usage", "usage without prompt tokens", "error chunk", "bad json"],
)
def test_a_reply_without_full_usage_is_a_failed_call(last: bytes) -> None:

    async def stream(_: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield last

    gateway = FakeGateway(stream=stream)
    assert failed_calls(replay(plan_of(), gateway)) == 1


@pytest.mark.parametrize("completion_tokens", [3, 7], ids=["short", "long"])
def test_a_reply_of_another_length_is_a_failed_call(
    completion_tokens: int, capsys: pytest.CaptureFixture[str]
) -> None:

    async def stream(_: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield event(
            {"choices": [], "usage": {"prompt_tokens": 60, "completion_tokens": completion_tokens}},
        )

    lines = replay(plan_of(), FakeGateway(stream=stream))
    assert [line["step"] for line in lines] == [1]
    assert failed_calls(lines) == 1
    assert lines[0]["completion_tokens"] == 5
    assert f"{completion_tokens} " in capsys.readouterr().out


MALFORMED = {
    "a list": b"data: [1, 2]\n\n",
    "a number": b"data: 5\n\n",
    "choices null": event({"choices": None}),
    "choices not a list": event({"choices": 5}),
    "a choice not an object": event({"choices": [5]}),
    "delta null": event({"choices": [{"delta": None}]}),
    "delta not an object": event({"choices": [{"delta": 5}]}),
    "content not a string": event({"choices": [{"delta": {"content": 5}}]}),
    "usage not an object": event({"choices": [], "usage": 5}),
    "usage without prompt tokens": event({"choices": [], "usage": {"completion_tokens": 5}}),
    "usage without completion tokens": event({"choices": [], "usage": {"prompt_tokens": 60}}),
    "tokens a string": event(
        {"choices": [], "usage": {"prompt_tokens": "60", "completion_tokens": 5}}
    ),
    "completion tokens null": event(
        {"choices": [], "usage": {"prompt_tokens": 60, "completion_tokens": None}}
    ),
    "tokens true": event({"choices": [], "usage": {"prompt_tokens": True, "completion_tokens": 5}}),
    "details not an object": event(
        {
            "choices": [],
            "usage": {"prompt_tokens": 60, "completion_tokens": 5, "prompt_tokens_details": 5},
        }
    ),
    "cached tokens a string": event(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 60,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": "16"},
            },
        }
    ),
    "pod a number": event({"choices": [], "system_fingerprint": 5}),
    "not utf-8": b"data: \xff\n\n",
}


@pytest.mark.parametrize("chunk", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_chunk_fails_its_call_and_the_other_run_goes_on(chunk: bytes) -> None:
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2)

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        if body["messages"][0]["content"] == plan[0].steps[0].content:
            yield chunk
        yield usage_chunk(body)

    lines = replay(plan, FakeGateway(stream=stream))
    assert [line["step"] for line in lines if line["run_id"] == plan[0].run_id] == [1]
    assert [line["step"] for line in lines if line["run_id"] == plan[1].run_id] == [1, 2]
    assert failed_calls(lines) == 1


def test_an_error_chunk_names_its_message_in_the_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:

    async def stream(_: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event({"error": {"message": "the model crashed"}})

    assert failed_calls(replay(plan_of(), FakeGateway(stream=stream))) == 1
    assert "the model crashed" in capsys.readouterr().out


def test_nulls_in_later_chunks_do_not_wipe_what_a_reply_has_given() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield usage_chunk(body)
        yield event({"choices": [], "usage": None, "system_fingerprint": None})

    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), FakeGateway(stream=stream))[0]
    assert "duration_ms" in line
    assert line["pod"] == POD
    assert line["cached_tokens"] == 16


def test_nulls_a_server_may_send_are_read_as_absent() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event({"choices": [{"delta": {"content": None}}], "usage": None, "error": None})
        await asyncio.sleep(1.0)
        yield event(content_chunk("hello"))
        usage = {
            "prompt_tokens": 60,
            "completion_tokens": body["max_tokens"],
            "prompt_tokens_details": None,
        }
        yield event({"choices": [], "usage": usage, "system_fingerprint": None})

    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), FakeGateway(stream=stream))[0]
    assert line["ttft_ms"] == 1000
    assert "cached_tokens" not in line
    assert "pod" not in line


def test_a_reply_without_a_fingerprint_takes_its_pod_from_the_simulators_header() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield usage_chunk(body, pod=None)

    gateway = FakeGateway(stream=stream, reply_headers={"x-inference-pod": "vllm-sim-1"})
    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), gateway)[0]
    assert line["pod"] == "vllm-sim-1"


def test_a_fingerprint_names_the_pod_even_with_the_header() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield usage_chunk(body)

    gateway = FakeGateway(stream=stream, reply_headers={"x-inference-pod": "vllm-sim-1"})
    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), gateway)[0]
    assert line["pod"] == POD


# Reading the stream.


@pytest.mark.parametrize("character", ["\u2028", "\u2029", "\u0085"], ids=["2028", "2029", "85"])
def test_a_line_separator_inside_a_token_does_not_end_the_line(character: str) -> None:
    """json.dumps escapes these by default; a server need not."""

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        chunk = content_chunk(f"a{character}b")
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        yield usage_chunk(body)

    lines = replay(plan_of(), FakeGateway(stream=stream))
    assert failed_calls(lines) == 0


def test_a_chunk_split_across_network_reads_is_read_whole() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        chunk = f"data: {json.dumps(content_chunk('é'), ensure_ascii=False)}\n\n".encode()
        middle = chunk.index("é".encode()) + 1
        yield chunk[:middle]
        await asyncio.sleep(1.0)
        yield chunk[middle:]
        yield usage_chunk(body)

    line = replay(plan_of(runs=[trace_run(prompts=(60,))]), FakeGateway(stream=stream))[0]
    assert line["ttft_ms"] == 1000
    assert "duration_ms" in line


def test_a_last_line_with_no_newline_is_still_read() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield usage_chunk(body).rstrip(b"\n")

    assert failed_calls(replay(plan_of(), FakeGateway(stream=stream))) == 0


def test_lines_ended_by_a_carriage_return_too_are_read() -> None:

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello")).replace(b"\n", b"\r\n")
        yield usage_chunk(body).replace(b"\n", b"\r\n")
        yield event("[DONE]").replace(b"\n", b"\r\n")

    assert failed_calls(replay(plan_of(), FakeGateway(stream=stream))) == 0


# The deadline of a call.


def test_a_reply_that_keeps_trickling_past_the_deadline_is_a_failed_call() -> None:
    """Each chunk would restart a read timeout, so only a deadline on the whole call ends this."""

    async def stream(_: dict[str, Any]) -> AsyncIterator[bytes]:
        for _second in range(1000):
            await asyncio.sleep(1.0)
            yield event(content_chunk("word"))

    gateway = FakeGateway(stream=stream)
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2, rate=1.0)

    async def go() -> tuple[list[dict[str, Any]], float]:
        async with client_of(gateway) as client:
            lines = await replay_runs(client, MODEL, plan, 10.0, ignore_run)
            return lines, asyncio.get_running_loop().time()

    lines, ended = run_virtual(go())
    assert failed_calls(lines) == 2
    assert [line["step"] for line in lines] == [1, 1]
    assert ended == pytest.approx(plan[1].start_seconds + 10.0)


def test_a_call_that_ends_inside_the_deadline_is_kept() -> None:
    gateway = FakeGateway(ttft=2.0, body_seconds=3.0)
    assert failed_calls(replay(plan_of(), gateway, deadline=5.5)) == 0


def test_the_deadline_counts_each_call_on_its_own() -> None:
    gateway = FakeGateway(ttft=2.0, body_seconds=3.0)
    lines = replay(plan_of(runs=[trace_run(prompts=(60, 80, 90))]), gateway, deadline=5.5)
    assert len(lines) == 3
    assert failed_calls(lines) == 0


# Keeping each run's lines as it ends.


def test_each_runs_lines_are_kept_as_that_run_ends_whichever_ends_first() -> None:
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2)

    class EarlierEndsLater(FakeGateway):
        async def _stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
            if body["messages"][0]["content"].startswith(plan[0].steps[0].content):
                await asyncio.sleep(1000.0)
            yield event(content_chunk("hello"))
            yield usage_chunk(body)

    kept: list[list[str]] = []

    def keep(lines: list[dict[str, Any]]) -> None:
        kept.append([line["run_id"] for line in lines])

    async def go() -> list[dict[str, Any]]:
        async with client_of(EarlierEndsLater()) as client:
            return await replay_runs(client, MODEL, plan, DEADLINE_SECONDS, keep)

    returned = run_virtual(go())
    assert kept == [[plan[1].run_id] * 2, [plan[0].run_id] * 2]
    assert [line["run_id"] for line in returned] == [plan[0].run_id] * 2 + [plan[1].run_id] * 2


def test_a_failed_calls_line_is_kept_with_its_run() -> None:
    plan = plan_of(runs=[trace_run("run-a")])

    def fail(seen: Seen) -> httpx.Response | None:
        return httpx.Response(500) if seen.headers["x-step"] == "2" else None

    kept: list[list[dict[str, Any]]] = []

    async def go() -> list[dict[str, Any]]:
        async with client_of(FakeGateway(fail=fail)) as client:
            return await replay_runs(client, MODEL, plan, DEADLINE_SECONDS, kept.append)

    returned = run_virtual(go())
    assert kept == [returned]
    assert failed_calls(kept[0]) == 1


def test_one_failed_branch_of_a_group_ends_the_run_after_the_others_finish() -> None:
    calls = {"step-2": 0}

    def fail(seen: Seen) -> httpx.Response | None:
        if seen.headers.get("x-parallel-group") == "step-2":
            calls["step-2"] += 1
            return httpx.Response(500) if calls["step-2"] == 1 else None
        return None

    gateway = FakeGateway(fail=fail)
    lines = replay(plan_of(runs=[trace_run(prompts=(60, 80, 90))], fan_out=FanOut(1.0, 2)), gateway)
    assert [line["step"] for line in lines] == [1, 2, 2]
    assert failed_calls(lines) == 1
    assert not [seen for seen in gateway.seen if seen.headers["x-step"] == "3"]


def test_no_failures_counts_zero() -> None:
    assert failed_calls(replay(plan_of(), FakeGateway())) == 0


# The end of a run (P3.3, D1).


@dataclass
class FakePicker:
    """Records each end of run and when it came; answers with fail's answer instead if given."""

    ends: list[tuple[str, str, float]] = field(default_factory=list)
    fail: httpx.Response | Exception | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.ends.append((request.method, request.url.path, asyncio.get_running_loop().time()))
        if isinstance(self.fail, Exception):
            raise self.fail
        return self.fail if self.fail is not None else httpx.Response(204)


def replay_ending(
    plan: list[PlannedRun], gateway: FakeGateway, picker: FakePicker
) -> list[dict[str, Any]]:
    """replay with the end of each run sent to picker."""

    async def go() -> list[dict[str, Any]]:
        async with (
            client_of(gateway) as client,
            httpx.AsyncClient(
                base_url="http://picker:9010", transport=httpx.MockTransport(picker)
            ) as picker_client,
        ):
            return await replay_runs(
                client, MODEL, plan, DEADLINE_SECONDS, ignore_run, picker_client
            )

    return run_virtual(go())


def test_each_run_ends_once_after_its_last_reply() -> None:
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2)
    picker = FakePicker()
    gateway = FakeGateway()
    lines = replay_ending(plan, gateway, picker)
    assert sorted(path for _, path, _ in picker.ends) == sorted(
        f"/runs/{run.run_id}/end" for run in plan
    )
    assert {method for method, _, _ in picker.ends} == {"POST"}
    for run in plan:
        (at,) = [at for _, path, at in picker.ends if path == f"/runs/{run.run_id}/end"]
        last = max(seen.at for seen in gateway.seen if seen.headers["x-run-id"] == run.run_id)
        # The last call was sent at last and took ttft plus body_seconds.
        assert at >= last + gateway.ttft + gateway.body_seconds
    assert failed_calls(lines) == 0


def test_a_run_that_stopped_on_a_failed_call_ends_too() -> None:
    plan = plan_of(runs=[trace_run("run-a")])
    picker = FakePicker()
    gateway = FakeGateway(fail=lambda seen: httpx.Response(500))
    lines = replay_ending(plan, gateway, picker)
    assert failed_calls(lines) == 1
    assert [path for _, path, _ in picker.ends] == [f"/runs/{plan[0].run_id}/end"]


@pytest.mark.parametrize(
    "fail",
    [httpx.Response(503), httpx.ConnectError("picker down"), httpx.ReadTimeout("picker slow")],
    ids=["503", "connection refused", "timeout"],
)
def test_an_end_of_run_the_picker_misses_changes_no_line(
    fail: httpx.Response | Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lost end is left to the pin's TTL: the replay's lines must not depend on it."""
    plan = plan_of(runs=[trace_run("run-a"), trace_run("run-b")], count=2)
    without = replay(plan, FakeGateway())
    lines = replay_ending(plan, FakeGateway(), FakePicker(fail=fail))
    strip = [{k: v for k, v in line.items() if k != "ts_start"} for line in lines]
    assert strip == [{k: v for k, v in line.items() if k != "ts_start"} for line in without]
    out = capsys.readouterr().out
    assert all(f"{run.run_id}: end of run not delivered" in out for run in plan)


# The run tracker (P3.8, D2).


def expected_run(run_id: str, expected: tuple[int, ...]) -> TraceRun:
    """A run of len(expected) calls, call n with x-expected-steps expected[n - 1]."""
    calls = [
        TraceCall(
            step=step,
            expected_steps=steps,
            gap_before_ms=0 if step == 1 else 700,
            prompt_tokens=60,
            completion_tokens=5,
        )
        for step, steps in enumerate(expected, start=1)
    ]
    return TraceRun(run_id=run_id, task_type="csv", calls=calls)


@dataclass
class TrackedGateway:
    """FakeGateway that notes, as each call arrives, its run and x-step with what the tracker
    reads then: (run, step, remaining work, active runs)."""

    tracker: RunTracker
    gateway: FakeGateway = field(default_factory=FakeGateway)
    tracked: list[tuple[str, int, int, int]] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.tracked.append(
            (
                request.headers["x-run-id"],
                int(request.headers["x-step"]),
                self.tracker.remaining_work(),
                self.tracker.active_runs(),
            )
        )
        return await self.gateway(request)


def replay_tracked(plan: list[PlannedRun], gateway: TrackedGateway) -> list[dict[str, Any]]:
    async def go() -> list[dict[str, Any]]:
        async with client_of(gateway) as client:
            return await replay_runs(
                client, MODEL, plan, DEADLINE_SECONDS, ignore_run, tracker=gateway.tracker
            )

    return run_virtual(go())


def test_the_tracker_counts_each_run_by_the_x_step_it_sends() -> None:
    plan = plan_of(runs=[expected_run("run-a", (4, 4, 4))])
    gateway = TrackedGateway(RunTracker())
    replay_tracked(plan, gateway)
    run_id = plan[0].run_id
    assert gateway.tracked == [(run_id, 1, 3, 1), (run_id, 2, 2, 1), (run_id, 3, 1, 1)]
    assert (gateway.tracker.remaining_work(), gateway.tracker.active_runs()) == (0, 0)


def test_a_run_past_its_expected_steps_still_counts_one() -> None:
    plan = plan_of(runs=[expected_run("run-a", (2, 2, 2))])
    gateway = TrackedGateway(RunTracker())
    replay_tracked(plan, gateway)
    assert [work for _, _, work, _ in gateway.tracked] == [1, 1, 1]


def test_a_step_with_other_expected_steps_changes_the_runs_work() -> None:
    """D13: the trace's later steps may carry the expected steps the scenario gave."""
    plan = plan_of(runs=[expected_run("run-a", (2, 5, 5))])
    gateway = TrackedGateway(RunTracker())
    replay_tracked(plan, gateway)
    assert [work for _, _, work, _ in gateway.tracked] == [1, 3, 2]


def test_a_run_is_counted_from_its_start_time_to_its_end() -> None:
    """Far apart, so the first run has ended before the second starts."""
    runs = [expected_run("run-a", (3, 3)), expected_run("run-b", (3, 3))]
    plan = plan_of(runs=runs, count=2, rate=0.0001)
    assert plan[1].start_seconds > 100
    gateway = TrackedGateway(RunTracker())
    replay_tracked(plan, gateway)
    assert [active for _, _, _, active in gateway.tracked] == [1, 1, 1, 1]
    assert gateway.tracker.active_runs() == 0


def test_runs_that_overlap_are_summed() -> None:
    """Both start within a second and the first call takes 50 s, so the second call to arrive is
    the other run's first, with both runs at step 1."""
    runs = [expected_run("run-a", (3, 3)), expected_run("run-b", (5, 5))]
    plan = plan_of(runs=runs, count=2, rate=1000.0)
    gateway = TrackedGateway(RunTracker())
    gateway.gateway.ttft = 50.0
    replay_tracked(plan, gateway)
    first, second = gateway.tracked[:2]
    assert (first[1], first[3]) == (1, 1)
    assert first[0] != second[0]
    assert second[1:] == (1, (3 - 1) + (5 - 1), 2)


def test_a_fanned_out_step_is_one_step() -> None:
    plan = plan_of(runs=[expected_run("run-a", (4, 4))], fan_out=FanOut(share=1.0, k=2))
    gateway = TrackedGateway(RunTracker())
    replay_tracked(plan, gateway)
    assert [(step, work) for _, step, work, _ in gateway.tracked] == [(1, 3), (2, 2), (2, 2)]


def test_a_run_that_stopped_on_a_failed_call_leaves_the_tracker() -> None:
    plan = plan_of(runs=[expected_run("run-a", (3, 3))])
    gateway = TrackedGateway(RunTracker(), FakeGateway(fail=lambda seen: httpx.Response(500)))
    lines = replay_tracked(plan, gateway)
    assert failed_calls(lines) == 1
    assert [(step, work, active) for _, step, work, active in gateway.tracked] == [(1, 2, 1)]
    assert gateway.tracker.active_runs() == 0


def get(port: int, path: str) -> tuple[int, str, str]:
    """(status, content type, body) of a GET to this machine."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, response.headers["Content-Type"], response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], ""


def test_serve_metrics_serves_the_trackers_text_until_the_block_ends() -> None:
    tracker = RunTracker()
    with serve_metrics(tracker, 0) as port:
        tracker.started("run-a", 4)
        assert get(port, "/metrics") == (200, METRICS_CONTENT_TYPE, tracker.exposition())
        tracker.ended("run-a")
        status, _, body = get(port, "/metrics")
        assert (status, body) == (200, tracker.exposition())
        assert "thesis_remaining_work 0\n" in body
        assert get(port, "/other")[0] == 404
    with pytest.raises(urllib.error.URLError):
        get(port, "/metrics")


def test_serve_metrics_on_a_port_in_use_fails_at_once() -> None:
    with socket.socket() as taken:
        taken.bind(("", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(OSError), serve_metrics(RunTracker(), port):
            pass


# The token check.


def tokenizer_of(count: Callable[[str], int]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tokenize"
        content = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json={"count": count(content)})

    return handler


def word_count(content: str) -> int:
    return (len(content.split(" ")) if content else 0) + TEMPLATE_TOKENS


def plan_with(handler: Callable[[httpx.Request], httpx.Response]) -> list[PlannedRun]:
    async def go() -> list[PlannedRun]:
        async with client_of(handler) as client:
            return await measure_template_and_plan(
                client, MODEL, [trace_run()], Pattern(1, 0.05, None, None), SHARED, 1
            )

    return run_virtual(go())


def test_the_template_is_measured_not_assumed() -> None:
    def fifteen(content: str) -> int:
        return (len(content.split(" ")) if content else 0) + 15

    plan = plan_with(tokenizer_of(fifteen))
    assert len(plan[0].steps[0].content.split(" ")) == 60 - 15


def test_counts_that_match_the_trace_give_the_plan() -> None:
    assert len(plan_with(tokenizer_of(word_count))) == 1


def test_a_prompt_that_does_not_tokenize_to_the_traces_length_is_refused() -> None:
    def off_by_one(content: str) -> int:
        return word_count(content) + (1 if content else 0)

    with pytest.raises(ReplayError, match="60"):
        plan_with(tokenizer_of(off_by_one))


# The output.


def test_the_output_is_named_after_the_trace_pattern_and_seed() -> None:
    path = output_path(Path("experiments/results/traces/record-demo-abc.jsonl"), "steady", 7)
    assert path == (
        REPO_ROOT / "experiments" / "results" / "replays" / "record-demo-abc-steady-7.jsonl"
    )


def test_an_invalid_line_stops_the_replay() -> None:
    validator = Draft202012Validator(json.loads(TRACE_SCHEMA.read_text()))
    with pytest.raises(ReplayError, match="run-a"):
        check_lines(validator, [{"run_id": "run-a", "step": 1}])


# make replay, with the cluster and both HTTP clients faked.


def trace_file(tmp_path: Path) -> Path:
    path = tmp_path / "trace.jsonl"
    lines = [
        {
            "schema_version": 1,
            "run_id": "run-a",
            "task_type": "csv",
            "step": step,
            "expected_steps": 2,
            "gap_before_ms": 0 if step == 1 else 1,
            "prompt_tokens": prompt,
            "completion_tokens": 2,
        }
        for step, prompt in enumerate((800, 900), start=1)
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


# The arguments of each client main opens on the gateway.
gateway_clients: list[dict[str, Any]] = []
# The tracker and port of each serve_metrics main opens; fake_cluster's stand-in binds no port.
metrics_served: list[tuple[RunTracker, int]] = []


@contextlib.contextmanager
def serve_nothing(tracker: RunTracker, port: int) -> Iterator[int]:
    metrics_served.append((tracker, port))
    yield port


def fake_cluster(
    monkeypatch: pytest.MonkeyPatch,
    out: Path,
    gateway: Callable[..., Any],
    trace: Path,
    picker: Callable[..., Any] | None = None,
    picker_port: str = "9010",
) -> list[str]:
    """main with kubectl answering a service's IP and the picker's end-of-run port (picker_port, ""
    for a Service without it), the gateway, the picker and the tokenizer each on its own fake by
    port, and virtual time: pattern steady comes from config/real.yaml, so its 20 runs are all the
    trace's one run repeated. Returns the kubectl commands; the arguments of the gateway's clients
    go to gateway_clients."""
    asked: list[str] = []
    gateway_clients.clear()
    metrics_served.clear()

    def kubectl(*args: str) -> str:
        asked.append(" ".join(args))
        if any("end-of-run" in arg for arg in args):
            return picker_port
        return "10.0.0.1"

    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        is_gateway = kwargs["base_url"].endswith(":80")
        if is_gateway:
            gateway_clients.append(kwargs)
        handler = gateway if is_gateway else tokenizer_of(word_count)
        if kwargs["base_url"].endswith(f":{picker_port}") and picker is not None:
            handler = picker
        return real_client(
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("load_generator.replay.kubectl", kubectl)
    monkeypatch.setattr("load_generator.replay.serve_metrics", serve_nothing)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(asyncio, "run", run_virtual)
    monkeypatch.setattr("sys.argv", ["replay", str(trace), "steady", "--out", str(out)])
    return asked


def test_main_refuses_an_existing_output_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    out.write_text("an earlier replay\n")
    gateway = FakeGateway()
    asked = fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    with pytest.raises(ReplayError, match="exists"):
        main()
    assert out.read_text() == "an earlier replay\n"
    assert gateway.seen == []
    assert asked == []


def test_main_refuses_a_pattern_the_profile_does_not_have(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_cluster(monkeypatch, tmp_path / "out.jsonl", FakeGateway(), trace_file(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        ["replay", str(trace_file(tmp_path)), "no-such", "--out", str(tmp_path / "o")],
    )
    with pytest.raises(ReplayError, match="no-such"):
        main()


def test_main_writes_the_lines_and_exits_0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "replays" / "out.jsonl"
    gateway = FakeGateway()
    asked = fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    main()
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 20 * 2
    assert all(line["task_type"] == "csv" for line in lines)
    assert any("inference-gateway" in command for command in asked)
    assert any(" vllm " in command for command in asked)


def test_main_with_the_plugin_off_sends_no_end_of_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    picker = FakePicker()
    asked = fake_cluster(
        monkeypatch, tmp_path / "out.jsonl", FakeGateway(), trace_file(tmp_path), picker
    )
    main()
    assert picker.ends == []
    assert not any("vllm-pool-epp" in command for command in asked)


def use_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, out: Path) -> None:
    """The tiny profile with the plugin on."""
    use_tiny_profile(monkeypatch, tmp_path, out)
    path = tmp_path / "config" / "tiny.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["routing"]["plugin"] = True
    path.write_text(yaml.safe_dump(profile))


def test_main_with_the_plugin_on_ends_every_run_at_the_pickers_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    picker = FakePicker()
    asked = fake_cluster(monkeypatch, out, FakeGateway(), trace_file(tmp_path), picker)
    use_plugin(monkeypatch, tmp_path, out)
    main()
    assert sorted(path for _, path, _ in picker.ends) == ["/runs/run-a-1/end", "/runs/run-a-2/end"]
    assert any("vllm-pool-epp" in command for command in asked)


def test_main_with_the_plugin_on_refuses_a_picker_with_no_end_of_run_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The profile says plugin on, the picker runs without it: no pin would ever be cleared."""
    out = tmp_path / "out.jsonl"
    gateway = FakeGateway()
    fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path), FakePicker(), picker_port="")
    use_plugin(monkeypatch, tmp_path, out)
    with pytest.raises(ReplayError, match="end-of-run"):
        main()
    assert gateway.seen == []


def test_main_sets_no_connection_limit_so_no_call_waits_in_the_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_cluster(monkeypatch, tmp_path / "out.jsonl", FakeGateway(), trace_file(tmp_path))
    main()
    assert [client["limits"].max_connections for client in gateway_clients] == [None]


def test_main_leaves_a_calls_time_to_its_deadline_not_to_the_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fake transport ignores a client's timeout, so a limit here would pass every other test
    and then cut real calls short of agent.llm_timeout_seconds."""
    fake_cluster(monkeypatch, tmp_path / "out.jsonl", FakeGateway(), trace_file(tmp_path))
    main()
    assert [client["timeout"] for client in gateway_clients] == [None]


def test_main_writes_the_lines_and_exits_1_when_a_call_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    gateway = FakeGateway(fail=lambda seen: httpx.Response(500))
    fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    assert len(out.read_text().splitlines()) == 20


def partial_of(out: Path) -> Path:
    return out.with_name(out.name + ".partial")


def test_main_removes_the_partial_file_once_the_output_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "replays" / "out.jsonl"
    fake_cluster(monkeypatch, out, FakeGateway(), trace_file(tmp_path))
    main()
    assert out.exists()
    assert not partial_of(out).exists()


def test_main_refuses_a_partial_file_left_by_a_replay_that_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    partial_of(out).write_text("a run that finished\n")
    gateway = FakeGateway()
    asked = fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    with pytest.raises(ReplayError, match="partial"):
        main()
    assert partial_of(out).read_text() == "a run that finished\n"
    assert not out.exists()
    assert gateway.seen == []
    assert asked == []


# A profile of its own, so that a test sees which of the profile's values main used.
TINY_SEED = 5
TINY_DEADLINE = 7.0
TINY_TRACKER_PORT = 9999


def use_tiny_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, out: Path) -> Path:
    (tmp_path / "config").mkdir(exist_ok=True)
    profile = {
        "model": {"name": "the-base-model", "lora": {"name": "the-adapter"}},
        "agent": {"llm_timeout_seconds": TINY_DEADLINE},
        "routing": {"plugin": False},
        "replay": {
            "seed": TINY_SEED,
            "gap_tolerance_ms": 50,
            "shared_prefix_tokens": {"all_types": 8, "by_type": {"csv": 20}},
            # Far apart, so that the first run has ended when the second starts.
            "patterns": {"only-here": {"runs": 2, "rate_per_second": 0.0001}},
            "tracker_port": TINY_TRACKER_PORT,
        },
    }
    (tmp_path / "config" / "tiny.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setattr("load_generator.replay.REPO_ROOT", tmp_path)
    trace = trace_file(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["replay", str(trace), "only-here", "--profile", "tiny", "--out", str(out)],
    )
    return trace


def tiny_plan(trace: Path, seed: int) -> list[PlannedRun]:
    return build_plan(
        load_trace(trace),
        Pattern(2, 0.0001, None, None),
        SharedPrefix(all_types=8, by_type={"csv": 20}),
        seed,
        TEMPLATE_TOKENS,
    )


def test_main_serves_the_tracker_the_runs_feed_on_the_profiles_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    seen_work: list[int] = []
    gateway = FakeGateway()

    async def tracked(request: httpx.Request) -> httpx.Response:
        ((tracker, _),) = metrics_served
        seen_work.append(tracker.remaining_work())
        return await gateway(request)

    fake_cluster(monkeypatch, out, tracked, trace_file(tmp_path))
    use_tiny_profile(monkeypatch, tmp_path, out)
    main()
    ((tracker, port),) = metrics_served
    assert port == TINY_TRACKER_PORT
    # trace_file's runs: two calls, expected steps 2, one run at a time.
    assert seen_work == [1, 1, 1, 1]
    assert tracker.active_runs() == 0


def test_main_stops_before_any_call_when_it_cannot_serve_metrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    gateway = FakeGateway()
    fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    use_tiny_profile(monkeypatch, tmp_path, out)

    @contextlib.contextmanager
    def port_in_use(tracker: RunTracker, port: int) -> Iterator[int]:
        raise OSError(98, "Address already in use")
        yield port

    monkeypatch.setattr("load_generator.replay.serve_metrics", port_in_use)
    with pytest.raises(OSError, match="in use"):
        main()
    assert gateway.seen == []
    assert not out.exists()


def test_main_uses_the_named_profiles_pattern_seed_and_served_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    gateway = FakeGateway()
    fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    trace = use_tiny_profile(monkeypatch, tmp_path, out)
    main()
    assert {seen.body["model"] for seen in gateway.seen} == {"the-adapter"}
    sent = [seen.body["messages"][0]["content"] for seen in gateway.seen]
    planned = [step.content for run in tiny_plan(trace, TINY_SEED) for step in run.steps]
    assert sent == planned


def test_main_replaces_the_profiles_seed_with_the_one_it_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    gateway = FakeGateway()
    fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    trace = use_tiny_profile(monkeypatch, tmp_path, out)
    monkeypatch.setattr("sys.argv", [*sys.argv, "--seed", "6"])
    main()
    sent = [seen.body["messages"][0]["content"] for seen in gateway.seen]
    planned = [step.content for run in tiny_plan(trace, 6) for step in run.steps]
    assert sent == planned
    assert planned != [step.content for run in tiny_plan(trace, TINY_SEED) for step in run.steps]


@pytest.mark.parametrize(
    ("ttft", "failed"),
    [(TINY_DEADLINE - 3.0, 0), (TINY_DEADLINE + 1.0, 2)],
    ids=["inside the profile's deadline", "past it"],
)
def test_a_calls_deadline_is_the_profiles_llm_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ttft: float, failed: int
) -> None:
    out = tmp_path / "out.jsonl"
    gateway = FakeGateway(ttft=ttft, body_seconds=1.0)
    fake_cluster(monkeypatch, out, gateway, trace_file(tmp_path))
    use_tiny_profile(monkeypatch, tmp_path, out)
    if failed:
        with pytest.raises(SystemExit):
            main()
    else:
        main()
    assert failed_calls(json.loads(line) for line in out.read_text().splitlines()) == failed


def test_main_writes_nothing_when_a_reply_breaks_the_trace_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A negative count is well typed, so the call counts as answered: only the check of the lines
    against trace.v1 can stop it. The partial file keeps what was measured."""

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield event(content_chunk("hello"))
        yield usage_chunk(body, cached=-1)

    out = tmp_path / "out.jsonl"
    fake_cluster(monkeypatch, out, FakeGateway(stream=stream), trace_file(tmp_path))
    with pytest.raises(ReplayError, match=r"trace\.v1"):
        main()
    assert not out.exists()
    assert len(partial_of(out).read_text().splitlines()) == 20 * 2


def test_a_replay_that_dies_leaves_the_finished_runs_in_the_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"

    def fail(seen: Seen) -> Exception | None:
        return RuntimeError("a bug") if seen.headers["x-run-id"].endswith("-2") else None

    fake_cluster(monkeypatch, out, FakeGateway(fail=fail), trace_file(tmp_path))
    use_tiny_profile(monkeypatch, tmp_path, out)
    with pytest.raises(ExceptionGroup):
        main()
    assert not out.exists()
    kept = [json.loads(line) for line in partial_of(out).read_text().splitlines()]
    assert [(line["run_id"], line["step"]) for line in kept] == [("run-a-1", 1), ("run-a-1", 2)]


def test_main_writes_the_output_in_the_plans_order_whichever_run_ended_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out.jsonl"
    trace = trace_file(tmp_path)
    planned = tiny_plan(trace, TINY_SEED)

    async def stream(body: dict[str, Any]) -> AsyncIterator[bytes]:
        # The first run outlasts the wait for the second, so the second ends first.
        if body["messages"][0]["content"].startswith(planned[0].steps[0].content):
            await asyncio.sleep(planned[1].start_seconds + 100.0)
        yield event(content_chunk("hello"))
        yield usage_chunk(body)

    fake_cluster(monkeypatch, out, FakeGateway(stream=stream), trace)
    use_tiny_profile(monkeypatch, tmp_path, out)
    # A deadline that lets the first run's slow calls finish.
    profile_path = tmp_path / "config" / "tiny.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["agent"]["llm_timeout_seconds"] = planned[1].start_seconds + 1000.0
    profile_path.write_text(yaml.safe_dump(profile))
    main()
    written = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["run_id"] for line in written] == ["run-a-1"] * 2 + ["run-a-2"] * 2
