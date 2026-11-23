"""Runs one task as a chain of LLM calls with the D1 headers, in either D10 mode, and records each
call (P2.2a). Step 1 picks the task's scenario, and later steps see only its tools (D13, P4.8)."""

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

import httpx

from agent_runtime.config import OUT_OF_SCOPE, AgentConfig, Scenario
from agent_runtime.csv_tools import Table
from agent_runtime.documents import Document
from agent_runtime.history import CountError, TokenCounter
from agent_runtime.llm import ChatReply, LLMClient, LLMResponseError
from agent_runtime.retry import send_with_retries
from agent_runtime.run_tracker import RunTracker
from agent_runtime.store import CallRecord
from agent_runtime.tools import (
    FINAL_ANSWER,
    InvalidCallError,
    check_call,
    run_call,
    scenario_tools,
    scenarios,
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

# Marks the saved assistant message that holds step 1's pick (D13): the step view shows it as step
# 1, and the later steps' prompts leave it out (_Run.prompt), so they read the system prompt, the
# tools, the history and the task, as the adapter was trained on.
SCENARIO_KEY = "scenario"

# Awaited with (step, invalid_calls, expected_steps, messages, calls) after every step that leaves
# the run going, so the service can save the history and the call records before the next call
# (D7); expected_steps changes once step 1 has picked the scenario (D13).
StepCallback = Callable[[int, int, int, list[dict[str, Any]], list[CallRecord]], Awaitable[None]]


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
    # The x-expected-steps the run ended on: its scenario's, once step 1 picked one.
    expected_steps: int
    messages: list[dict[str, Any]]
    # One per LLM call that got a reply, invalid and cut-off replies included (P2.2a).
    calls: list[CallRecord]


@dataclass(frozen=True)
class SavedRun:
    """What a run had saved when its runtime died, so another one continues it (P4.4): the history
    and the call records after its last saved step, the calls it had sent, its invalid calls."""

    messages: list[dict[str, Any]]
    step: int
    invalid_calls: int
    calls: list[CallRecord]


def initial_messages(
    task: str, config: AgentConfig, history: Sequence[Mapping[str, str]] = ()
) -> list[dict[str, Any]]:
    """The history a run starts from: the system prompt, the chat's earlier turns kept
    (agent_runtime/history.py), then the task. The system prompt and the turns come first, so the
    prompt's prefix is the same on every step of the run and from one turn to the next (D13)."""
    return [
        {"role": "system", "content": config.system_prompt},
        *(dict(message) for message in history),
        {"role": "user", "content": task},
    ]


def picked_scenario(messages: Sequence[Mapping[str, Any]]) -> Scenario | None:
    """The scenario step 1 picked, from the saved messages; None before step 1 has answered."""
    for message in messages:
        if message.get(SCENARIO_KEY):
            return cast(Scenario, message["content"])
    return None


class _Run:
    """State of one run while the loop works on it."""

    def __init__(
        self,
        run_id: str,
        task: str,
        config: AgentConfig,
        saved: SavedRun | None,
        history: Sequence[Mapping[str, str]],
    ) -> None:
        self.run_id = run_id
        self.config = config
        self.messages = (
            initial_messages(task, config, history) if saved is None else list(saved.messages)
        )
        # Number of LLM calls sent so far; 0 on the lines logged before the first call.
        self.step = 0 if saved is None else saved.step
        self.invalid_calls = 0 if saved is None else saved.invalid_calls
        self.calls: list[CallRecord] = [] if saved is None else list(saved.calls)
        self.scenario = picked_scenario(self.messages)
        # What a run sends until it knows its scenario (D13).
        self.expected_steps = (
            config.expected_steps_default
            if self.scenario is None or self.scenario == OUT_OF_SCOPE
            else config.scenario_expected_steps[self.scenario]
        )

    def prompt(self) -> list[dict[str, Any]]:
        """The messages the next call sends: the history without step 1's pick."""
        return [message for message in self.messages if SCENARIO_KEY not in message]

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
            expected_steps=self.expected_steps,
            messages=self.messages,
            calls=self.calls,
        )


@dataclass(frozen=True)
class Tracking:
    """The run tracker the run reports to (P3.8) and the counter of each call's prompt, which the
    tracker hears before the call is sent (P4.10, P4.8)."""

    tracker: RunTracker
    counter: TokenCounter


async def _send(
    run: _Run,
    llm: LLMClient,
    prompt: list[dict[str, Any]],
    definitions: list[dict[str, Any]],
    choice: Sequence[str] | None,
    headers: dict[str, str],
    seed: int | None,
    clock: Clock,
    tracking: Tracking | None,
) -> tuple[ChatReply, int, float, int]:
    """The step's call, sent again with the same history and x-step when its pod died
    (agent.llm_retries, P4.4), with the wall-clock and the monotonic start of the attempt that got
    the reply, and how many times it was sent again. A failed attempt adds nothing to the history,
    so the step view keeps one entry per x-step (P2.6). With tracking, each attempt first counts
    its prompt for thesis_context_tokens; the count goes to the same pods, so a pod that died fails
    it as it fails the call, and the same rule sends both again."""
    starts: list[tuple[int, float]] = []
    # Every attempt, one that failed on its count included: retries counts them all.
    attempts = 0

    async def attempt() -> ChatReply:
        nonlocal attempts
        attempts += 1
        if tracking is not None:
            tokens = await tracking.counter.count(prompt, definitions, add_generation_prompt=True)
            tracking.tracker.context(run.run_id, tokens)
        starts.append((clock.wall_ms(), clock.monotonic_ms()))
        return await llm.chat(prompt, definitions, headers, seed, choice)

    def retried(number: int, error: httpx.HTTPError) -> None:
        reason = f"{type(error).__name__}: {error}"
        run.log(logging.WARNING, "llm call retried", attempt=number, reason=reason)

    reply = await send_with_retries(attempt, run.config.retry, retried)
    ts_start, started_ms = starts[-1]
    return reply, ts_start, started_ms, attempts - 1


def _calls_that_ran(
    messages: list[dict[str, Any]], tools: Mapping[str, dict[str, Any]]
) -> set[tuple[str, str]]:
    """(tool, arguments as sorted JSON) of every call in the history that passed its schema, for
    REPEATED_CALL: the loop ran each such call, or answered it as a repeat of one that ran. A
    running run's history holds no final answer, which ends a run."""
    ran = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            name = call["function"]["name"]
            try:
                arguments = check_call(name, call["function"]["arguments"], tools)
            except InvalidCallError:
                continue
            ran.add((name, json.dumps(arguments, sort_keys=True)))
    return ran


def _last_reply_ms(calls: Sequence[CallRecord], clock: Clock) -> float | None:
    """When the last saved call's reply arrived, on this process's monotonic clock: its end on the
    wall clock, the only clock the dead runtime's records share with this one. Never after now,
    since the wall clock can step back (P2.3) and a gap below 0 breaks run-calls.v1."""
    if not calls:
        return None
    last = calls[-1]
    since_ms = clock.wall_ms() - (last.ts_start + last.duration_ms)
    return clock.monotonic_ms() - max(0, since_ms)


def _last_count(calls: Sequence[CallRecord]) -> int | None:
    """The prompt tokens of the latest saved call with a count: what the run held when its runtime
    died, as a reply with no count leaves the figure as it was (P4.10)."""
    counts = [call.prompt_tokens for call in calls if call.prompt_tokens is not None]
    return counts[-1] if counts else None


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
    tracking: Tracking | None = None,
    saved: SavedRun | None = None,
    history: Sequence[Mapping[str, str]] = (),
) -> RunResult:
    """tables and documents are the run request's files, already read
    (documents.read_run_files); history the chat's earlier turns kept, as messages
    (history.kept_history); clock times the calls (a test passes a fake one). tracking, when
    given, has a tracker that started the run and hears each call's x-step and prompt tokens as it
    is sent (P3.8, P4.10) and the scenario's expected steps once step 1 picked it (D13); the caller
    ends the run there. saved continues a run a dead runtime left (P4.4): the step it was on is
    sent again with its x-step, from the history saved before it.

    Step 1 sends no tools: its prompt ends with agent.scenario_prompt, and vLLM's structured
    outputs keep the reply to the scenarios the files can feed (tools.scenarios). out_of_scope
    ends the run with agent.out_of_scope_reply; any other scenario gives the later steps its tools.
    A reply outside the list fails the run, since vLLM's choice cannot give one: only a server
    that ignores structured outputs can, as the simulator does."""
    run = _Run(run_id, task, config, saved, history)
    choices = scenarios(tables, documents)
    tools: dict[str, dict[str, Any]] = {}
    if run.scenario is not None:
        tools = scenario_tools(run.scenario, config.tool_choice, tables, documents)
    definitions = tool_definitions(tools)
    tables_by_id = {table.id: table for table in tables}
    documents_by_id = {document.id: document for document in documents}
    # (tool, arguments as sorted JSON) of every call that ran, for REPEATED_CALL.
    ran = _calls_that_ran(run.messages, tools)
    max_invalid = f"{config.max_invalid_calls} invalid calls (agent.max_invalid_calls)"
    # When the last reply arrived, on clock.monotonic_ms(); None until the first one.
    last_reply_ms = _last_reply_ms(run.calls, clock)
    # Until a resumed run's first reply: its call record says a new runtime sent it (P4.6b).
    resuming = saved is not None
    if saved is None:
        run.log(logging.INFO, "run started", tool_choice=config.tool_choice)
    else:
        run.log(logging.INFO, "run resumed", tool_choice=config.tool_choice)
        held = _last_count(run.calls)
        if tracking is not None and held is not None:
            tracking.tracker.context(run_id, held)
    while run.step < config.max_steps:
        run.step += 1
        headers = {
            "x-run-id": run_id,
            "x-step": str(run.step),
            "x-expected-steps": str(run.expected_steps),
        }
        if tracking is not None:
            tracking.tracker.stepped(run_id, run.step)
        picking = run.scenario is None
        prompt = run.prompt()
        if picking:
            prompt.append({"role": "user", "content": config.scenario_prompt})
        try:
            reply, ts_start, started_ms, retries = await _send(
                run,
                llm,
                prompt,
                definitions,
                choices if picking else None,
                headers,
                seed,
                clock,
                tracking,
            )
        except (httpx.HTTPError, LLMResponseError, CountError) as error:
            # No reply, so no call record; run.step still counts the call and the error says why:
            # the last attempt's, after any retries, which the "llm call retried" lines count.
            return run.finish(error=f"LLM call failed: {type(error).__name__}: {error}")
        replied_ms = clock.monotonic_ms()
        call_record = CallRecord(
            step=run.step,
            ts_start=ts_start,
            duration_ms=round(replied_ms - started_ms),
            gap_before_ms=0 if last_reply_ms is None else round(started_ms - last_reply_ms),
            expected_steps=run.expected_steps,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            cached_tokens=reply.cached_tokens,
            pod=reply.pod,
            retries=retries or None,
            resumed=True if resuming else None,
        )
        resuming = False
        last_reply_ms = replied_ms
        run.calls.append(call_record)
        message = reply.message
        if picking:
            picked = message.get("content") or ""
            run.log(
                logging.INFO,
                "llm call",
                scenario=picked,
                finish_reason=reply.finish_reason,
                **call_record.fields(),
            )
            if picked not in choices:
                return run.finish(
                    error=f"step {run.step} picked {picked!r}, not one of {', '.join(choices)}"
                )
            run.messages.append({"role": "assistant", "content": picked, SCENARIO_KEY: True})
            if picked == OUT_OF_SCOPE:
                return run.finish(answer=config.out_of_scope_reply)
            run.scenario = cast(Scenario, picked)
            run.expected_steps = config.scenario_expected_steps[run.scenario]
            if tracking is not None:
                tracking.tracker.expected_changed(run_id, run.expected_steps)
            tools = scenario_tools(run.scenario, config.tool_choice, tables, documents)
            definitions = tool_definitions(tools)
            if on_step is not None:
                await on_step(
                    run.step,
                    run.invalid_calls,
                    run.expected_steps,
                    list(run.messages),
                    list(run.calls),
                )
            continue
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
            await on_step(
                run.step,
                run.invalid_calls,
                run.expected_steps,
                list(run.messages),
                list(run.calls),
            )
    return run.finish(error=f"{config.max_steps} calls without an answer (agent.max_steps)")
