"""The prompts make calibrate sends (P2.8a): pure and seeded, no network.

Prompts are the replayer's words (load_generator.plan.WORDS), one token each on the Qwen3
tokenizer; make calibrate checks every length with /tokenize before it measures. Each call's words
come from a stream named by its seed, pod, cell and call, so no two calls share a prefix by chance
and an uncached call finds nothing cached. A cached call names what is sent just before it, its
primer: the whole prompt (full) or its first partial_share of words (partial).
"""

import random
from dataclasses import dataclass

from load_generator.plan import WORDS

from calibrator.config import CalibrationError, Cell


@dataclass(frozen=True)
class CallPrompt:
    # Sent just before the measured call and not measured; None for an uncached call.
    primer: str | None
    prompt: str


def _words(stream: str, count: int) -> list[str]:
    return random.Random(stream).choices(WORDS, k=count)


def prompt_words(prompt_tokens: int, template_tokens: int) -> int:
    """Words for a prompt of prompt_tokens: the chat template's tokens are part of the length."""
    if template_tokens >= prompt_tokens:
        raise CalibrationError(
            f"a prompt of {prompt_tokens} tokens has no room beside the template's "
            f"{template_tokens}"
        )
    return prompt_tokens - template_tokens


def call_prompt(
    cell: Cell, pod: str, call: int, words: int, partial_share: float | None, seed: int
) -> CallPrompt:
    """partial_share is used only by the partial mode."""
    stream = f"calibrate/{seed}/{pod}/{cell.concurrency}/{cell.prompt_tokens}/{cell.cache}/{call}"
    if cell.cache == "uncached":
        return CallPrompt(primer=None, prompt=" ".join(_words(stream, words)))
    if cell.cache == "full":
        prompt = " ".join(_words(stream, words))
        return CallPrompt(primer=prompt, prompt=prompt)
    if partial_share is None:
        raise CalibrationError("the partial mode needs cache.partial_share")
    prefix = round(partial_share * words)
    if not 0 < prefix < words:
        raise CalibrationError(
            f"cache.partial_share {partial_share} of {words} words leaves no prefix or no new tail"
        )
    text = _words(stream, words)
    return CallPrompt(primer=" ".join(text[:prefix]), prompt=" ".join(text))


def warmup_prompt(pod: str, call: int, words: int, seed: int) -> str:
    """New words for every warm-up call, so each does the work of an uncached one. A pod's name is
    new at every start, so calls after a restart find nothing of the earlier ones."""
    return " ".join(_words(f"calibrate/{seed}/warmup/{pod}/{call}", words))
