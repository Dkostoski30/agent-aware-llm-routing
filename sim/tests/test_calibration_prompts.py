"""The prompts make calibrate sends (P2.8a): seeded words, one token each, so that uncached calls
share nothing, a full one repeats its primer and a partial one extends it. No network."""

import pytest
from calibrator.config import CalibrationError, Cell
from calibrator.prompts import call_prompt, prompt_words, warmup_prompt
from load_generator.plan import WORDS

UNCACHED = Cell(concurrency=2, prompt_tokens=256, cache="uncached")
FULL = Cell(concurrency=2, prompt_tokens=256, cache="full")
PARTIAL = Cell(concurrency=2, prompt_tokens=256, cache="partial")


def test_prompt_words_leave_room_for_the_chat_template() -> None:
    assert prompt_words(256, template_tokens=12) == 244
    with pytest.raises(CalibrationError, match="template"):
        prompt_words(12, template_tokens=12)


def test_uncached_calls_share_no_text_and_have_no_primer() -> None:
    prompts = [call_prompt(UNCACHED, "pod-a", call, 244, 0.75, seed=1) for call in range(20)]
    assert all(prompt.primer is None for prompt in prompts)
    assert len({prompt.prompt for prompt in prompts}) == 20
    first_blocks = {" ".join(prompt.prompt.split()[:16]) for prompt in prompts}
    assert len(first_blocks) == 20


def test_every_prompt_is_the_asked_number_of_known_words() -> None:
    for cell in (UNCACHED, FULL, PARTIAL):
        words = call_prompt(cell, "pod-a", 0, 244, 0.75, seed=1).prompt.split()
        assert len(words) == 244
        assert set(words) <= set(WORDS)


def test_pods_cells_and_seeds_get_their_own_words() -> None:
    base = call_prompt(UNCACHED, "pod-a", 0, 244, 0.75, seed=1).prompt
    assert call_prompt(UNCACHED, "pod-b", 0, 244, 0.75, seed=1).prompt != base
    assert call_prompt(FULL, "pod-a", 0, 244, 0.75, seed=1).prompt != base
    other_level = Cell(concurrency=4, prompt_tokens=256, cache="uncached")
    assert call_prompt(other_level, "pod-a", 0, 244, 0.75, seed=1).prompt != base
    assert call_prompt(UNCACHED, "pod-a", 0, 244, 0.75, seed=2).prompt != base
    assert call_prompt(UNCACHED, "pod-a", 0, 244, 0.75, seed=1).prompt == base


def test_a_full_call_repeats_its_primer() -> None:
    prompt = call_prompt(FULL, "pod-a", 3, 244, 0.75, seed=1)
    assert prompt.primer == prompt.prompt


def test_a_partial_call_extends_its_primer_with_new_words() -> None:
    prompts = [call_prompt(PARTIAL, "pod-a", call, 244, 0.75, seed=1) for call in range(5)]
    for prompt in prompts:
        assert prompt.primer is not None
        assert len(prompt.primer.split()) == 183
        assert prompt.prompt.startswith(prompt.primer + " ")
    assert len({prompt.primer for prompt in prompts}) == 5


def test_a_share_that_leaves_no_prefix_or_no_tail_is_refused() -> None:
    with pytest.raises(CalibrationError, match="partial_share"):
        call_prompt(PARTIAL, "pod-a", 0, 3, 0.1, seed=1)
    with pytest.raises(CalibrationError, match="partial_share"):
        call_prompt(PARTIAL, "pod-a", 0, 3, 0.9, seed=1)


def test_warmup_prompts_differ_per_pod_and_call_and_from_cells() -> None:
    pods = ("pod-a", "pod-b")
    prompts = {warmup_prompt(pod, call, 244, seed=1) for pod in pods for call in range(3)}
    assert len(prompts) == 6
    assert (
        warmup_prompt("pod-a", 0, 244, seed=1)
        != call_prompt(UNCACHED, "pod-a", 0, 244, 0.75, seed=1).prompt
    )
    assert len(warmup_prompt("pod-a", 0, 244, seed=1).split()) == 244
