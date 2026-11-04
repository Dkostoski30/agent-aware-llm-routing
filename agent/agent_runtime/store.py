"""Run history (D7): each run's metadata, full message list and LLM call records, saved after every
step."""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

RunStatus = Literal["running", "done", "failed"]


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: RunStatus
    task: str
    # LLM calls sent so far; the x-step of the last call.
    step: int
    expected_steps: int
    invalid_calls: int
    answer: str | None
    error: str | None
    # Milliseconds since the Unix epoch, the unit of the trace's ts_start (schemas/trace.v1.json).
    created_ms: int
    updated_ms: int


@dataclass(frozen=True)
class CallRecord:
    """One LLM call that got a reply (P2.2a), named as in schemas/trace.v1.json where it has the
    field."""

    # The call's x-step.
    step: int
    # When the call was sent, in milliseconds since the Unix epoch.
    ts_start: int
    duration_ms: int
    # From the end of the previous reply to this call's start: the tools and the step's save (D9);
    # 0 on step 1.
    gap_before_ms: int
    # The x-expected-steps the call carried (D1).
    expected_steps: int
    # From the reply's usage; None when the reply has no such field. vLLM fills cached_tokens only
    # with --enable-prompt-tokens-details (infra/charts/vllm).
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    # The reply's system_fingerprint, which the vLLM chart sets to the pod's name; None without one.
    pod: str | None

    def fields(self) -> dict[str, int | str]:
        """The record as it is saved and logged: a measurement the reply lacked is left out, so it
        never reads as 0."""
        return {name: value for name, value in asdict(self).items() if value is not None}


class RunStore(Protocol):
    async def save(
        self, record: RunRecord, messages: list[dict[str, Any]], calls: Sequence[CallRecord]
    ) -> None: ...

    async def save_files(self, run_id: str, files: list[dict[str, str]]) -> None:
        """The run request's files, saved once before the run starts (D13)."""
        ...

    async def get(self, run_id: str) -> tuple[RunRecord, list[dict[str, Any]]] | None:
        """The run's last saved record and messages, or None if it is unknown or expired."""
        ...

    async def get_calls(self, run_id: str) -> list[dict[str, Any]]:
        """The run's call records as saved (CallRecord.fields); empty if it has none."""
        ...


_INT_FIELDS = ("step", "expected_steps", "invalid_calls", "created_ms", "updated_ms")


class RedisStore:
    """thesis:run:<id> is a hash of the record, thesis:run:<id>:messages the messages as JSON,
    thesis:run:<id>:calls the call records as a JSON list (CallRecord.fields), thesis:run:<id>:files
    the run request's files as JSON.

    The record, messages and calls are written in one MULTI transaction with the same TTL, so a
    reader (P4.4 failover on another pod) never sees them from different steps. The files
    are written once; every save renews their TTL with the run's, so they expire together. The
    client must decode responses (decode_responses=True), so the hash comes back as strings.
    """

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def save(
        self, record: RunRecord, messages: list[dict[str, Any]], calls: Sequence[CallRecord]
    ) -> None:
        key = f"thesis:run:{record.run_id}"
        # A hash holds strings only, so None fields (answer, error while running) are left out.
        # redis-py's own types: Mapping's key type is invariant, so mypy refuses dict[str, ...].
        fields: dict[FieldT, EncodableT] = {
            name: value for name, value in asdict(record).items() if value is not None
        }
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=fields)
            pipe.set(f"{key}:messages", json.dumps(messages), ex=self._ttl_seconds)
            calls_json = json.dumps([call.fields() for call in calls])
            pipe.set(f"{key}:calls", calls_json, ex=self._ttl_seconds)
            pipe.expire(key, self._ttl_seconds)
            pipe.expire(f"{key}:files", self._ttl_seconds)
            await pipe.execute()

    async def save_files(self, run_id: str, files: list[dict[str, str]]) -> None:
        key = f"thesis:run:{run_id}:files"
        await self._redis.set(key, json.dumps(files), ex=self._ttl_seconds)

    async def get(self, run_id: str) -> tuple[RunRecord, list[dict[str, Any]]] | None:
        key = f"thesis:run:{run_id}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hgetall(key)
            pipe.get(f"{key}:messages")
            fields, messages = await pipe.execute()
        if not fields or messages is None:
            return None
        values: dict[str, Any] = {name: fields.get(name) for name in RunRecord.__dataclass_fields__}
        for name in _INT_FIELDS:
            values[name] = int(values[name])
        return RunRecord(**values), json.loads(messages)

    async def get_calls(self, run_id: str) -> list[dict[str, Any]]:
        calls = await self._redis.get(f"thesis:run:{run_id}:calls")
        if calls is None:
            return []
        records: list[dict[str, Any]] = json.loads(calls)
        return records
