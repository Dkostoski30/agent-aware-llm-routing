"""One non-streaming chat completion through the gateway (D6)."""

from dataclasses import dataclass
from typing import Any

import httpx

from agent_runtime.config import AgentConfig


class LLMResponseError(Exception):
    """A 2xx answer whose body is not a chat completion."""


@dataclass(frozen=True)
class ChatReply:
    message: dict[str, Any]
    # "stop", "tool_calls", or "length" when the reply hit model.max_model_len.
    finish_reason: str


class LLMClient:
    """Sends chat completions to the gateway that the http client's base_url points at."""

    def __init__(self, http: httpx.AsyncClient, config: AgentConfig) -> None:
        self._http = http
        self._config = config

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        headers: dict[str, str],
        seed: int | None,
    ) -> ChatReply:
        """Returns choices[0] of the answer.

        A non-2xx answer or a timeout raises httpx.HTTPError, a body that is not a chat completion
        LLMResponseError. httpx applies agent.llm_timeout_seconds to each phase of the call
        separately; a non-streaming answer arrives at once at the end, so it effectively bounds the
        whole call.
        """
        sampling = self._config.sampling
        body: dict[str, Any] = {
            "model": self._config.model_name,
            "messages": messages,
            "tools": tools,
            "tool_choice": self._config.tool_choice,
            # Non-streaming, so a call that fails can simply be sent again (D6).
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
        }
        if seed is not None:
            body["seed"] = seed
        response = await self._http.post(
            "/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=self._config.llm_timeout_seconds,
        )
        response.raise_for_status()
        try:
            choice = response.json()["choices"][0]
            reply = ChatReply(message=choice["message"], finish_reason=choice["finish_reason"])
        except (KeyError, IndexError, ValueError) as error:
            raise LLMResponseError(
                f"not a chat completion: {type(error).__name__}: {error}"
            ) from error
        return reply
