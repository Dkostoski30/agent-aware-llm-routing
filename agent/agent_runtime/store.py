"""Run history (D7): each run's metadata and full message list, saved after every step."""

import json
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


class RunStore(Protocol):
    async def save(self, record: RunRecord, messages: list[dict[str, Any]]) -> None: ...

    async def get(self, run_id: str) -> tuple[RunRecord, list[dict[str, Any]]] | None:
        """The run's last saved record and messages, or None if it is unknown or expired."""
        ...


_INT_FIELDS = ("step", "expected_steps", "invalid_calls", "created_ms", "updated_ms")


class RedisStore:
    """thesis:run:<id> is a hash of the record, thesis:run:<id>:messages the messages as JSON.

    Both are written in one MULTI transaction with the same TTL, so a reader (P4.4 failover on
    another pod) never sees a record and messages from different steps. The client must decode
    responses (decode_responses=True), so the hash comes back as strings.
    """

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def save(self, record: RunRecord, messages: list[dict[str, Any]]) -> None:
        key = f"thesis:run:{record.run_id}"
        # A hash holds strings only, so None fields (answer, error while running) are left out.
        # redis-py's own types: Mapping's key type is invariant, so mypy refuses dict[str, ...].
        fields: dict[FieldT, EncodableT] = {
            name: value for name, value in asdict(record).items() if value is not None
        }
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=fields)
            pipe.set(f"{key}:messages", json.dumps(messages), ex=self._ttl_seconds)
            pipe.expire(key, self._ttl_seconds)
            await pipe.execute()

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
