"""The agent runtime's HTTP API: start a run, read it back, stream its events, read its call
records, health (P1.3a-2, P1.4b-1, P2.6b). With the picker's plugin on, a run's end goes to the
picker once the run is saved (P3.3). The run tracker counts each run from its acceptance to its end,
and GET /metrics serves its remaining work to Prometheus (D2, P3.8). A runtime that starts resumes
every run the store holds as running, which a dead one left (P4.4). A run request's chat history is
cut to agent.history_budget_tokens before the run starts (D13, P4.8)."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from redis.asyncio import Redis

from agent_runtime import logs
from agent_runtime.config import AgentConfig, load_config
from agent_runtime.csv_tools import FileError, Table
from agent_runtime.documents import Document, read_run_files
from agent_runtime.history import CountError, TokenCounter, VllmCounter, kept_history
from agent_runtime.llm import LLMClient
from agent_runtime.loop import SYSTEM_CLOCK, SavedRun, Tracking, initial_messages, run_task
from agent_runtime.run_tracker import METRICS_CONTENT_TYPE, RunTracker
from agent_runtime.store import CallRecord, RedisStore, RunRecord, RunStore

# The port the agent Service targets (infra/manifests/agent.yaml, P1.3b).
PORT = 8080

logger = logging.getLogger(__name__)


async def end_run(picker: httpx.AsyncClient, run_id: str) -> None:
    """POST /runs/{id}/end (D1), which clears the run's pin. A failure is logged and left to the
    pin's TTL (routing.run_ttl_seconds): the run is saved already, and its answer must not depend on
    the picker."""
    try:
        response = await picker.post(f"/runs/{run_id}/end")
        response.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning(
            "end of run not delivered, the pin lasts until its TTL",
            extra={"run-id": run_id, "error": repr(error)},
        )


def _arguments(raw: str) -> Any:
    """A call's arguments as JSON, or the raw text when the model wrote invalid JSON."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def steps_view(messages: list[dict[str, Any]], history_messages: int) -> list[dict[str, Any]]:
    """One entry per assistant message after the system prompt and the history_messages of the
    chat's history (P4.8), i.e. per LLM call of the run that answered, so entry n is x-step n.

    Each holds the reply's text, its tool calls with their results, and the error the runtime sent
    back when it rejected the reply (D10).
    """
    steps: list[dict[str, Any]] = []
    calls_by_id: dict[str, dict[str, Any]] = {}
    for message in messages[1 + history_messages :]:
        if message["role"] == "assistant":
            calls_by_id = {
                call["id"]: {
                    "name": call["function"]["name"],
                    "arguments": _arguments(call["function"]["arguments"]),
                    "result": None,
                }
                for call in message.get("tool_calls") or []
            }
            steps.append(
                {
                    "step": len(steps) + 1,
                    "content": message["content"],
                    "tool_calls": list(calls_by_id.values()),
                    "error": None,
                }
            )
        elif message["role"] == "tool":
            calls_by_id[message["tool_call_id"]]["result"] = json.loads(message["content"])
        elif message["role"] == "user" and steps:
            steps[-1]["error"] = message["content"]
    return steps


async def run_events(
    store: RunStore, run_id: str, poll_seconds: float
) -> AsyncIterator[ServerSentEvent]:
    """The run's events (schemas/run-events.v1.json): run, one step per saved step, then end.

    Every poll reads the run from the store and sends the steps not sent yet, so a stream opened
    mid-run first replays the earlier steps. A step is saved whole, after its tools ran, so a sent
    step never changes. The stream ends after end, or without it once the run has expired.
    """
    found = await store.get(run_id)
    if found is None:
        return
    record, _ = found
    run = {
        "run_id": record.run_id,
        "task": record.task,
        "expected_steps": record.expected_steps,
        "created_ms": record.created_ms,
    }
    yield ServerSentEvent(event="run", data=run)
    sent = 0
    while found is not None:
        record, messages = found
        steps = steps_view(messages, record.history_messages)
        for step in steps[sent:]:
            fields = {"run_id": record.run_id, "expected_steps": record.expected_steps, **step}
            yield ServerSentEvent(event="step", data=fields)
        sent = len(steps)
        if record.status != "running":
            end = {
                "run_id": record.run_id,
                "status": record.status,
                "answer": record.answer,
                "error": record.error,
                "steps": record.step,
                "invalid_calls": record.invalid_calls,
                "elapsed_ms": record.updated_ms - record.created_ms,
            }
            yield ServerSentEvent(event="end", data=end)
            return
        await asyncio.sleep(poll_seconds)
        found = await store.get(run_id)


def create_app(
    config: AgentConfig,
    store: RunStore,
    http: httpx.AsyncClient,
    picker: httpx.AsyncClient,
    run_request_schema: dict[str, Any],
    tracker: RunTracker,
    counter: TokenCounter,
) -> FastAPI:
    llm = LLMClient(http, config)
    validator = Draft202012Validator(run_request_schema)
    tracking = Tracking(tracker, counter)

    async def execute(
        record: RunRecord,
        tables: list[Table],
        documents: list[Document],
        saved: SavedRun | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> None:
        """history: a new run's kept chat history, as messages; a resumed run has it in saved."""

        async def save_step(
            step: int,
            invalid_calls: int,
            expected_steps: int,
            messages: list[dict[str, Any]],
            calls: list[CallRecord],
        ) -> None:
            updated = replace(
                record,
                step=step,
                invalid_calls=invalid_calls,
                expected_steps=expected_steps,
                updated_ms=SYSTEM_CLOCK.wall_ms(),
            )
            await store.save(updated, messages, calls)

        # finally: a run that raises (Redis gone) ends in the tracker too, or the metric would count
        # it until the process restarts.
        try:
            result = await run_task(
                record.task,
                record.run_id,
                record.seed,
                config,
                llm,
                save_step,
                tables,
                documents,
                tracking=tracking,
                saved=saved,
                history=history or [],
            )
            final = replace(
                record,
                status=result.status,
                step=result.steps,
                invalid_calls=result.invalid_calls,
                expected_steps=result.expected_steps,
                answer=result.answer,
                error=result.error,
                updated_ms=SYSTEM_CLOCK.wall_ms(),
            )
            await store.save(final, result.messages, result.calls)
            if config.end_of_run:
                await end_run(picker, record.run_id)
        finally:
            tracker.ended(record.run_id)

    async def resume(run_id: str) -> None:
        """Continues a run a dead runtime left from what it saved; one that expired meanwhile is
        left alone. Its files are read again under this runtime's profile, whose limits a changed
        profile may have moved, so a run they now refuse fails with why."""
        found = await store.get(run_id)
        files = await store.get_files(run_id)
        if found is None or files is None:
            return
        record, messages = found
        calls = [CallRecord.from_fields(call) for call in await store.get_calls(run_id)]
        try:
            tables, documents = read_run_files(files, config.max_files, config.max_file_bytes)
        except FileError as error:
            failed = replace(
                record,
                status="failed",
                error=f"files of the resumed run: {error}",
                updated_ms=SYSTEM_CLOCK.wall_ms(),
            )
            await store.save(failed, messages, calls)
            return
        tracker.started(run_id, record.expected_steps)
        saved = SavedRun(messages, record.step, record.invalid_calls, calls)
        await execute(record, tables, documents, saved)

    # The resumed runs' tasks, held so the event loop does not drop them while they run.
    resumed: set[asyncio.Task[None]] = set()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Before the first request: with one runtime at a time (agent.yaml, strategy Recreate),
        every run still running in the store was left by a dead one."""
        for run_id in await store.running_runs():
            task = asyncio.create_task(resume(run_id))
            resumed.add(task)
            task.add_done_callback(resumed.discard)
        yield

    app = FastAPI(lifespan=lifespan)

    @app.post("/runs", status_code=202)
    async def start_run(request: Request, background: BackgroundTasks) -> dict[str, str]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise HTTPException(422, f"body is not JSON: {error}") from error
        violation = best_match(validator.iter_errors(body))
        if violation is not None:
            raise HTTPException(422, f"not a run-request.v1: {violation.message}")
        files = body.get("files", [])
        try:
            tables, documents = read_run_files(files, config.max_files, config.max_file_bytes)
        except FileError as error:
            raise HTTPException(422, f"files: {error}") from error
        try:
            history = await kept_history(
                body.get("history", []), counter, config.history_budget_tokens
            )
        except (httpx.HTTPError, CountError) as error:
            # Nothing is saved yet, so the app's message fails as when the runtime is unreachable.
            reason = f"history not counted: {type(error).__name__}: {error}"
            raise HTTPException(502, reason) from error
        now = SYSTEM_CLOCK.wall_ms()
        record = RunRecord(
            run_id=f"run-{secrets.token_hex(6)}",
            status="running",
            task=body["task"],
            step=0,
            expected_steps=config.expected_steps_default,
            invalid_calls=0,
            answer=None,
            error=None,
            created_ms=now,
            updated_ms=now,
            seed=body.get("seed"),
            history_messages=len(history),
        )
        # Saved before 202, so a GET right after it finds the run instead of a 404. The files first,
        # so a saved run always has its files (P4.4 failover reads both). No calls yet.
        await store.save_files(record.run_id, files)
        await store.save(record, initial_messages(record.task, config, history), [])
        # Counted from here, before its first call: the run's work has arrived (D2). It counts
        # agent.expected_steps_default until step 1 has picked its scenario (D13).
        tracker.started(record.run_id, record.expected_steps)
        background.add_task(execute, record, tables, documents, history=history)
        return {"run_id": record.run_id}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        found = await store.get(run_id)
        if found is None:
            raise HTTPException(404, f"unknown run {run_id}")
        record, messages = found
        return {
            "run_id": record.run_id,
            "status": record.status,
            "task": record.task,
            "answer": record.answer,
            "error": record.error,
            "expected_steps": record.expected_steps,
            "invalid_calls": record.invalid_calls,
            "steps": steps_view(messages, record.history_messages),
        }

    async def known_run(run_id: str) -> None:
        """Answers 404 before the response starts; once a stream has started, the status is already
        200."""
        if await store.get(run_id) is None:
            raise HTTPException(404, f"unknown run {run_id}")

    @app.get("/runs/{run_id}/calls", dependencies=[Depends(known_run)])
    async def get_calls(run_id: str) -> dict[str, Any]:
        """schemas/run-calls.v1.json: the records of the calls so far; the UI's badges read them
        (P2.6b). A step and its call are saved in one transaction, so a client that asks after a
        step event gets that step's call."""
        return {"run_id": run_id, "calls": await store.get_calls(run_id)}

    @app.get(
        "/runs/{run_id}/events",
        response_class=EventSourceResponse,
        dependencies=[Depends(known_run)],
    )
    async def stream_run(run_id: str) -> AsyncIterator[ServerSentEvent]:
        async for event in run_events(store, run_id, config.events_poll_seconds):
            yield event

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        """Prometheus scrapes this through the PodMonitor in infra/manifests/agent.yaml (P3.8)."""
        return PlainTextResponse(tracker.exposition(), media_type=METRICS_CONTENT_TYPE)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def from_env() -> FastAPI:
    """The app as the P1.3b Deployment runs it: addresses from the environment, tunables from the
    profile. The HTTP and Redis clients live as long as the process."""
    config = load_config(Path(os.environ["THESIS_CONFIG"]))
    schema_path = Path(os.environ["THESIS_SCHEMAS_DIR"]) / "run-request.v1.json"
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    http = httpx.AsyncClient(base_url=os.environ["GATEWAY_URL"])
    # httpx's default timeout, 5 s: the picker answers at once, and a missed end is left to the TTL.
    picker = httpx.AsyncClient(base_url=os.environ["PICKER_URL"])
    # The vLLM pods' own Service (agent_runtime/history.py), with the calls' deadline: /tokenize is
    # answered by the same pods' API server.
    tokenizer = httpx.AsyncClient(
        base_url=os.environ["TOKENIZE_URL"], timeout=config.llm_timeout_seconds
    )
    store = RedisStore(redis, config.history_ttl_seconds)
    schema = json.loads(schema_path.read_text())
    counter = VllmCounter(tokenizer, config.model_name)
    return create_app(config, store, http, picker, schema, RunTracker(), counter)


def main() -> None:
    logs.configure()
    # log_config=None keeps uvicorn's own lines on the JSON handler that logs.configure set up.
    uvicorn.run(from_env(), host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
