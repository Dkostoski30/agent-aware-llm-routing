"""The retry of a call whose pod died (P4.4): which failures are sent again, how often, and after
what wait. The agent runtime and the replayer both use it."""

from typing import Any

import httpx
import pytest
from agent_runtime.retry import (
    NO_RETRY,
    RetryPolicy,
    retry_policy,
    retryable,
    send_with_retries,
)

pytestmark = pytest.mark.anyio

REQUEST = httpx.Request("POST", "http://gateway/v1/chat/completions")


def status_error(code: int) -> httpx.HTTPStatusError:
    response = httpx.Response(code, request=REQUEST)
    return httpx.HTTPStatusError(f"status {code}", request=REQUEST, response=response)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.ReadError("connection reset"),
        status_error(500),
        status_error(502),
        status_error(503),
    ],
    ids=["refused", "cut mid-reply", "reset", "500", "502", "503"],
)
def test_a_dead_pod_or_a_gateway_5xx_is_retried(error: httpx.HTTPError) -> None:
    assert retryable(error)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("deadline"),
        httpx.ConnectTimeout("deadline"),
        status_error(400),
        status_error(404),
        status_error(422),
    ],
    ids=["read timeout", "connect timeout", "400", "404", "422"],
)
def test_a_timeout_or_a_4xx_is_not_retried(error: httpx.HTTPError) -> None:
    assert not retryable(error)


class Sends:
    """Fails with the scripted errors in turn, then answers "reply"; records the waits."""

    def __init__(self, *errors: httpx.HTTPError) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.waits: list[float] = []
        self.retries: list[tuple[int, str]] = []
        # What happened, in order: "send", "retry <n>", "wait <s>".
        self.events: list[str] = []

    async def send(self) -> str:
        self.calls += 1
        self.events.append("send")
        if self.errors:
            raise self.errors.pop(0)
        return "reply"

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.events.append(f"wait {seconds}")

    def on_retry(self, attempt: int, error: httpx.HTTPError) -> None:
        self.retries.append((attempt, str(error)))
        self.events.append(f"retry {attempt}")


async def send(sends: Sends, policy: RetryPolicy) -> Any:
    return await send_with_retries(sends.send, policy, sends.on_retry, sends.sleep)


async def test_a_call_that_answers_is_sent_once() -> None:
    sends = Sends()
    assert await send(sends, RetryPolicy(retries=2, backoff_seconds=5)) == "reply"
    assert (sends.calls, sends.waits, sends.retries) == (1, [], [])


async def test_a_dead_pod_is_retried_after_the_backoff_and_the_reply_returned() -> None:
    sends = Sends(httpx.ConnectError("refused"), status_error(503))
    assert await send(sends, RetryPolicy(retries=2, backoff_seconds=5)) == "reply"
    assert sends.events == ["send", "retry 1", "wait 5", "send", "retry 2", "wait 5", "send"]
    assert sends.retries == [(1, "refused"), (2, "status 503")]


async def test_out_of_retries_the_last_failure_is_raised() -> None:
    sends = Sends(status_error(500), status_error(502), status_error(503))
    with pytest.raises(httpx.HTTPStatusError, match="503"):
        await send(sends, RetryPolicy(retries=2, backoff_seconds=5))
    assert sends.calls == 3
    assert [attempt for attempt, _ in sends.retries] == [1, 2]


async def test_a_failure_that_is_not_retried_is_raised_at_once() -> None:
    sends = Sends(httpx.ReadTimeout("deadline"))
    with pytest.raises(httpx.ReadTimeout):
        await send(sends, RetryPolicy(retries=2, backoff_seconds=5))
    assert (sends.calls, sends.waits, sends.retries) == (1, [], [])


async def test_no_retry_sends_once() -> None:
    sends = Sends(httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        await send(sends, NO_RETRY)
    assert (sends.calls, sends.waits) == (1, [])


def test_the_policy_comes_from_the_agent_section() -> None:
    policy = retry_policy({"llm_retries": 2, "llm_retry_backoff_seconds": 5})
    assert policy == RetryPolicy(retries=2, backoff_seconds=5.0)


@pytest.mark.parametrize(
    ("retries", "backoff"),
    [(-1, 5), (1.5, 5), (True, 5), ("2", 5), (2, -1), (2, "5"), (2, False)],
)
def test_a_policy_that_is_not_a_count_and_a_wait_is_refused(retries: Any, backoff: Any) -> None:
    with pytest.raises(ValueError, match="llm_retr"):
        retry_policy({"llm_retries": retries, "llm_retry_backoff_seconds": backoff})
