"""Sending a call again when its pod died (P4.4).

The agent runtime's rule, which the replayer imports as it does the run tracker (D2), so a replay
fails a call when the agent it replays would. A call is non-streaming for the agent (D6), so sending
it again with the same history and x-step is the same call; the picker has by then dropped the dead
pod and remaps the run (P3.4). Decided with the user, docs/task-cards/60-P4.4-failover.md.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class RetryPolicy:
    # Attempts after the first (agent.llm_retries).
    retries: int
    # The wait before each retry (agent.llm_retry_backoff_seconds).
    backoff_seconds: float


NO_RETRY = RetryPolicy(retries=0, backoff_seconds=0.0)


def _count(value: object) -> bool:
    """An int that is not a bool: True is an int to isinstance."""
    return type(value) is int


def retry_policy(agent: Mapping[str, Any]) -> RetryPolicy:
    """From a profile's agent section; a missing key raises KeyError."""
    retries, backoff = agent["llm_retries"], agent["llm_retry_backoff_seconds"]
    if not _count(retries) or retries < 0:
        raise ValueError(f"agent.llm_retries is {retries!r}, expected a count of 0 or more")
    if not (_count(backoff) or type(backoff) is float) or backoff < 0:
        raise ValueError(
            f"agent.llm_retry_backoff_seconds is {backoff!r}, expected seconds, 0 or more"
        )
    return RetryPolicy(retries=retries, backoff_seconds=float(backoff))


def retryable(error: httpx.HTTPError) -> bool:
    """A pod that died: no connection, a connection cut mid-reply, or a 5xx from the gateway, which
    answers so when it has no pod to send to or the picker restarts (P3.11). Not a timeout: a call
    past its deadline means overload, and sending it again adds load. Not a 4xx: the same request
    would get the same answer. Decided 2026-11-19."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500
    return isinstance(error, httpx.TransportError) and not isinstance(error, httpx.TimeoutException)


async def send_with_retries[T](
    send: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    on_retry: Callable[[int, httpx.HTTPError], None],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """send()'s result. A retryable failure is told to on_retry with the retry's number, from 1,
    then send() is awaited again after the backoff, up to policy.retries times; the last failure,
    or one that is not retryable, is raised. Each send() applies its own deadline."""
    attempt = 0
    while True:
        try:
            return await send()
        except httpx.HTTPError as error:
            if attempt >= policy.retries or not retryable(error):
                raise
            attempt += 1
            on_retry(attempt, error)
            await sleep(policy.backoff_seconds)
