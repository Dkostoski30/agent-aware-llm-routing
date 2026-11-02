"""Runs one task as a chain of LLM calls with the D1 headers, in either D10 mode, and records each
call (P2.2a)."""

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from agent_runtime.config import AgentConfig
from agent_runtime.csv_tools import Table
from agent_runtime.documents import Document
from agent_runtime.llm import LLMClient, LLMResponseError
from agent_runtime.store import CallRecord
from agent_runtime.tools import (
    FINAL_ANSWER,
    InvalidCallError,
    check_call,
    offered_tools,
    run_call,
    tool_definitions,
)

logger = logging.getLogger(__name__)

# Opening tag of a tool call in Qwen3's output format (vLLM's hermes parser). vLLM leaves the call
# in the text when it cannot parse it, so a text answer that contains the tag is a broken call, not
# an answer (D10).
TOOL_CALL_MARKER = "<tool_call>"

# The tools are deterministic, so a call with the same tool and arguments as an earlier one in the
# run can only return what the history already holds; it is answered with this instead of run again.
# The demo model repeated draft_reply until agent.max_steps instead of answering (P2.1a-2 demo run,
# 2026-11-01).
REPEATED_CALL = (
    "{name} was already called with these arguments in this run and its result is above; use that "
    "result, or give the final answer"
)

# Awaited with (step, invalid_calls, messages, calls) after every step that leaves the run going, so
# the service can save the history and the call records before the next call (D7).
StepCallback = Callable[[int, int, list[dict[str, Any]], list[CallRecord]], Awaitable[None]]


class Clock(Protocol):
    def wall_ms(self) -> int:
        """Milliseconds since the Unix epoch, for when a call was sent (ts_start)."""
        ...

    def monotonic_ms(self) -> float:
        """Milliseconds from an arbitrary start that never go back, for durations and gaps, which a
        wall clock set back by NTP could make negative."""
        ...


class SystemClock:
    def wall_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic_ms(self) -> float:
        return time.perf_counter() * 1000


SYSTEM_CLOCK = SystemClock()


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: Literal["done", "failed"]
    answer: str | None
    error: str | None
    steps: int
    invalid_calls: int
    messages: list[dict[str, Any]]
    # One per LLM call that got a reply, invalid and cut-off replies included (P2.2a).
    calls: list[CallRecord]


def initial_messages(task: str, config: AgentConfig) -> list[dict[str, Any]]:
    """The history a run starts from: the system prompt and the task."""
    return [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": task},
    ]


class _Run:
    """State of one run while the loop works on it."""

    def __init__(self, run_id: str, task: str, config: AgentConfig) -> None:
        self.run_id = run_id
        self.config = config
        self.messages = initial_messages(task, config)
        # Number of LLM calls sent so far; 0 on the lines logged before the first call.
        self.step = 0
        self.invalid_calls = 0
        self.calls: list[CallRecord] = []

    def log(self, level: int, message: str, **fields: Any) -> None:
        logger.log(level, message, extra={"run-id": self.run_id, "step": self.step, **fields})

    def invalid(self, reason: str) -> bool:
        """Counts an invalid call; True once the run has reached agent.max_invalid_calls."""
        self.invalid_calls += 1
        self.log(logging.WARNING, "invalid call", reason=reason, invalid_calls=self.invalid_calls)
        return self.invalid_calls >= self.config.max_invalid_calls

    def finish(self, answer: str | None = None, error: str | None = None) -> RunResult:
        status: Literal["done", "failed"] = "failed" if error is not None else "done"
        self.log(logging.INFO, f"run {status}", error=error)
        return RunResult(
            run_id=self.run_id,
            status=status,
            answer=answer,
            error=error,
            steps=self.step,
            invalid_calls=self.invalid_calls,
            messages=self.messages,
            calls=self.calls,
        )


def _tool_message(call_id: str, content: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(content)}


async def run_task(
    task: str,
    run_id: str,
    seed: int | None,
    config: AgentConfig,
    llm: LLMClient,
    on_step: StepCallback | None = None,
    tables: Sequence[Table] = (),
    documents: Sequence[Document] = (),
    clock: Clock = SYSTEM_CLOCK,
) -> RunResult:
    """tables and documents are the run request's files, already read
    (documents.read_run_files); clock times the calls (a test passes a fake one)."""
    run = _Run(run_id, task, config)
    tools = offered_tools(config.tool_choice, tables, documents)
    definitions = tool_definitions(tools)
    tables_by_id = {table.id: table for table in tables}
    documents_by_id = {document.id: document for document in documents}
    # (tool, arguments as sorted JSON) of every call that ran, for REPEATED_CALL.
    ran: set[tuple[str, str]] = set()
    max_invalid = f"{config.max_invalid_calls} invalid calls (agent.max_invalid_calls)"
    expected_steps = config.expected_steps_default
    # When the last reply arrived, on clock.monotonic_ms(); None until the first one.
    last_reply_ms: float | None = None
    run.log(logging.INFO, "run started", tool_choice=config.tool_choice)
    while run.step < config.max_steps:
        run.step += 1
        headers = {
            "x-run-id": run_id,
            "x-step": str(run.step),
            "x-expected-steps": str(expected_steps),
        }
        ts_start = clock.wall_ms()
        started_ms = clock.monotonic_ms()
        try:
            reply = await llm.chat(run.messages, definitions, headers, seed)
        except (httpx.HTTPError, LLMResponseError) as error:
            # No reply, so no call record; run.step still counts the call and the error says why.
            return run.finish(error=f"LLM call failed: {type(error).__name__}: {error}")
        replied_ms = clock.monotonic_ms()
        call_record = CallRecord(
            step=run.step,
            ts_start=ts_start,
            duration_ms=round(replied_ms - started_ms),
            gap_before_ms=0 if last_reply_ms is None else round(started_ms - last_reply_ms),
            expected_steps=expected_steps,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            cached_tokens=reply.cached_tokens,
            pod=reply.pod,
        )
        last_reply_ms = replied_ms
        run.calls.append(call_record)
        message = reply.message
        tool_calls: list[dict[str, Any]] = message.get("tool_calls") or []
        # Only what the next call needs; the other fields vLLM returns stay out of the history.
        assistant: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        run.messages.append(assistant)
        run.log(
            logging.INFO,
            "llm call",
            tool_calls=[c["function"]["name"] for c in tool_calls],
            finish_reason=reply.finish_reason,
            **call_record.fields(),
        )
        # With no max_tokens cap a cut-off reply filled the context, so a retry can only overflow
        # again.
        if reply.finish_reason == "length":
            return run.finish(
                error="the reply was cut off at model.max_model_len (finish_reason length)"
            )

        if not tool_calls:
            content: str = message.get("content") or ""
            if config.tool_choice == "auto" and content and TOOL_CALL_MARKER not in content:
                return run.finish(answer=content)
            if config.tool_choice == "auto" and not content:
                reason = "your answer is empty; call the tool, or answer in plain text"
            elif config.tool_choice == "auto":
                # The marker itself stays out of the message, so the model is not shown the tag to
                # repeat.
                reason = (
                    "your answer contains a tool call that could not be parsed; call the tool "
                    "again with valid JSON arguments, or answer in plain text"
                )
            else:
                reason = f"answer with a call to one of the tools: {', '.join(tools)}"
            run.messages.append({"role": "user", "content": f"Error: {reason}"})
            if run.invalid(reason):
                return run.finish(error=max_invalid)

        # Several calls in one response run in order and stay one step.
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                arguments = check_call(name, call["function"]["arguments"], tools)
            except InvalidCallError as error:
                run.messages.append(_tool_message(call["id"], {"error": str(error)}))
                if run.invalid(str(error)):
                    return run.finish(error=max_invalid)
                continue
            if name == FINAL_ANSWER:
                return run.finish(answer=arguments["answer"])
            key = (name, json.dumps(arguments, sort_keys=True))
            if key in ran:
                # Fits the schema, so not an invalid call (D10); logged to count how often it helps.
                run.log(logging.WARNING, "repeated call", tool=name)
                repeated = {"error": REPEATED_CALL.format(name=name)}
                run.messages.append(_tool_message(call["id"], repeated))
                continue
            ran.add(key)
            result = run_call(name, arguments, tables_by_id, documents_by_id, config)
            if "error" in result:
                # A valid call the data cannot answer: not an invalid call (D10), but P1.6b counts
                # these to tell failures on the data from failures on the schema.
                run.log(logging.WARNING, "tool error", tool=name, error=result["error"])
            run.messages.append(_tool_message(call["id"], result))
        if on_step is not None:
            # Copies: the loop keeps appending to both lists while the callback's caller holds them.
            await on_step(run.step, run.invalid_calls, list(run.messages), list(run.calls))
    return run.finish(error=f"{config.max_steps} calls without an answer (agent.max_steps)")
