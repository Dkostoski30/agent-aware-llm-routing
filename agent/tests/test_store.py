"""RedisStore's commands (D7, P1.6a): the run's files key and its TTL, the calls key (P2.2a).

The client is a hand-written fake that records the commands it gets, so these tests check what is
sent to Redis, not Redis itself; the live P1.3b deployment runs them against a real Redis.
"""

import json
from typing import Any, cast

import pytest
from agent_runtime.store import CallRecord, RedisStore, RunRecord
from redis.asyncio import Redis

pytestmark = pytest.mark.anyio

TTL_SECONDS = 60
RUN_ID = "run-000000000001"
FILES = [{"id": "sales", "name": "sales.csv", "content": "region,revenue\nNorth,1\n"}]


class FakePipeline:
    def __init__(self, transaction: bool, sent: list[tuple[Any, ...]]) -> None:
        self.transaction = transaction
        self.commands: list[tuple[Any, ...]] = []
        self._sent = sent

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.commands.append(("hset", key, mapping))

    def set(self, key: str, value: str, ex: int) -> None:
        self.commands.append(("set", key, value, ex))

    def expire(self, key: str, seconds: int) -> None:
        self.commands.append(("expire", key, seconds))

    async def execute(self) -> list[Any]:
        self._sent.append(("pipeline", self.transaction, self.commands))
        return []


class FakeRedis:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, ...]] = []
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    def pipeline(self, transaction: bool) -> FakePipeline:
        return FakePipeline(transaction, self.sent)

    async def set(self, key: str, value: str, ex: int) -> None:
        self.sent.append(("set", key, value, ex))


def record() -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        status="running",
        task="Which region had the highest revenue?",
        step=1,
        expected_steps=3,
        invalid_calls=0,
        answer=None,
        error=None,
        created_ms=1,
        updated_ms=2,
    )


async def test_save_files_sets_the_files_key_with_the_runs_ttl() -> None:
    redis = FakeRedis()
    await RedisStore(cast(Redis, redis), TTL_SECONDS).save_files(RUN_ID, FILES)
    assert redis.sent == [("set", f"thesis:run:{RUN_ID}:files", json.dumps(FILES), TTL_SECONDS)]


async def test_save_renews_the_files_ttl_in_the_same_transaction() -> None:
    redis = FakeRedis()
    await RedisStore(cast(Redis, redis), TTL_SECONDS).save(record(), [], [])
    [(kind, transaction, commands)] = redis.sent
    assert (kind, transaction) == ("pipeline", True)
    key = f"thesis:run:{RUN_ID}"
    assert ("set", f"{key}:messages", "[]", TTL_SECONDS) in commands
    assert ("expire", key, TTL_SECONDS) in commands
    assert ("expire", f"{key}:files", TTL_SECONDS) in commands


async def test_save_writes_the_calls_with_the_runs_ttl_in_the_same_transaction() -> None:
    """One JSON list per run, saved whole with every step like the messages (P2.2a); a field the
    reply did not have is left out, not written as 0."""
    call = CallRecord(
        step=1,
        ts_start=1_793_600_000_000,
        duration_ms=812,
        gap_before_ms=0,
        expected_steps=3,
        prompt_tokens=120,
        completion_tokens=9,
        cached_tokens=None,
        pod="vllm-6f9c8d7b5-x2kqp",
    )
    redis = FakeRedis()
    await RedisStore(cast(Redis, redis), TTL_SECONDS).save(record(), [], [call])
    [(kind, transaction, commands)] = redis.sent
    assert (kind, transaction) == ("pipeline", True)
    [(_, _, value, ttl)] = [c for c in commands if c[:2] == ("set", f"thesis:run:{RUN_ID}:calls")]
    saved = {
        "step": 1,
        "ts_start": 1_793_600_000_000,
        "duration_ms": 812,
        "gap_before_ms": 0,
        "expected_steps": 3,
        "prompt_tokens": 120,
        "completion_tokens": 9,
        "pod": "vllm-6f9c8d7b5-x2kqp",
    }
    assert (json.loads(value), ttl) == ([saved], TTL_SECONDS)
