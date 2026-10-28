"""The agent runtime's HTTP API: start a run, read it back, health (P1.3a-2)."""

import json
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from redis.asyncio import Redis

from agent_runtime import logs
from agent_runtime.config import AgentConfig, load_config
from agent_runtime.csv_tools import FileError, Table, read_files
from agent_runtime.llm import LLMClient
from agent_runtime.loop import initial_messages, run_task
from agent_runtime.store import RedisStore, RunRecord, RunStore

# The port the agent Service targets (infra/manifests/agent.yaml, P1.3b).
PORT = 8080


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _arguments(raw: str) -> Any:
    """A call's arguments as JSON, or the raw text when the model wrote invalid JSON."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def steps_view(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per assistant message, i.e. per LLM call that answered, so entry n is x-step n.

    Each holds the reply's text, its tool calls with their results, and the error the runtime sent
    back when it rejected the reply (D10).
    """
    steps: list[dict[str, Any]] = []
    calls_by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
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


def create_app(
    config: AgentConfig,
    store: RunStore,
    http: httpx.AsyncClient,
    run_request_schema: dict[str, Any],
) -> FastAPI:
    llm = LLMClient(http, config)
    validator = Draft202012Validator(run_request_schema)
    app = FastAPI()

    async def execute(record: RunRecord, seed: int | None, tables: list[Table]) -> None:
        async def save_step(step: int, invalid_calls: int, messages: list[dict[str, Any]]) -> None:
            updated = replace(record, step=step, invalid_calls=invalid_calls, updated_ms=_now_ms())
            await store.save(updated, messages)

        result = await run_task(record.task, record.run_id, seed, config, llm, save_step, tables)
        final = replace(
            record,
            status=result.status,
            step=result.steps,
            invalid_calls=result.invalid_calls,
            answer=result.answer,
            error=result.error,
            updated_ms=_now_ms(),
        )
        await store.save(final, result.messages)

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
            tables = read_files(files, config.max_files, config.max_file_bytes)
        except FileError as error:
            raise HTTPException(422, f"files: {error}") from error
        now = _now_ms()
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
        )
        # Saved before 202, so a GET right after it finds the run instead of a 404. The files first,
        # so a saved run always has its files (P4.4 failover reads both).
        await store.save_files(record.run_id, files)
        await store.save(record, initial_messages(record.task, config))
        background.add_task(execute, record, body.get("seed"), tables)
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
            "steps": steps_view(messages),
        }

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
    store = RedisStore(redis, config.history_ttl_seconds)
    return create_app(config, store, http, json.loads(schema_path.read_text()))


def main() -> None:
    logs.configure()
    # log_config=None keeps uvicorn's own lines on the JSON handler that logs.configure set up.
    uvicorn.run(from_env(), host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
