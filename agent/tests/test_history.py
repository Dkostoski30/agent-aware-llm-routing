"""A chat's earlier turns cut to the token budget (D13, P4.8): the rule replayed turn by turn, and
the counts from vLLM's /tokenize.

The property tests draw their counts from seeded random.Random, so a failure repeats."""

import json
import random
from typing import Any

import httpx
import pytest
from agent_runtime.history import (
    CountError,
    VllmCounter,
    first_kept,
    kept_history,
    turn_messages,
)

pytestmark = pytest.mark.anyio

BUDGET = 100
# Turns per drawn chat and counts per turn: some turns alone are over the budget.
TURNS = 30
MAX_COUNT = 130
DRAWS = 200


def drawn(seed: int) -> list[int]:
    rng = random.Random(seed)
    return [rng.randint(1, MAX_COUNT) for _ in range(TURNS)]


def test_a_history_under_the_budget_is_kept_whole() -> None:
    assert first_kept([30, 30, 40], BUDGET) == 0


def test_over_the_budget_the_oldest_half_is_dropped() -> None:
    # After turn 4 the four turns hold 120: the oldest two go; turns 5 and 6 fit beside the rest.
    assert first_kept([30, 30, 30, 30], BUDGET) == 2
    assert first_kept([30, 30, 30, 30, 20, 10], BUDGET) == 2


def test_an_odd_count_of_turns_drops_the_larger_half() -> None:
    assert first_kept([40, 40, 40], BUDGET) == 2


def test_a_turn_alone_over_the_budget_is_dropped() -> None:
    assert first_kept([10, BUDGET + 1], BUDGET) == 2


@pytest.mark.parametrize("seed", range(DRAWS))
def test_the_kept_history_never_exceeds_the_budget(seed: int) -> None:
    counts = drawn(seed)
    for end in range(len(counts) + 1):
        assert sum(counts[first_kept(counts[:end], BUDGET) : end]) <= BUDGET


@pytest.mark.parametrize("seed", range(DRAWS))
def test_the_cut_moves_only_when_a_drop_happens(seed: int) -> None:
    """The prefix across turns: turn n+1's history starts where turn n's did, unless the new turn
    pushed it over the budget; then it starts later, never earlier."""
    counts = drawn(seed)
    for end in range(1, len(counts)):
        before, after = first_kept(counts[:end], BUDGET), first_kept(counts[: end + 1], BUDGET)
        if sum(counts[before : end + 1]) <= BUDGET:
            assert after == before
        else:
            assert after > before


def test_a_turn_is_its_question_then_its_answer() -> None:
    assert turn_messages({"question": "Hi?", "answer": "Hello."}) == [
        {"role": "user", "content": "Hi?"},
        {"role": "assistant", "content": "Hello."},
    ]


class Tokenizer:
    """vLLM's /tokenize, counting a turn's words; records every body it got."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status = status
        self.body = body

    def handle(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        self.bodies.append(sent)
        if self.status != 200 or self.body is not None:
            return httpx.Response(self.status, json=self.body)
        words = sum(len(message["content"].split()) for message in sent["messages"])
        return httpx.Response(200, json={"count": words, "max_model_len": 4096, "tokens": []})

    def counter(self) -> VllmCounter:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle), base_url="http://vllm")
        return VllmCounter(http, "test-model")


async def test_kept_history_counts_each_turn_and_keeps_the_newest_that_fit() -> None:
    tokenizer = Tokenizer()
    history = [
        {"question": "one two three", "answer": "four five"},
        {"question": "six", "answer": "seven"},
        {"question": "eight", "answer": "nine"},
    ]
    kept = await kept_history(history, tokenizer.counter(), budget=5)
    # 5 + 2 words fill 7 > 5 after turn 2, so turn 1 goes; turn 3 fits beside turn 2.
    assert kept == turn_messages(history[1]) + turn_messages(history[2])
    assert [body["messages"] for body in tokenizer.bodies] == [turn_messages(t) for t in history]


async def test_a_turn_is_counted_through_the_chat_template_without_a_generation_prompt() -> None:
    tokenizer = Tokenizer()
    await kept_history([{"question": "a", "answer": "b"}], tokenizer.counter(), budget=5)
    (body,) = tokenizer.bodies
    assert body["model"] == "test-model"
    assert body["add_generation_prompt"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in body


async def test_an_empty_history_asks_nothing() -> None:
    tokenizer = Tokenizer()
    assert await kept_history([], tokenizer.counter(), budget=5) == []
    assert tokenizer.bodies == []


async def test_a_prompt_is_counted_with_its_tools_and_the_generation_prompt() -> None:
    tokenizer = Tokenizer()
    tools = [{"type": "function", "function": {"name": "calculator"}}]
    messages = [{"role": "user", "content": "seven times eight"}]
    assert await tokenizer.counter().count(messages, tools, add_generation_prompt=True) == 3
    (body,) = tokenizer.bodies
    assert (body["tools"], body["add_generation_prompt"]) == (tools, True)


async def test_an_error_answer_raises_an_http_error() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await Tokenizer(status=503, body={"error": "down"}).counter().count([], [], True)


@pytest.mark.parametrize("body", [{"tokens": []}, {"count": "3"}, {"count": True}, []])
async def test_an_answer_without_a_whole_count_raises_count_error(body: Any) -> None:
    with pytest.raises(CountError):
        await Tokenizer(body=body).counter().count([], [], True)
