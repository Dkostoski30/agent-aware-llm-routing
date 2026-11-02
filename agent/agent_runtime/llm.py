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
    # usage.prompt_tokens, usage.completion_tokens and usage.prompt_tokens_details.cached_tokens;
    # None when the answer has no such field. The defaults are for a reply no server sent, such as
    # experiments/finetune_data.py's scripted ones, which have neither usage nor a pod.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    # system_fingerprint, the name of the pod that answered (infra/charts/vllm); None without one.
    pod: str | None = None


def _optional[T](parent: dict[str, Any], name: str, kind: type[T]) -> T | None:
    """parent[name], or None when the answer leaves it out or sends null.

    The type must match exactly: a bool is an int to isinstance, but never a token count.
    """
    value = parent.get(name)
    if value is None:
        return None
    if not isinstance(value, kind) or type(value) is not kind:
        raise LLMResponseError(
            f"not a chat completion: {name} is a {type(value).__name__}, not a {kind.__name__}"
        )
    return value


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
        """Returns choices[0] of the answer with its token counts and pod.

        A non-2xx answer or a timeout raises httpx.HTTPError, a body that is not a chat completion
        (a usage field of the wrong type included) LLMResponseError. httpx applies
        agent.llm_timeout_seconds to each phase of the call separately; a non-streaming answer
        arrives at once at the end, so it effectively bounds the whole call.
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
            answer = response.json()
            choice = answer["choices"][0]
            message, finish_reason = choice["message"], choice["finish_reason"]
        except (KeyError, IndexError, ValueError) as error:
            raise LLMResponseError(
                f"not a chat completion: {type(error).__name__}: {error}"
            ) from error
        usage = _optional(answer, "usage", dict) or {}
        details = _optional(usage, "prompt_tokens_details", dict) or {}
        return ChatReply(
            message=message,
            finish_reason=finish_reason,
            prompt_tokens=_optional(usage, "prompt_tokens", int),
            completion_tokens=_optional(usage, "completion_tokens", int),
            cached_tokens=_optional(details, "cached_tokens", int),
            pod=_optional(answer, "system_fingerprint", str),
        )
