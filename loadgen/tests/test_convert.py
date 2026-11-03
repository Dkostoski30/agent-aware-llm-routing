"""The AgentProcessBench converter (P2.4): which messages become calls, how a type is fitted into
the model's context, and what the trace holds.

/tokenize and the download are httpx.MockTransport, so nothing here needs a cluster or the network;
make convert's done-when runs them against the real ones. The fixture's text is written by hand: no
dataset text enters the repo. trace.v1 is the repo's own, so these are also its contract tests for
the converter (D12).
"""

import hashlib
import json
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from load_generator.convert import (
    Call,
    ConvertError,
    CountedRun,
    Trajectory,
    VllmTokenizer,
    check_model,
    common_prefix_length,
    config_block,
    convert_type,
    count_run,
    fetch_file,
    largest_kept_size,
    llm_call_indexes,
    load_spec,
    main,
    output_path,
    read_trajectories,
    recorded_gap_ms,
    reply_text,
    request_tools,
    trace_lines_of,
)
from load_generator.expected_steps import expected_steps
from load_generator.plan import (
    SharedPrefix,
    TraceCall,
    TraceRun,
    build_plan,
    load_replay_config,
    load_trace,
    served_model,
)
from load_generator.record import RecordError, check_line, trace_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "apb-trajectories.jsonl"
EXPERIMENT = REPO_ROOT / "experiments" / "convert-apb.yaml"
TRACES = REPO_ROOT / "experiments" / "results" / "traces"
RECORDED_TRACE = TRACES / "record-demo-e1044feb534c-d86ed436c5b2-5554fcf81b09.jsonl"
TASK_TYPES = {
    "searchR1_hotpotqa": "hotpotqa",
    "tau2_airline": "tau2_airline",
    "bfcl_multi_turn_base": "bfcl",
}
REVISION = "0123456789abcdef0123456789abcdef01234567"
GREETING = "Hi! How can I help you today?"
# The chat template's tokens around one user message, as /tokenize measured them (P2.3).
TEMPLATE_TOKENS = 12
LIMIT = 4096


class WordTokenizer:
    """One token per word, with each word's own number, so that two prompts share a prefix exactly
    when their words do. A real tokenizer gives other counts, not another logic."""

    def __init__(self) -> None:
        self.prompts: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def prompt(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> list[int]:
        self.prompts.append(([dict(m) for m in messages], [dict(t) for t in tools]))
        text = " ".join([json.dumps(tools), *(message["content"] for message in messages)])
        return [zlib.crc32(word.encode()) for word in text.split()]

    def reply(self, text: str) -> int:
        return len(text.split())


def fixture_trajectories() -> list[Trajectory]:
    return list(read_trajectories(FIXTURE, TASK_TYPES))


def named(run_id: str) -> Trajectory:
    return next(t for t in fixture_trajectories() if t.run_id == run_id)


def run_of(
    *calls: tuple[int, int], run_id: str = "a-1", first: Sequence[int] = (1, 2)
) -> CountedRun:
    """A counted run from (prompt, completion) pairs."""
    return CountedRun(
        run_id=run_id,
        task_type="a",
        calls=[Call(prompt, completion) for prompt, completion in calls],
        first_prompt=list(first),
    )


# Reading the dataset.


def test_a_trajectory_is_named_by_its_type_and_its_index() -> None:
    assert [(t.run_id, t.task_type) for t in fixture_trajectories()] == [
        ("hotpotqa-7", "hotpotqa"),
        ("tau2_airline-750", "tau2_airline"),
        ("bfcl-500", "bfcl"),
    ]


def test_a_data_source_the_experiment_does_not_list_is_refused() -> None:
    with pytest.raises(ConvertError, match="bfcl_multi_turn_base"):
        list(read_trajectories(FIXTURE, {"searchR1_hotpotqa": "hotpotqa", "tau2_airline": "tau2"}))


def test_bfcls_state_snapshots_are_not_in_the_history() -> None:
    """bfcl's environment appends one after each turn, and 246 of its 250 runs end on one: a record
    of the benchmark's, not a message of the conversation."""
    messages = named("bfcl-500").messages
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_the_same_words_from_another_role_stay_in_the_history(tmp_path: Path) -> None:
    record = {
        "total_index": 1,
        "data_source": "bfcl_multi_turn_base",
        "tools": [],
        "messages": [
            {"role": "system", "content": "you compose functions"},
            {"role": "user", "content": "<state_info> is what this tag is called"},
        ],
    }
    path = tmp_path / "test.jsonl"
    path.write_text(json.dumps(record) + "\n")
    trajectory = next(read_trajectories(path, TASK_TYPES))
    assert trajectory.messages == record["messages"]


def test_a_system_message_with_other_words_stays_in_the_history(tmp_path: Path) -> None:
    record = {
        "total_index": 1,
        "data_source": "bfcl_multi_turn_base",
        "tools": [],
        "messages": [{"role": "system", "content": "you compose functions"}],
    }
    path = tmp_path / "test.jsonl"
    path.write_text(json.dumps(record) + "\n")
    assert next(read_trajectories(path, TASK_TYPES)).messages == record["messages"]


# Which messages are LLM calls.


def test_each_assistant_message_is_one_call_however_many_tools_it_calls() -> None:
    """The second reply calls two tools: one call of the model, so no parallel_group."""
    assert llm_call_indexes(named("hotpotqa-7").messages) == [2, 4, 7]


def test_tau2s_fixed_greeting_is_not_a_call() -> None:
    """All 250 tau2 runs open with these words before any user message: the environment's text."""
    assert llm_call_indexes(named("tau2_airline-750").messages) == [3, 5]


def test_a_snapshot_changes_no_calls_place_in_the_history() -> None:
    assert llm_call_indexes(named("bfcl-500").messages) == [2, 4]


@pytest.mark.parametrize(
    "messages",
    [
        # The same words as an answer, after a user message.
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": GREETING},
        ],
        # Other words before any user message.
        [{"role": "system", "content": "s"}, {"role": "assistant", "content": "Welcome back."}],
    ],
)
def test_only_the_greeting_before_a_user_message_is_left_out(
    messages: list[dict[str, Any]],
) -> None:
    assert llm_call_indexes(messages) == [len(messages) - 1]


# What the model wrote.


def test_a_text_reply_is_its_text() -> None:
    assert reply_text({"role": "assistant", "content": "the alder"}) == "the alder"


def test_an_empty_reply_is_empty() -> None:
    assert reply_text({"role": "assistant", "content": ""}) == ""


def test_a_tool_call_is_written_as_the_chat_template_renders_it() -> None:
    message = named("hotpotqa-7").messages[2]
    assert reply_text(message) == (
        '<tool_call>\n{"name": "search", "arguments": {"query": "old town river"}}\n</tool_call>'
    )


def test_text_and_two_tool_calls_are_one_reply_in_their_order() -> None:
    message = named("hotpotqa-7").messages[4]
    assert reply_text(message) == (
        "the page does not name it\n"
        '<tool_call>\n{"name": "search", "arguments": {"query": "river name"}}\n</tool_call>\n'
        '<tool_call>\n{"name": "search", "arguments": {"query": "old town map"}}\n</tool_call>'
    )


def test_malformed_arguments_stay_as_the_model_wrote_them() -> None:
    """32 replies of the dataset hold arguments that are not JSON; they are still its tokens."""
    message = named("bfcl-500").messages[2]
    assert '"arguments": {"path": "broken}\n' in reply_text(message)


# Tool definitions.


def test_a_definition_in_the_requests_shape_stays() -> None:
    tools = named("hotpotqa-7").tools
    assert request_tools(tools) == tools


def test_bfcls_definitions_are_not_sent_since_its_system_message_lists_them() -> None:
    """Sending them too would count every tool twice: 6,986 tokens against 4,003 at a first prompt
    of the dataset."""
    assert request_tools(named("bfcl-500").tools) == []


# Counting.


def test_a_calls_prompt_is_the_history_before_it_with_the_tools() -> None:
    tokenizer = WordTokenizer()
    trajectory = named("hotpotqa-7")
    count_run(trajectory, tokenizer)
    assert [len(messages) for messages, _ in tokenizer.prompts] == [2, 4, 7]
    assert all(tools == trajectory.tools for _, tools in tokenizer.prompts)


def test_the_greeting_stays_in_the_history_of_the_calls_after_it() -> None:
    tokenizer = WordTokenizer()
    count_run(named("tau2_airline-750"), tokenizer)
    assert tokenizer.prompts[0][0][1]["content"] == GREETING


def test_bfcl_reaches_the_tokenizer_without_tools_or_snapshots() -> None:
    tokenizer = WordTokenizer()
    count_run(named("bfcl-500"), tokenizer)
    assert [tools for _, tools in tokenizer.prompts] == [[], []]
    assert not any(
        message["content"].startswith("<state_info")
        for messages, _ in tokenizer.prompts
        for message in messages
    )


def test_a_reply_counts_its_words_and_the_end_of_reply_token() -> None:
    """vLLM counts the token that ended a reply in completion_tokens."""
    run = count_run(named("hotpotqa-7"), WordTokenizer())
    # 7 words and the token that ended the reply.
    assert run.calls[2].completion_tokens == 8


def test_an_empty_reply_is_still_one_token() -> None:
    """A server refuses max_tokens 0, and the model did send the end of its reply."""
    run = count_run(named("bfcl-500"), WordTokenizer())
    assert run.calls[-1].completion_tokens == 1


def test_the_first_prompts_tokens_are_kept_for_the_shared_prefix() -> None:
    tokenizer = WordTokenizer()
    run = count_run(named("hotpotqa-7"), tokenizer)
    messages, tools = tokenizer.prompts[0]
    assert run.first_prompt == WordTokenizer().prompt(messages, tools)


def test_a_trajectory_with_no_call_is_refused() -> None:
    trajectory = Trajectory("a-1", "a", [], [{"role": "user", "content": "hello"}])
    with pytest.raises(ConvertError, match="a-1"):
        count_run(trajectory, WordTokenizer())


# Which size a type is fitted by.


@pytest.mark.parametrize(
    ("sizes", "share", "limit", "wanted"),
    [
        # The nearest rank: the 9th of 10 runs, so the 10th is dropped.
        ([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], 0.9, 400, 900),
        ([1000, 900, 800, 700, 600, 500, 400, 300, 200, 100], 0.9, 400, 900),
        # All of them, so no run is dropped.
        ([100, 200, 300], 1.0, 50, 300),
        # 90% of 7 is 6.3, so 7 runs are kept: at least the share.
        ([1, 2, 3, 4, 5, 6, 700], 0.9, 1, 700),
        # A type that already fits is never made larger.
        ([100, 200], 1.0, 4096, 4096),
        ([100, 200, 5000], 0.5, 4096, 4096),
    ],
)
def test_the_largest_size_kept_is_the_one_that_maps_to_the_limit(
    sizes: list[int], share: float, limit: int, wanted: int
) -> None:
    assert largest_kept_size(sizes, limit, share) == wanted


# One type through the fitting.


def convert_a(
    runs: list[CountedRun], limit: int = 100, share: float = 1.0, reply: int = 1000
) -> Any:
    return convert_type("a", runs, limit, share, reply)


def test_prompts_and_replies_scale_by_one_factor_rounded_down() -> None:
    result = convert_a([run_of((150, 49), (190, 10))], limit=100)
    assert result.factor == pytest.approx(0.5)
    assert result.runs[0].calls == [Call(75, 24), Call(95, 5)]


def test_a_reply_never_scales_to_zero() -> None:
    """A server refuses max_tokens 0."""
    result = convert_a([run_of((199, 1))], limit=100)
    assert result.runs[0].calls == [Call(99, 1)]


@pytest.mark.parametrize("limit", [100, 97, 4096])
@pytest.mark.parametrize(
    "calls",
    [
        [(4095, 1)],
        [(3000, 1096)],
        [(13365, 1), (13366, 873)],
        [(52986, 7), (52987, 1)],
        [(1, 1)],
        # The largest call is not the last one.
        [(100, 900), (200, 1)],
    ],
)
def test_no_scaled_call_passes_the_limit(limit: int, calls: list[tuple[int, int]]) -> None:
    """vLLM refuses prompt + max_tokens above max_model_len. Integers: no rounding can pass it."""
    # A reply of at most the limit: the reply cap has its own tests.
    result = convert_a([run_of(*calls)], limit=limit, reply=limit)
    assert len(result.runs) == 1
    assert all(c.prompt_tokens + c.completion_tokens <= limit for c in result.runs[0].calls)


def test_the_step_count_and_the_growth_are_the_trajectorys() -> None:
    calls = [(100, 10), (250, 20), (250, 5), (900, 100)]
    result = convert_a([run_of(*calls)], limit=500)
    scaled = result.runs[0].calls
    assert len(scaled) == 4
    prompts = [call.prompt_tokens for call in scaled]
    assert prompts == sorted(prompts)


def test_each_share_of_a_prompt_and_its_reply_is_kept() -> None:
    """Prefill and generation shrink alike, so a call's ratio of the two stays the dataset's. With
    replies left as they are, a run's history would grow by less than its replies."""
    result = convert_a([run_of((800, 200))], limit=100)
    assert result.runs[0].calls == [Call(80, 20)]


def test_the_largest_runs_past_the_share_are_dropped_and_counted() -> None:
    runs = [run_of((size, 1), run_id=f"a-{size}") for size in range(100, 1100, 100)]
    result = convert_a(runs, limit=500, share=0.9)
    assert [run.run_id for run in result.runs] == [f"a-{s}" for s in range(100, 1000, 100)]
    assert result.runs_in == 10
    assert result.too_large == 1


def test_a_run_is_measured_by_its_largest_call_not_its_last() -> None:
    small_end = run_of((100, 900), (200, 1), run_id="a-big-reply")
    small = run_of((100, 1), run_id="a-small")
    result = convert_a([small_end, small], limit=100, share=0.5)
    assert [run.run_id for run in result.runs] == ["a-small"]


def test_a_type_that_fits_keeps_its_counts_and_every_run_that_fits() -> None:
    runs = [run_of((100, 10), run_id="a-1"), run_of((300, 10), run_id="a-2")]
    result = convert_a(runs, limit=4096, share=0.5)
    assert result.factor == 1.0
    assert result.too_large == 0
    assert [run.calls for run in result.runs] == [[Call(100, 10)], [Call(300, 10)]]


def test_a_reply_too_long_after_scaling_drops_its_run() -> None:
    """A replay could not finish it inside the deadline."""
    runs = [run_of((400, 400), run_id="a-long"), run_of((798, 2), run_id="a-short")]
    result = convert_a(runs, limit=100, reply=49)
    assert [run.run_id for run in result.runs] == ["a-short"]
    assert result.long_reply == 1
    assert result.too_large == 0


def test_a_long_reply_on_an_earlier_call_drops_its_run_too() -> None:
    runs = [run_of((100, 400), (300, 2), run_id="a-long"), run_of((798, 2), run_id="a-short")]
    result = convert_a(runs, limit=100, reply=49)
    assert [run.run_id for run in result.runs] == ["a-short"]
    assert result.long_reply == 1


def test_a_reply_too_long_only_before_scaling_keeps_its_run() -> None:
    result = convert_a([run_of((400, 400))], limit=100, reply=50)
    assert len(result.runs) == 1
    assert result.long_reply == 0


@pytest.mark.parametrize(
    ("prompts", "wanted"),
    [
        ([[1, 2, 3], [1, 2, 3]], 3),
        ([[1, 2, 3], [1, 2, 9]], 2),
        ([[1, 2, 3], [1, 2]], 2),
        ([[9, 2, 3], [1, 2, 3]], 0),
        ([[1, 2, 3], [1, 2, 9], [1, 8, 3]], 1),
        ([[1, 2, 3]], 3),
    ],
)
def test_the_common_prefix_of_prompts(prompts: list[list[int]], wanted: int) -> None:
    assert common_prefix_length(prompts) == wanted


def test_the_shared_prefix_is_the_kept_runs_scaled_like_their_prompts() -> None:
    kept_a = run_of((200, 2), first=[1, 2, 3, 4, 5, 6], run_id="a-1")
    kept_b = run_of((200, 2), first=[1, 2, 3, 4, 9, 9], run_id="a-2")
    dropped = run_of((900, 2), first=[7], run_id="a-3")
    result = convert_a([kept_a, kept_b, dropped], limit=101, share=0.5)
    assert result.too_large == 1
    # 4 tokens in common x 101/202.
    assert result.shared_prefix_tokens == 2


def test_a_run_dropped_for_its_reply_has_no_say_in_the_shared_prefix() -> None:
    kept_a = run_of((100, 2), first=[1, 2, 3, 4], run_id="a-1")
    kept_b = run_of((100, 2), first=[1, 2, 3, 9], run_id="a-2")
    long_reply = run_of((100, 100), first=[7], run_id="a-3")
    result = convert_a([kept_a, kept_b, long_reply], limit=200, share=1.0, reply=50)
    assert result.long_reply == 1
    assert result.shared_prefix_tokens == 3


def test_the_shared_prefix_rounds_down_like_a_prompt() -> None:
    """5 tokens x 100/300 is 1.67: a replay's prompts share no more than a scaled prompt holds."""
    runs = [
        run_of((298, 2), first=[1, 2, 3, 4, 5, 6], run_id="a-1"),
        run_of((298, 2), first=[1, 2, 3, 4, 5, 9], run_id="a-2"),
    ]
    assert convert_a(runs, limit=100).shared_prefix_tokens == 1


def test_no_run_left_shares_nothing() -> None:
    result = convert_a([run_of((400, 400))], limit=100, reply=1)
    assert result.runs == []
    assert result.shared_prefix_tokens == 0


# The trace.


def test_the_recorded_gap_is_the_median_of_later_steps() -> None:
    """P2.2b's 44 later steps: median 2 ms. Its first steps' 0 would pull it down."""
    assert recorded_gap_ms(load_trace(RECORDED_TRACE)) == 2


def test_the_recorded_gap_of_an_even_count_is_a_gap_that_happened() -> None:
    """The lower middle one: the median between two would be a time no tool took."""
    calls = [TraceCall(step, 2, gap, 10, 1) for step, gap in enumerate([0, 1, 2, 8, 9], start=1)]
    assert recorded_gap_ms([TraceRun("a-1", "a", calls)]) == 2


def lines_of_fixture_run(run_id: str = "hotpotqa-7", gap_ms: int = 2) -> list[dict[str, Any]]:
    run = count_run(named(run_id), WordTokenizer())
    return trace_lines_of(run, expected=5, gap_ms=gap_ms)


def test_one_line_per_call_numbered_from_one() -> None:
    assert [line["step"] for line in lines_of_fixture_run()] == [1, 2, 3]


def test_prompt_tokens_increase_along_the_fixture() -> None:
    prompts = [line["prompt_tokens"] for line in lines_of_fixture_run()]
    assert prompts[0] < prompts[1] < prompts[2]


def test_the_first_call_has_no_gap_and_the_later_ones_the_rule() -> None:
    assert [line["gap_before_ms"] for line in lines_of_fixture_run(gap_ms=2)] == [0, 2, 2]


def test_every_line_is_valid_trace_v1_and_names_its_run() -> None:
    validator = trace_validator()
    for line in lines_of_fixture_run():
        check_line(validator, line)
        assert line["run_id"] == "hotpotqa-7"
        assert line["task_type"] == "hotpotqa"
        assert line["expected_steps"] == 5


def test_a_line_holds_nothing_that_was_not_counted() -> None:
    """No measurement, no fan-out: those come from a replay."""
    assert set(lines_of_fixture_run()[0]) == {
        "schema_version",
        "run_id",
        "task_type",
        "step",
        "expected_steps",
        "gap_before_ms",
        "prompt_tokens",
        "completion_tokens",
    }


# The experiment file.


def spec_text(**changes: Any) -> dict[str, Any]:
    return {
        "profile": "real",
        "dataset": {"repo": "a/b", "revision": REVISION, "files": {"x/test.jsonl": "ab" * 32}},
        "task_types": TASK_TYPES,
        "keep_share": 0.9,
        "max_reply_tokens": 265,
        "gap_trace": "results/traces/recorded.jsonl",
        **changes,
    }


def write_spec(tmp_path: Path, **changes: Any) -> Path:
    path = tmp_path / "convert-x.yaml"
    path.write_text(yaml.safe_dump(spec_text(**changes)))
    return path


def test_the_experiment_file_is_read() -> None:
    spec = load_spec(EXPERIMENT)
    assert spec.profile == "real"
    assert spec.keep_share == 0.9
    assert spec.max_reply_tokens == 265
    assert spec.gap_trace == RECORDED_TRACE
    assert spec.gap_trace.exists()
    assert spec.revision == "cd81f326aece8b0a3f6359e20df370690d3a98bf"
    assert set(spec.task_types.values()) == {
        "hotpotqa",
        "gaia_dev",
        "tau2_airline",
        "tau2_retail",
        "tau2_telecom",
        "bfcl",
    }
    assert sorted(spec.files) == [
        "bfcl/test.jsonl",
        "gaia_dev/test.jsonl",
        "hotpotqa/test.jsonl",
        "tau2/test.jsonl",
    ]


@pytest.mark.parametrize(
    ("changes", "named_field"),
    [
        ({"keep_share": 0}, "keep_share"),
        ({"keep_share": 1.5}, "keep_share"),
        ({"max_reply_tokens": 0}, "max_reply_tokens"),
        ({"max_reply_tokens": 2.5}, "max_reply_tokens"),
    ],
)
def test_a_value_that_could_not_run_is_refused_by_name(
    tmp_path: Path, changes: dict[str, Any], named_field: str
) -> None:
    with pytest.raises(ConvertError, match=named_field):
        load_spec(write_spec(tmp_path, **changes))


def test_the_trace_is_named_by_the_experiment_and_the_revision(tmp_path: Path) -> None:
    spec = load_spec(write_spec(tmp_path))
    assert output_path(spec) == (
        tmp_path / f"results/traces/convert-x-{spec.hash}-{REVISION[:12]}.jsonl"
    )


# The download.


def download_client(body: bytes, calls: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_missing_file_is_downloaded_and_checked(tmp_path: Path) -> None:
    body = b"the data\n"
    target = tmp_path / "cache" / "x" / "test.jsonl"
    calls: list[str] = []
    fetch_file(
        download_client(body, calls), "https://host/x", hashlib.sha256(body).hexdigest(), target
    )
    assert target.read_bytes() == body
    assert calls == ["https://host/x"]


def test_a_file_already_there_is_checked_not_downloaded_again(tmp_path: Path) -> None:
    body = b"the data\n"
    target = tmp_path / "test.jsonl"
    target.write_bytes(body)
    calls: list[str] = []
    fetch_file(
        download_client(b"other", calls), "https://host/x", hashlib.sha256(body).hexdigest(), target
    )
    assert calls == []
    assert target.read_bytes() == body


def test_a_download_with_another_sha256_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "test.jsonl"
    with pytest.raises(ConvertError, match="sha256"):
        fetch_file(download_client(b"not the pinned data"), "https://host/x", "ab" * 32, target)


def test_a_cached_file_with_another_sha256_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "test.jsonl"
    target.write_bytes(b"changed since")
    with pytest.raises(ConvertError, match="sha256"):
        fetch_file(download_client(b"unused"), "https://host/x", "ab" * 32, target)


def test_a_failed_download_leaves_no_file_to_trust(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    target = tmp_path / "test.jsonl"
    with pytest.raises(httpx.HTTPStatusError):
        fetch_file(client, "https://host/x", "ab" * 32, target)
    assert not target.exists()


def test_a_download_that_breaks_off_leaves_no_file_to_trust(tmp_path: Path) -> None:
    class BreaksOff(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"the first half"
            raise httpx.ReadError("connection lost")

    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BreaksOff()))
    )
    target = tmp_path / "test.jsonl"
    with pytest.raises(httpx.ReadError):
        fetch_file(client, "https://host/x", "ab" * 32, target)
    assert not target.exists()


# /tokenize.


def tokenizer_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> VllmTokenizer:
    client = httpx.Client(base_url="http://vllm", transport=httpx.MockTransport(handler))
    return VllmTokenizer(client, "thesis-adapter")


def test_a_prompt_goes_through_the_chat_template_with_its_tools_and_no_thinking() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tokenize"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"count": 3, "tokens": [7, 8, 9]})

    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "a"}}]
    assert tokenizer_with(handler).prompt(messages, tools) == [7, 8, 9]
    assert seen == [
        {
            "model": "thesis-adapter",
            "messages": messages,
            "tools": tools,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ]


def test_a_reply_is_counted_as_plain_text() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"count": 4, "tokens": [1, 2, 3, 4]})

    assert tokenizer_with(handler).reply("the alder") == 4
    assert seen == [{"model": "thesis-adapter", "prompt": "the alder", "add_special_tokens": False}]


def test_a_refusal_says_what_the_server_said() -> None:
    tokenizer = tokenizer_with(lambda _: httpx.Response(400, text="bad message shape"))
    with pytest.raises(ConvertError, match="bad message shape"):
        tokenizer.reply("the alder")


# The cluster.


def test_a_cluster_on_the_profiles_model_passes() -> None:
    wanted = {"model": {"name": "m", "revision": "r"}, "agent": {"max_steps": 10}}
    running = yaml.safe_dump({"model": {"name": "m", "revision": "r"}, "agent": {"max_steps": 99}})
    check_model("real", running, wanted)


def test_a_cluster_on_another_model_is_refused() -> None:
    """The simulator's /tokenize counts words, and another revision has another tokenizer: counts
    from either would not be this trace's."""
    running = yaml.safe_dump({"model": {"name": "other"}})
    with pytest.raises(ConvertError, match="make up PROFILE=real"):
        check_model("real", running, {"model": {"name": "m"}})


def test_a_cluster_on_another_revision_of_the_model_is_refused() -> None:
    running = yaml.safe_dump({"model": {"name": "m", "revision": "r1"}})
    with pytest.raises(ConvertError, match="make up PROFILE=real"):
        check_model("real", running, {"model": {"name": "m", "revision": "r2"}})


# What to paste into the profiles.


def test_the_block_to_paste_has_every_types_values() -> None:
    results = [convert_type("a", [run_of((100, 10), first=[1, 2])], 4096, 1.0, 265)]
    block = config_block({"csv": 2, "a": 7}, results)
    parsed = yaml.safe_load(block)
    assert parsed == {
        "agent.expected_steps_by_type": {"csv": 2, "a": 7},
        "replay.shared_prefix_tokens.by_type": {"a": 2},
    }


# main, end to end against fakes.


def fake_tokenize(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if "prompt" in body:
        return httpx.Response(200, json={"count": len(body["prompt"].split()), "tokens": []})
    text = " ".join([json.dumps(body["tools"]), *(m["content"] for m in body["messages"])])
    tokens = [zlib.crc32(word.encode()) for word in text.split()]
    return httpx.Response(200, json={"count": len(tokens), "tokens": tokens})


class Setup:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: bytes | None = None,
        keep_share: float = 1.0,
        max_model_len: int | None = None,
    ) -> None:
        body = FIXTURE.read_bytes() if body is None else body
        self.spec_path = write_spec(
            tmp_path,
            dataset={
                "repo": "a/b",
                "revision": REVISION,
                "files": {"x/test.jsonl": hashlib.sha256(body).hexdigest()},
            },
            keep_share=keep_share,
            gap_trace=str(RECORDED_TRACE),
        )
        self.kubectl_calls: list[tuple[str, ...]] = []
        self.requests: list[str] = []
        self.tokenized: list[dict[str, Any]] = []
        profile_text = (REPO_ROOT / "config" / "real.yaml").read_text()
        if max_model_len is not None:
            profile = yaml.safe_load(profile_text)
            profile["model"]["max_model_len"] = max_model_len
            profile_text = yaml.safe_dump(profile)
            (tmp_path / "config").mkdir()
            (tmp_path / "config" / "real.yaml").write_text(profile_text)
            monkeypatch.setattr("load_generator.convert.REPO_ROOT", tmp_path)

        def kubectl(*args: str) -> str:
            self.kubectl_calls.append(args)
            return profile_text if args[1] == "configmap" else "10.0.0.1"

        original_client = httpx.Client

        def client(*args: Any, **kwargs: Any) -> httpx.Client:
            def handler(request: httpx.Request) -> httpx.Response:
                self.requests.append(str(request.url))
                if request.url.path == "/tokenize":
                    self.tokenized.append(json.loads(request.content))
                    return fake_tokenize(request)
                # Hugging Face sends a download on to its storage.
                if request.url.host == "huggingface.co":
                    return httpx.Response(302, headers={"location": "https://storage.test/x"})
                return httpx.Response(200, content=body)

            kwargs.pop("transport", None)
            return original_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("load_generator.convert.kubectl", kubectl)
        monkeypatch.setattr("load_generator.convert.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(httpx, "Client", client)
        monkeypatch.setattr("sys.argv", ["convert", str(self.spec_path)])
        self.trace = output_path(load_spec(self.spec_path))


def test_main_writes_a_trace_the_replayer_can_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    main()
    runs = load_trace(setup.trace)
    assert [(run.run_id, len(run.calls)) for run in runs] == [
        ("hotpotqa-7", 3),
        ("tau2_airline-750", 2),
        ("bfcl-500", 2),
    ]
    assert all(call.expected_steps == len(run.calls) for run in runs for call in run.calls)
    assert [call.gap_before_ms for call in runs[0].calls] == [0, 2, 2]
    validator = trace_validator()
    for line in setup.trace.read_text().splitlines():
        check_line(validator, json.loads(line))
    section = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())["replay"]
    config = load_replay_config(section)
    shared = SharedPrefix(all_types=0, by_type={"hotpotqa": 5, "tau2_airline": 5, "bfcl": 5})
    pattern = next(iter(config.patterns.values()))
    plan = build_plan(runs, pattern, shared, 1, TEMPLATE_TOKENS)
    assert len(plan) == pattern.runs


def test_main_downloads_the_pinned_revision_through_its_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    main()
    assert setup.requests[:2] == [
        f"https://huggingface.co/datasets/a/b/resolve/{REVISION}/x/test.jsonl",
        "https://storage.test/x",
    ]


def test_main_counts_on_the_name_the_replays_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The adapter's, when the profile serves one: vLLM answers to it as it does to the base."""
    setup = Setup(tmp_path, monkeypatch)
    main()
    model = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())["model"]
    assert {body["model"] for body in setup.tokenized} == {model["lora"]["name"]}


def test_main_counts_bfcl_without_tools_or_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    main()
    prompts = [body for body in setup.tokenized if "messages" in body]
    bfcl = [body for body in prompts if "you compose" in body["messages"][0]["content"]]
    assert [body["tools"] for body in bfcl] == [[], []]
    assert not any(
        message["content"].startswith("<state_info")
        for body in prompts
        for message in body["messages"]
    )


def trajectory_line(index: int, calls: int, words: int = 2) -> str:
    """A hotpotqa run of that many calls, each reply of that many words."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": "you search"}]
    for _ in range(calls):
        messages.append({"role": "user", "content": "next"})
        messages.append({"role": "assistant", "content": "word " * words})
    return json.dumps(
        {
            "total_index": index,
            "data_source": "searchR1_hotpotqa",
            "tools": [],
            "messages": messages,
        }
    )


def test_main_gives_each_run_its_types_median_over_the_runs_it_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Runs of 1, 2 and 4 calls: 2. The run of 40 calls is dropped as too large, and counted it
    would make the median 3."""
    lines = [trajectory_line(i, calls) for i, calls in [(9, 1), (2, 2), (30, 4), (4, 40)]]
    setup = Setup(
        tmp_path,
        monkeypatch,
        body="".join(f"{line}\n" for line in lines).encode(),
        keep_share=0.75,
        max_model_len=30,
    )
    main()
    runs = load_trace(setup.trace)
    # In the file's order.
    assert [(run.run_id, len(run.calls)) for run in runs] == [
        ("hotpotqa-9", 1),
        ("hotpotqa-2", 2),
        ("hotpotqa-30", 4),
    ]
    assert {call.expected_steps for run in runs for call in run.calls} == {2}
    meta = json.loads(setup.trace.with_suffix(".meta.json").read_text())
    assert meta["task_types"]["hotpotqa"]["too_large"] == 1
    assert meta["task_types"]["hotpotqa"]["expected_steps"] == 2


def test_main_fits_the_calls_to_the_profiles_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, monkeypatch, max_model_len=20)
    main()
    runs = load_trace(setup.trace)
    assert max(c.prompt_tokens + c.completion_tokens for run in runs for c in run.calls) <= 20
    assert json.loads(setup.trace.with_suffix(".meta.json").read_text())["max_model_len"] == 20


def test_main_prints_each_types_result_and_the_block_to_paste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    main()
    out = capsys.readouterr().out
    assert "hotpotqa: factor 1.000, 1 of 1 runs" in out
    assert "agent.expected_steps_by_type" in out
    assert "replay.shared_prefix_tokens.by_type" in out
    # The demo trace's types too, since the profiles hold both.
    assert "contracts: 3" in out
    assert str(setup.trace) in out


def test_main_writes_what_it_decided_beside_the_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    main()
    meta = json.loads(setup.trace.with_suffix(".meta.json").read_text())
    assert meta["gap_ms"] == 2
    assert meta["gap_trace"] == RECORDED_TRACE.name
    assert meta["max_model_len"] == 4096
    assert meta["keep_share"] == 1.0
    assert meta["max_reply_tokens"] == 265
    assert meta["dataset_revision"] == REVISION
    model = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())["model"]
    assert meta["model"] == {
        "name": model["name"],
        "revision": model["revision"],
        "served_as": model["lora"]["name"],
    }
    # One run of the type, so all of its first prompt is shared.
    first_prompt = count_run(named("tau2_airline-750"), WordTokenizer()).first_prompt
    assert meta["task_types"]["tau2_airline"] == {
        "factor": 1.0,
        "largest_kept_size": 4096,
        "runs_in": 1,
        "too_large": 0,
        "long_reply": 0,
        "runs": 1,
        "calls": 2,
        "expected_steps": 2,
        "shared_prefix_tokens": len(first_prompt),
    }


def test_main_refuses_an_existing_trace_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    setup.trace.parent.mkdir(parents=True)
    setup.trace.write_text("an earlier conversion\n")
    with pytest.raises(ConvertError, match="exists"):
        main()
    assert setup.trace.read_text() == "an earlier conversion\n"
    assert setup.requests == []
    assert setup.kubectl_calls == []


def test_main_refuses_a_leftover_meta_file_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused only at the end, it would cost the whole conversion."""
    setup = Setup(tmp_path, monkeypatch)
    meta = setup.trace.with_suffix(".meta.json")
    meta.parent.mkdir(parents=True)
    meta.write_text("an earlier conversion\n")
    with pytest.raises(ConvertError, match=r"meta\.json exists"):
        main()
    assert setup.requests == []
    assert setup.kubectl_calls == []


def test_main_refuses_another_model_before_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "load_generator.convert.kubectl", lambda *_: yaml.safe_dump({"model": {"name": "other"}})
    )
    with pytest.raises(ConvertError, match="make up PROFILE=real"):
        main()
    assert not setup.trace.exists()
    assert setup.requests == []


def test_a_conversion_that_stops_leaves_no_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(tmp_path, monkeypatch)

    def broken(*_: Any) -> list[int]:
        raise ConvertError("the server refused")

    monkeypatch.setattr(VllmTokenizer, "prompt", broken)
    with pytest.raises(ConvertError):
        main()
    assert not setup.trace.exists()
    assert not setup.trace.with_suffix(".meta.json").exists()


def test_a_line_trace_v1_refuses_stops_the_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "load_generator.convert.trace_lines_of",
        lambda *_, **__: [{"run_id": "a", "step": 1}],
    )
    with pytest.raises(RecordError, match=r"trace\.v1"):
        main()
    assert not setup.trace.exists()


# The committed result.


def test_a_converted_trace_is_committed() -> None:
    """So that the two tests below, one case per trace, cannot pass by having no cases."""
    assert sorted(TRACES.glob("convert-apb-*.jsonl"))


@pytest.mark.parametrize("trace", sorted(TRACES.glob("convert-apb-*.jsonl")), ids=lambda p: p.name)
def test_a_converted_trace_carries_its_own_medians_and_fits_the_context(trace: Path) -> None:
    runs = load_trace(trace)
    wanted = expected_steps((run.task_type, len(run.calls)) for run in runs)
    limit = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())["model"][
        "max_model_len"
    ]
    for run in runs:
        for call in run.calls:
            assert call.expected_steps == wanted[run.task_type]
            assert call.prompt_tokens + call.completion_tokens <= limit


@pytest.mark.parametrize("trace", sorted(TRACES.glob("convert-apb-*.jsonl")), ids=lambda p: p.name)
def test_no_prompt_of_a_converted_trace_shrinks(trace: Path) -> None:
    """Every later step of a replay extends the one before it, and P3.3's check rests on that."""
    for run in load_trace(trace):
        prompts = [call.prompt_tokens for call in run.calls]
        assert prompts == sorted(prompts), run.run_id


@pytest.mark.parametrize("trace", sorted(TRACES.glob("convert-apb-*.jsonl")), ids=lambda p: p.name)
def test_a_converted_traces_meta_counts_what_the_trace_holds(trace: Path) -> None:
    meta = json.loads(trace.with_suffix(".meta.json").read_text())
    runs = load_trace(trace)
    found = {
        task_type: {
            "runs": sum(run.task_type == task_type for run in runs),
            "calls": sum(len(run.calls) for run in runs if run.task_type == task_type),
            "expected_steps": wanted,
        }
        for task_type, wanted in expected_steps(
            (run.task_type, len(run.calls)) for run in runs
        ).items()
    }
    assert {
        task_type: {key: values[key] for key in ("runs", "calls", "expected_steps")}
        for task_type, values in meta["task_types"].items()
    } == found
    for values in meta["task_types"].values():
        assert values["runs_in"] == values["runs"] + values["too_large"] + values["long_reply"]
    assert {call.gap_before_ms for run in runs for call in run.calls if call.step > 1} == {
        meta["gap_ms"]
    }


@pytest.mark.parametrize("trace", sorted(TRACES.glob("convert-apb-*.jsonl")), ids=lambda p: p.name)
def test_a_converted_traces_meta_names_the_tokenizer_and_the_dataset(trace: Path) -> None:
    spec = load_spec(EXPERIMENT)
    assert trace == output_path(spec)
    meta = json.loads(trace.with_suffix(".meta.json").read_text())
    model = yaml.safe_load((REPO_ROOT / "config" / f"{spec.profile}.yaml").read_text())["model"]
    assert meta["model"] == {
        "name": model["name"],
        "revision": model["revision"],
        "served_as": served_model(model),
    }
    assert meta["dataset_revision"] == spec.revision
    assert meta["max_model_len"] == model["max_model_len"]


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
@pytest.mark.parametrize("trace", sorted(TRACES.glob("convert-apb-*.jsonl")), ids=lambda p: p.name)
def test_the_profiles_hold_the_shared_prefix_of_the_converted_trace(
    trace: Path, profile: str
) -> None:
    """make convert prints these and they are pasted, as with expected_steps: a replay's prompts are
    built from them, and nothing else would notice a wrong value."""
    meta = json.loads(trace.with_suffix(".meta.json").read_text())
    section = yaml.safe_load((REPO_ROOT / "config" / f"{profile}.yaml").read_text())["replay"]
    by_type = section["shared_prefix_tokens"]["by_type"]
    for task_type, values in meta["task_types"].items():
        assert by_type[task_type] == values["shared_prefix_tokens"]


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
@pytest.mark.parametrize("trace", sorted(TRACES.glob("convert-apb-*.jsonl")), ids=lambda p: p.name)
def test_a_converted_trace_plans_on_every_profile_and_pattern(trace: Path, profile: str) -> None:
    section = yaml.safe_load((REPO_ROOT / "config" / f"{profile}.yaml").read_text())["replay"]
    config = load_replay_config(section)
    runs = load_trace(trace)
    for pattern in config.patterns.values():
        plan = build_plan(runs, pattern, config.shared_prefix, config.seed, TEMPLATE_TOKENS)
        assert len(plan) == pattern.runs
