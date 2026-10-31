"""Rendering a generated training example into the prompt vLLM would build and the text the model
must write after it (P1.7 step 2), with the served model's own chat template."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from render import GENERATION_PROMPT, render, vllm_messages
from transformers import AutoTokenizer, PreTrainedTokenizerBase

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())

CALCULATOR = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Applies one arithmetic operation to two numbers: a op b.",
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["op", "a", "b"],
            "additionalProperties": False,
        },
    },
}
FINAL_ANSWER = {
    "type": "function",
    "function": {
        "name": "final_answer",
        "description": "Ends the task with the answer for the user.",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string", "minLength": 1}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}
START = [
    {"role": "system", "content": "You are a data analysis agent."},
    {"role": "user", "content": "What is 7 times 8 plus 3?"},
]


def call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """An assistant message with one tool call, as the agent's history holds it."""
    function = {"name": name, "arguments": json.dumps(arguments)}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": function}],
    }


def result(call_id: str, content: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(content)}


MULTIPLY = call("call-1", "calculator", {"op": "multiply", "a": 7, "b": 8})
ADD = call("call-2", "calculator", {"op": "add", "a": 56, "b": 3})
AFTER_TWO_CALLS = [*START, MULTIPLY, result("call-1", {"result": 56}), ADD]
AFTER_TWO_CALLS.append(result("call-2", {"result": 59}))


def example(
    tools: list[dict[str, Any]], messages: list[dict[str, Any]], reply: dict[str, Any]
) -> dict[str, Any]:
    return {"tools": tools, "messages": messages, "reply": reply}


EXAMPLES = {
    "first call": example([CALCULATOR], START, MULTIPLY),
    "a call after a tool result": example(
        [CALCULATOR], [*START, MULTIPLY, result("call-1", {"result": 56})], ADD
    ),
    "auto answer": example(
        [CALCULATOR], AFTER_TWO_CALLS, {"role": "assistant", "content": "7 times 8 plus 3 is 59."}
    ),
    "required answer": example(
        [CALCULATOR, FINAL_ANSWER],
        AFTER_TWO_CALLS,
        call("call-3", "final_answer", {"answer": "7 times 8 plus 3 is 59."}),
    ),
}


@pytest.fixture(scope="module")
def tokenizer() -> PreTrainedTokenizerBase:
    model = PROFILE["model"]
    loaded: PreTrainedTokenizerBase = AutoTokenizer.from_pretrained(
        model["name"], revision=model["revision"]
    )
    return loaded


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_the_prompt_is_what_vllm_sends_and_the_reply_ends_the_turn(
    tokenizer: PreTrainedTokenizerBase, name: str
) -> None:
    rendered = render(tokenizer, EXAMPLES[name])
    assert rendered.prompt.endswith(GENERATION_PROMPT)
    assert rendered.reply.endswith("<|im_end|>")
    assert "<|im_start|>" not in rendered.reply


@pytest.mark.parametrize("name", ["first call", "a call after a tool result", "required answer"])
def test_a_tool_call_reply_is_the_hermes_tag_the_parser_reads(
    tokenizer: PreTrainedTokenizerBase, name: str
) -> None:
    """vLLM's hermes parser reads the call from these tags in both D10 modes (experiments/log.md,
    P1.7 step 1)."""
    reply = EXAMPLES[name]["reply"]
    function = reply["tool_calls"][0]["function"]
    payload = {"name": function["name"], "arguments": json.loads(function["arguments"])}
    expected = f"<tool_call>\n{json.dumps(payload)}\n</tool_call><|im_end|>"
    assert render(tokenizer, EXAMPLES[name]).reply == expected


def test_a_text_answer_is_the_answer_and_the_end_of_the_turn(
    tokenizer: PreTrainedTokenizerBase,
) -> None:
    assert render(tokenizer, EXAMPLES["auto answer"]).reply == "7 times 8 plus 3 is 59.<|im_end|>"


def test_arguments_are_rendered_as_vllm_renders_them(tokenizer: PreTrainedTokenizerBase) -> None:
    """vLLM parses a past call's JSON string into a dict before the template renders it
    (vllm/entrypoints/chat_utils.py _postprocess_messages in v0.31.0); the history keeps the
    string, as the OpenAI API carries it."""
    messages = vllm_messages(AFTER_TWO_CALLS)
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == {
        "op": "multiply",
        "a": 7,
        "b": 8,
    }
    assert (
        AFTER_TWO_CALLS[2]["tool_calls"][0]["function"]["arguments"]
        == MULTIPLY["tool_calls"][0]["function"]["arguments"]
    )
    rendered = render(tokenizer, EXAMPLES["auto answer"])
    assert '{"name": "calculator", "arguments": {"op": "multiply", "a": 7, "b": 8}}' in (
        rendered.prompt
    )
