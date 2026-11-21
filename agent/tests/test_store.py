"""RedisStore's commands (D7, P1.6a): the run's files key and its TTL, the calls key (P2.2a), the
set of running runs and the seed a resumed run needs (P4.4).

The client is a hand-written fake that records the commands it gets, so these tests check what is
sent to Redis, not Redis itself; the live P1.3b deployment runs them against a real Redis.
"""

import json
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any, cast

import pytest
from agent_runtime.store import CallRecord, RedisStore, RunRecord, RunStatus
from redis.asyncio import Redis

pytestmark = pytest.mark.anyio

TTL_SECONDS = 60
RUN_ID = "run-000000000001"
FILES = [{"id": "sales", "name": "sales.csv", "content": "region,revenue\nNorth,1\n"}]


class FakePipeline:
    def __init__(self, transaction: bool, redis: "FakeRedis") -> None:
        self.transaction = transaction
        self.commands: list[tuple[Any, ...]] = []
        self._redis = redis

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

    def sadd(self, key: str, member: str) -> None:
        self.commands.append(("sadd", key, member))

    def srem(self, key: str, member: str) -> None:
        self.commands.append(("srem", key, member))

    def hgetall(self, key: str) -> None:
        self.commands.append(("hgetall", key))

    def get(self, key: str) -> None:
        self.commands.append(("get", key))

    async def execute(self) -> list[Any]:
        """The reads answer from the fake's values; writes answer None."""
        self._redis.sent.append(("pipeline", self.transaction, self.commands))
        answers: dict[str, Callable[[str], Any]] = {
            "hgetall": self._redis.hashes.get,
            "get": self._redis.values.get,
        }
        return [
            answers[command[0]](command[1]) if command[0] in answers else None
            for command in self.commands
        ]


class FakeRedis:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, ...]] = []
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    def pipeline(self, transaction: bool) -> FakePipeline:
        return FakePipeline(transaction, self)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def exists(self, key: str) -> int:
        return int(key in self.hashes)

    async def srem(self, key: str, member: str) -> None:
        self.sent.append(("srem", key, member))
        self.sets.get(key, set()).discard(member)

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
        seed=7,
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


async def test_get_calls_reads_back_what_save_wrote() -> None:
    call = CallRecord(
        step=2,
        ts_start=1_793_600_000_000,
        duration_ms=812,
        gap_before_ms=40,
        expected_steps=3,
        prompt_tokens=398,
        completion_tokens=9,
        cached_tokens=352,
        pod="vllm-5c9565db76-6bq8s",
    )
    redis = FakeRedis()
    store = RedisStore(cast(Redis, redis), TTL_SECONDS)
    await store.save(record(), [], [call])
    [(_, _, commands)] = redis.sent
    key = f"thesis:run:{RUN_ID}:calls"
    [(_, _, value, _)] = [c for c in commands if c[:2] == ("set", key)]
    redis.values[key] = value
    assert await store.get_calls(RUN_ID) == [call.fields()]


async def test_get_calls_of_a_run_with_no_calls_key_is_empty() -> None:
    assert await RedisStore(cast(Redis, FakeRedis()), TTL_SECONDS).get_calls(RUN_ID) == []


RUNNING = "thesis:runs:running"


@pytest.mark.parametrize(
    ("status", "command"), [("running", "sadd"), ("done", "srem"), ("failed", "srem")]
)
async def test_save_keeps_the_set_of_running_runs_in_the_same_transaction(
    status: RunStatus, command: str
) -> None:
    """A runtime that starts finds the runs a dead one left in this set (P4.4)."""
    redis = FakeRedis()
    await RedisStore(cast(Redis, redis), TTL_SECONDS).save(replace(record(), status=status), [], [])
    [(_, transaction, commands)] = redis.sent
    assert transaction
    assert (command, RUNNING, RUN_ID) in commands


async def test_running_runs_are_those_still_saved_the_expired_dropped() -> None:
    redis = FakeRedis()
    redis.sets[RUNNING] = {"run-b", "run-a", "run-gone"}
    redis.hashes = {"thesis:run:run-a": {}, "thesis:run:run-b": {}}
    assert await RedisStore(cast(Redis, redis), TTL_SECONDS).running_runs() == ["run-a", "run-b"]
    assert redis.sets[RUNNING] == {"run-a", "run-b"}
    assert ("srem", RUNNING, "run-gone") in redis.sent


def saved_hash(redis: FakeRedis, run: RunRecord) -> None:
    """What save's HSET wrote for run, as Redis gives it back: strings, the None fields absent."""
    fields = {name: str(value) for name, value in asdict(run).items() if value is not None}
    redis.hashes[f"thesis:run:{run.run_id}"] = fields
    redis.values[f"thesis:run:{run.run_id}:messages"] = "[]"


@pytest.mark.parametrize("seed", [7, None])
async def test_get_reads_back_the_seed_a_resumed_run_sends(seed: int | None) -> None:
    redis = FakeRedis()
    run = replace(record(), seed=seed)
    saved_hash(redis, run)
    found = await RedisStore(cast(Redis, redis), TTL_SECONDS).get(RUN_ID)
    assert found == (run, [])


async def test_get_files_reads_back_what_save_files_wrote() -> None:
    redis = FakeRedis()
    store = RedisStore(cast(Redis, redis), TTL_SECONDS)
    await store.save_files(RUN_ID, FILES)
    [(_, key, value, _)] = redis.sent
    redis.values[key] = value
    assert await store.get_files(RUN_ID) == FILES


async def test_get_files_of_an_expired_run_is_none() -> None:
    assert await RedisStore(cast(Redis, FakeRedis()), TTL_SECONDS).get_files(RUN_ID) is None


def test_a_call_record_comes_back_from_its_saved_fields() -> None:
    call = CallRecord(
        step=2,
        ts_start=1_793_600_000_000,
        duration_ms=812,
        gap_before_ms=40,
        expected_steps=3,
        prompt_tokens=398,
        completion_tokens=9,
        cached_tokens=None,
        pod=None,
    )
    assert CallRecord.from_fields(call.fields()) == call


def test_a_call_sent_again_comes_back_from_its_saved_fields() -> None:
    call = CallRecord(
        step=2,
        ts_start=1_793_600_000_000,
        duration_ms=812,
        gap_before_ms=40,
        expected_steps=3,
        prompt_tokens=398,
        completion_tokens=9,
        cached_tokens=0,
        pod="vllm-1",
        retries=1,
        resumed=True,
    )
    assert call.fields()["retries"] == 1
    assert call.fields()["resumed"] is True
    assert CallRecord.from_fields(call.fields()) == call


def test_a_record_saved_before_p4_6b_reads_as_a_call_sent_once() -> None:
    """Records saved before the fields existed have neither (P4.6b)."""
    saved = {
        "step": 1,
        "ts_start": 1_793_600_000_000,
        "duration_ms": 812,
        "gap_before_ms": 0,
        "expected_steps": 3,
    }
    call = CallRecord.from_fields(saved)
    assert (call.retries, call.resumed) == (None, None)
    assert call.fields() == saved
