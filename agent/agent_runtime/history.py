"""A chat's earlier turns in a run's prompt (D13, P4.8): each turn is the user's question and the
run's final answer, and the turns kept fit agent.history_budget_tokens.

Over the budget the oldest half of the kept turns is dropped. The rule is replayed from the chat's
first turn every time a run starts, so the app stores no cut: turn n+1's history starts where turn
n's did until a drop moves it, and the prompt's prefix stays the same from turn to turn, which keeps
the prefix cache's hits (D13).

Tokens are counted by vLLM's /tokenize on the pods' own Service (infra/manifests/agent.yaml): the
gateway's route matches only /v1, and the picker must not see these requests as calls."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx

TOKENIZE_PATH = "/tokenize"


class CountError(Exception):
    """A 2xx answer from /tokenize without a whole count."""


class TokenCounter(Protocol):
    async def count(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        add_generation_prompt: bool,
    ) -> int:
        """The tokens of messages and tools through the model's chat template."""
        ...


class VllmCounter:
    """POST /tokenize with the chat's messages, on the http client's base_url."""

    def __init__(self, http: httpx.AsyncClient, model: str) -> None:
        self._http = http
        self._model = model

    async def count(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        add_generation_prompt: bool,
    ) -> int:
        """A non-2xx answer raises httpx.HTTPStatusError, so a call's retry rule (P4.4) judges it as
        it judges the call's own answer."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "add_generation_prompt": add_generation_prompt,
            # As every call sends it (agent_runtime/llm.py): thinking is off (CLAUDE.md).
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            body["tools"] = list(tools)
        response = await self._http.post(TOKENIZE_PATH, json=body)
        response.raise_for_status()
        answer = response.json()
        count = answer.get("count") if isinstance(answer, dict) else None
        # A bool is an int to isinstance, but never a count.
        if type(count) is not int:
            raise CountError(f"/tokenize answered without a whole count: {answer!r}")
        return count


def turn_messages(turn: Mapping[str, str]) -> list[dict[str, str]]:
    """A run-request.v1 history turn as the two messages the prompt holds."""
    return [
        {"role": "user", "content": turn["question"]},
        {"role": "assistant", "content": turn["answer"]},
    ]


def first_kept(counts: Sequence[int], budget: int) -> int:
    """The index of the first turn kept, given each turn's tokens: the drop rule applied after every
    turn, as each turn's run applied it. A turn over the budget on its own is dropped too."""
    cut = 0
    for end in range(1, len(counts) + 1):
        while sum(counts[cut:end]) > budget:
            # The larger half of an odd count, so a single turn over the budget goes.
            cut += (end - cut + 1) // 2
    return cut


async def kept_history(
    history: Sequence[Mapping[str, str]], counter: TokenCounter, budget: int
) -> list[dict[str, str]]:
    """The messages of the turns kept, oldest first. Each turn is counted on its own, without a
    generation prompt: Qwen3's template marks every message's start and end with special tokens, so
    turns add up to what they take inside the prompt. Alone, a turn's answer also gets the empty
    thinking block the template gives an answer after the last question, so a
    count is a little over, never under."""
    counts = [await counter.count(turn_messages(turn), [], False) for turn in history]
    kept = history[first_kept(counts, budget) :]
    return [message for turn in kept for message in turn_messages(turn)]
