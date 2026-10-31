"""Renders a generated training example (experiments/finetune_data.py) into the prompt vLLM builds
for that step and the text the model must write after it, with the served model's chat template
(P1.7 step 2). Step 3 trains on these.

Run directly, it reports the token lengths of make finetune-data's examples: step 3 sets its max
length from the longest, and its reply length (the logits it computes) from the longest reply.

Usage: render.py DATA_EXPERIMENT_YAML   (experiments/finetune-data.yaml)
"""

import argparse
import copy
import hashlib
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from transformers import AutoTokenizer, PreTrainedTokenizerBase

REPO_ROOT = Path(__file__).resolve().parents[1]
# What Qwen3's chat template adds after the history when thinking is off (CLAUDE.md): the model
# writes its reply after this.
GENERATION_PROMPT = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
# The token that ends an assistant turn; the model must learn to write it, or it never stops.
END_OF_TURN = "<|im_end|>"


@dataclass(frozen=True)
class Rendered:
    prompt: str
    reply: str


def vllm_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages as vLLM v0.31.0 hands them to the chat template: a past tool call's arguments
    parsed from the JSON string the OpenAI API carries into a dict
    (vllm/entrypoints/chat_utils.py, _postprocess_messages)."""
    parsed = copy.deepcopy(messages)
    for message in parsed:
        for call in message.get("tool_calls") or []:
            call["function"]["arguments"] = json.loads(call["function"]["arguments"])
    return parsed


def _template(
    tokenizer: PreTrainedTokenizerBase,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    add_generation_prompt: bool,
) -> str:
    rendered = tokenizer.apply_chat_template(
        vllm_messages(messages),
        # A new list: transformers types it as a list of dicts or functions, and a list[dict] is
        # not one for mypy (lists are invariant).
        tools=list(tools),
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
        enable_thinking=False,
    )
    if not isinstance(rendered, str):
        raise TypeError(f"apply_chat_template returned {type(rendered).__name__}, not text")
    return rendered


def render(tokenizer: PreTrainedTokenizerBase, example: dict[str, Any]) -> Rendered:
    """The prompt is the history rendered as vLLM renders it for the next call; the reply is what
    the template writes for the example's reply after that prompt, up to the end of the turn.

    Raises ValueError when the history with the reply does not start with the prompt: then the
    reply could not be cut off the end, and the prompt trained on would not be the one vLLM sends.
    """
    prompt = _template(tokenizer, example["messages"], example["tools"], True)
    full = _template(tokenizer, [*example["messages"], example["reply"]], example["tools"], False)
    if not full.startswith(prompt):
        raise ValueError(f"{example.get('id')}: the rendered history changes once the reply is in")
    # The template ends a turn with a newline after the end token; generation stops at the token.
    reply = full[len(prompt) :].removesuffix("\n")
    if not reply.endswith(END_OF_TURN):
        raise ValueError(f"{example.get('id')}: the reply does not end the turn: {reply!r}")
    return Rendered(prompt, reply)


def data_dir(spec_path: Path) -> Path:
    """Where make finetune-data wrote this experiment file's data: the same rule as
    finetune_data.data_dir, which this environment cannot import (it has no agent runtime)."""
    spec = yaml.safe_load(spec_path.read_text())
    digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()[:12]
    return Path(spec["output_dir"]).expanduser() / digest


def main() -> None:
    parser = argparse.ArgumentParser(description="P1.7: token lengths of the training examples")
    parser.add_argument("experiment", type=Path, help="experiments/finetune-data.yaml")
    spec_path: Path = parser.parse_args().experiment
    spec = yaml.safe_load(spec_path.read_text())
    profile = yaml.safe_load((REPO_ROOT / "config" / f"{spec['profile']}.yaml").read_text())
    model = profile["model"]
    tokenizer = AutoTokenizer.from_pretrained(model["name"], revision=model["revision"])

    lengths: dict[str, int] = {}
    replies: dict[str, int] = {}
    for line in (data_dir(spec_path) / "train.jsonl").read_text().splitlines():
        example = json.loads(line)
        rendered = render(tokenizer, example)
        lengths[example["id"]] = len(tokenizer(rendered.prompt + rendered.reply)["input_ids"])
        replies[example["id"]] = len(tokenizer(rendered.reply)["input_ids"])

    values = sorted(lengths.values())
    p50, p95 = statistics.quantiles(values, n=100)[49], statistics.quantiles(values, n=100)[94]
    longest = max(lengths, key=lambda example_id: lengths[example_id])
    longest_reply = max(replies, key=lambda example_id: replies[example_id])
    over = sum(length > model["max_model_len"] for length in values)
    print(f"render: {len(values)} examples, {sum(values)} tokens in all")
    print(f"render: length p50 {p50:.0f}, p95 {p95:.0f}, max {values[-1]} ({longest})")
    print(f"render: reply max {replies[longest_reply]} tokens ({longest_reply})")
    print(f"render: {over} examples longer than model.max_model_len {model['max_model_len']}")


if __name__ == "__main__":
    main()
