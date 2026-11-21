"""Run history (D7): each run's metadata, full message list and LLM call records, saved after every
step, and the set of running runs, which a runtime that starts resumes (P4.4)."""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, cast

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
    # The run request's seed, which a resumed run keeps sending (P4.4); None when it had none.
    seed: int | None


@dataclass(frozen=True)
class CallRecord:
    """One LLM call that got a reply (P2.2a), named as in schemas/trace.v1.json and
    schemas/run-calls.v1.json."""

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
    # How many times the step was sent again before this reply because its pod died
    # (agent.llm_retries, P4.4); None when it was sent once, so a call sent once and a record saved
    # before P4.6b look the same.
    retries: int | None = None
    # True on the first call a runtime sent for a run it resumed from Redis (P4.4); None otherwise.
    resumed: Literal[True] | None = None

    def fields(self) -> dict[str, int | str | bool]:
        """The record as it is saved and logged: a measurement the reply lacked is left out, so it
        never reads as 0, and so are retries and resumed on a call they do not describe."""
        return {name: value for name, value in asdict(self).items() if value is not None}

    @classmethod
    def from_fields(cls, fields: dict[str, Any]) -> "CallRecord":
        """A record as fields saved it; what it left out comes back as None."""
        values: dict[str, Any] = {name: fields.get(name) for name in cls.__dataclass_fields__}
        return cls(**values)


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

    async def get_files(self, run_id: str) -> list[dict[str, str]] | None:
        """The run request's files as saved, or None if the run expired."""
        ...

    async def running_runs(self) -> list[str]:
        """The runs last saved as running that have not expired, by id."""
        ...


# The set of runs last saved as running. With one runtime at a time (infra/manifests/agent.yaml,
# strategy Recreate), a run in it when a runtime starts was left by a dead one (P4.4).
RUNNING_KEY = "thesis:runs:running"

_INT_FIELDS = ("step", "expected_steps", "invalid_calls", "created_ms", "updated_ms")


class RedisStore:
    """thesis:run:<id> is a hash of the record, thesis:run:<id>:messages the messages as JSON,
    thesis:run:<id>:calls the call records as a JSON list (CallRecord.fields), thesis:run:<id>:files
    the run request's files as JSON, and thesis:runs:running (RUNNING_KEY) the ids of the running
    runs.

    The record, messages and calls are written in one MULTI transaction with the same TTL, with the
    run's place in the running set, so a reader (a runtime resuming the run, P4.4) never sees them
    from different steps. The set has no TTL of its own: running_runs drops a run whose keys
    expired. The files
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
            if record.status == "running":
                pipe.sadd(RUNNING_KEY, record.run_id)
            else:
                pipe.srem(RUNNING_KEY, record.run_id)
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
        if values["seed"] is not None:
            values["seed"] = int(values["seed"])
        return RunRecord(**values), json.loads(messages)

    async def get_calls(self, run_id: str) -> list[dict[str, Any]]:
        calls = await self._redis.get(f"thesis:run:{run_id}:calls")
        if calls is None:
            return []
        records: list[dict[str, Any]] = json.loads(calls)
        return records

    async def get_files(self, run_id: str) -> list[dict[str, str]] | None:
        files = await self._redis.get(f"thesis:run:{run_id}:files")
        if files is None:
            return None
        saved: list[dict[str, str]] = json.loads(files)
        return saved

    async def running_runs(self) -> list[str]:
        alive = []
        # Strings: the client decodes responses (the class docstring).
        members = cast(set[str], await self._redis.smembers(RUNNING_KEY))
        for run_id in sorted(members):
            if await self._redis.exists(f"thesis:run:{run_id}"):
                alive.append(run_id)
            else:
                await self._redis.srem(RUNNING_KEY, run_id)
        return alive
