"""The P4.1 priority check's logic: the KV size read from vLLM's metrics, the prompt's size, the
stream's first line, and the verdict on hand-written timings. The calls themselves are make
priority-check's on the cluster (card 57)."""

import pytest
from priority_check import (
    CallTiming,
    ModeVerdict,
    engine_counts,
    judge,
    kv_capacity_tokens,
    parse_chunk,
    prompt_sentences,
    queue_formed,
    read_stream,
    verdict,
)

CACHE_INFO = (
    "# HELP vllm:cache_config_info Information of the LLMEngine CacheConfig\n"
    "# TYPE vllm:cache_config_info gauge\n"
    'vllm:cache_config_info{block_size="16",cache_dtype="auto",enable_prefix_caching="True",'
    'num_gpu_blocks="274",num_cpu_blocks="0"} 1.0\n'
    "vllm:num_requests_waiting 0.0\n"
)


def test_the_kv_size_is_blocks_times_block_size() -> None:
    """274 blocks of 16 tokens: the 4,384 tokens the real profile's pods hold (P2.4)."""
    assert kv_capacity_tokens(CACHE_INFO) == 4384


def test_metrics_without_the_cache_line_are_refused() -> None:
    with pytest.raises(ValueError, match="cache_config_info"):
        kv_capacity_tokens("vllm:num_requests_waiting 0.0\n")


@pytest.mark.parametrize(
    ("target", "base", "per_sentence", "sentences"),
    [(1753, 13, 10, 174), (1753, 13, 11, 158), (20, 13, 10, 0)],
)
def test_the_prompt_has_as_many_sentences_as_fit_the_target(
    target: int, base: int, per_sentence: int, sentences: int
) -> None:
    assert prompt_sentences(target, base, per_sentence) == sentences


def test_a_sentence_of_no_tokens_is_refused() -> None:
    with pytest.raises(ValueError, match="sentence"):
        prompt_sentences(1753, 13, 0)


def test_a_chunk_gives_the_pod_and_the_usage() -> None:
    line = (
        b'data: {"id": "x", "system_fingerprint": "vllm-57d6548d4f-2rwqc", '
        b'"choices": [], "usage": {"prompt_tokens": 1750}}\n'
    )
    assert parse_chunk(line) == ("vllm-57d6548d4f-2rwqc", 1750)


@pytest.mark.parametrize("line", [b"\n", b"data: [DONE]\n", b": keep-alive\n"])
def test_lines_without_a_chunk_give_nothing(line: bytes) -> None:
    assert parse_chunk(line) is None


def test_a_content_chunk_gives_neither_pod_nor_usage() -> None:
    """vLLM v0.31.0 stamps system_fingerprint only on the last chunk, the usage chunk when
    include_usage is on (chat_completion/serving.py); the content chunks leave it out."""
    line = b'data: {"id": "x", "choices": [{"index": 0, "delta": {"content": "x"}}]}\n'
    assert parse_chunk(line) == (None, None)


# A stream as vLLM v0.31.0 sends it with include_usage: the role chunk with the first token, content
# chunks, the usage chunk with the pod, then [DONE].
STREAM = [
    b'data: {"id": "x", "choices": [{"index": 0, "delta": {"role": "assistant", '
    b'"content": ""}}]}\n',
    b"\n",
    b'data: {"id": "x", "choices": [{"index": 0, "delta": {"content": "a"}}]}\n',
    b"\n",
    b'data: {"id": "x", "choices": [], "system_fingerprint": "pod-a", '
    b'"usage": {"prompt_tokens": 1750}}\n',
    b"\n",
    b"data: [DONE]\n",
]


def test_a_stream_gives_its_first_chunks_time_its_pod_and_its_prompt_tokens() -> None:
    ticks = iter([11.0, 12.0, 13.0])
    assert read_stream(STREAM, lambda: next(ticks)) == (11.0, "pod-a", 1750)


@pytest.mark.parametrize(
    ("lines", "says"),
    [([b"data: [DONE]\n"], "no chunk"), (STREAM[:4] + STREAM[6:], "no pod")],
)
def test_a_stream_without_a_chunk_or_a_pod_is_refused(lines: list[bytes], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        read_stream(lines, lambda: 0.0)


# A pod's metrics with the adapter served: vLLM keeps one series per model name, base and adapter.
ENGINE = (
    'vllm:num_requests_running{engine="0",model_name="thesis-5554fcf81b09"} 2.0\n'
    'vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3-0.6B-GPTQ-Int8"} 0.0\n'
    'vllm:num_requests_waiting{engine="0",model_name="thesis-5554fcf81b09"} 1.0\n'
    'vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen3-0.6B-GPTQ-Int8"} 1.0\n'
    "vllm:num_requests_waiting_by_reason 7.0\n"
)


def test_the_engines_counts_add_up_every_model_name() -> None:
    assert engine_counts(ENGINE) == (2, 2)


def test_metrics_without_the_engines_counts_are_refused() -> None:
    with pytest.raises(ValueError, match="num_requests_running"):
        engine_counts("vllm:num_requests_waiting 1.0\n")


@pytest.mark.parametrize(
    ("pods", "started", "formed"),
    [
        # Both pods queue, all eight fillers reached an engine, and the four running have their
        # first token: a filler with none now is one vLLM holds in its queue.
        ({"a": (2, 2), "b": (2, 2)}, 4, True),
        # A running filler still in its prefill would read as waiting.
        ({"a": (2, 2), "b": (2, 2)}, 3, False),
        # 2026-11-19's run: one pod's engine had seen nothing yet, its calls still on their way.
        ({"a": (2, 2), "b": (0, 0)}, 2, False),
        # Everything arrived, but one pod has no queue: a finisher sent there has nothing to
        # overtake.
        ({"a": (2, 3), "b": (2, 1)}, 4, True),
        ({"a": (2, 4), "b": (2, 0)}, 4, False),
        # A filler between the gateway and an engine is in neither count.
        ({"a": (2, 2), "b": (2, 1)}, 4, False),
    ],
)
def test_the_finisher_goes_once_every_filler_is_running_with_its_first_token_or_queued(
    pods: dict[str, tuple[int, int]], started: int, formed: bool
) -> None:
    assert queue_formed(pods, fillers=8, started=started, finished=0) is formed


@pytest.mark.parametrize(
    ("pods", "started", "finished", "formed"),
    [
        # One filler ended on pod a while pod b's calls were still on their way, and a's queue
        # moved up: all eight are accounted for, queued, running past their first token or done.
        ({"a": (2, 1), "b": (2, 2)}, 5, 1, True),
        # The same, with one of a's running fillers still in its prefill.
        ({"a": (2, 1), "b": (2, 2)}, 4, 1, False),
        # A call that is no filler in an engine: the queue is not the fillers' alone.
        ({"a": (2, 3), "b": (2, 2)}, 4, 0, False),
        # A filler with its first token back in a queue, preempted for KV blocks: all eight are
        # counted, but a waiting filler is no longer one without a first token.
        ({"a": (1, 3), "b": (2, 2)}, 4, 0, False),
    ],
)
def test_a_filler_that_ended_counts_as_neither_running_nor_waiting(
    pods: dict[str, tuple[int, int]], started: int, finished: int, formed: bool
) -> None:
    assert queue_formed(pods, fillers=8, started=started, finished=finished) is formed


def filler(name: str, pod: str, sent: float, first_byte: float) -> CallTiming:
    return CallTiming(name=name, finisher=False, pod=pod, sent=sent, first_byte=first_byte)


def finisher(pod: str, sent: float, first_byte: float) -> CallTiming:
    return CallTiming(name="finisher", finisher=True, pod=pod, sent=sent, first_byte=first_byte)


def test_the_finisher_ahead_of_every_waiting_filler_on_its_pod_went_first() -> None:
    """Fillers 1 and 2 ran before the finisher was sent; 3 and 4 still waited on its pod; 5 waited
    on the other pod, which the finisher never joined."""
    timings = [
        filler("filler-1", "pod-a", 0.0, 1.0),
        filler("filler-2", "pod-a", 0.1, 1.5),
        filler("filler-3", "pod-a", 0.2, 40.0),
        filler("filler-4", "pod-a", 0.3, 41.0),
        filler("filler-5", "pod-b", 0.4, 12.0),
        finisher("pod-a", 5.0, 30.0),
    ]
    assert verdict(timings) == ModeVerdict(
        pod="pod-a", waiting=("filler-3", "filler-4"), overtaken=("filler-3", "filler-4")
    )


def test_a_finisher_behind_the_waiting_fillers_overtook_none() -> None:
    timings = [
        filler("filler-1", "pod-a", 0.0, 1.0),
        filler("filler-2", "pod-a", 0.2, 40.0),
        filler("filler-3", "pod-a", 0.3, 41.0),
        finisher("pod-a", 5.0, 60.0),
    ]
    assert verdict(timings) == ModeVerdict(
        pod="pod-a", waiting=("filler-2", "filler-3"), overtaken=()
    )


def test_a_filler_whose_first_byte_came_at_the_finishers_send_was_not_waiting() -> None:
    timings = [filler("filler-1", "pod-a", 0.0, 5.0), finisher("pod-a", 5.0, 9.0)]
    assert verdict(timings).waiting == ()


def test_timings_without_one_finisher_are_refused() -> None:
    with pytest.raises(ValueError, match="one finisher"):
        verdict([filler("filler-1", "pod-a", 0.0, 1.0)])


def test_both_modes_as_the_rule_says_pass() -> None:
    on = ModeVerdict(
        pod="pod-a", waiting=("filler-3", "filler-4"), overtaken=("filler-3", "filler-4")
    )
    off = ModeVerdict(pod="pod-b", waiting=("filler-6",), overtaken=())
    passed, lines = judge(on, off)
    assert passed
    assert lines[-1] == "priority-check: passed"


@pytest.mark.parametrize(
    ("on", "off", "says"),
    [
        # With the rule on, a filler that kept its place ahead of the finisher: the priority did
        # not reach vLLM, or did not order it.
        (
            ModeVerdict(pod="pod-a", waiting=("filler-3", "filler-4"), overtaken=("filler-3",)),
            ModeVerdict(pod="pod-b", waiting=("filler-6",), overtaken=()),
            "priority on",
        ),
        # The control overtook too: the order came from something other than the rule.
        (
            ModeVerdict(pod="pod-a", waiting=("filler-3",), overtaken=("filler-3",)),
            ModeVerdict(pod="pod-b", waiting=("filler-6",), overtaken=("filler-6",)),
            "priority off",
        ),
        # No queue on the finisher's pod: nothing to overtake, so nothing shown.
        (
            ModeVerdict(pod="pod-a", waiting=(), overtaken=()),
            ModeVerdict(pod="pod-b", waiting=("filler-6",), overtaken=()),
            "no filler waiting",
        ),
        (
            ModeVerdict(pod="pod-a", waiting=("filler-3",), overtaken=("filler-3",)),
            ModeVerdict(pod="pod-b", waiting=(), overtaken=()),
            "no filler waiting",
        ),
    ],
)
def test_a_mode_that_does_not_show_the_rule_fails(
    on: ModeVerdict, off: ModeVerdict, says: str
) -> None:
    passed, lines = judge(on, off)
    assert not passed
    assert any(says in line for line in lines)
    assert lines[-1].startswith("priority-check: FAIL")
