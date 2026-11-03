"""What a replay will send (P2.3): seeded arrivals, run choice, prompts and fan-out, with no network
and no clock. The schemas and the profiles are the repo's own, so these are also headers.v1's
contract tests for the replayer (D12)."""

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from load_generator.plan import (
    Burst,
    FanOut,
    Pattern,
    PlannedRun,
    ReplayConfig,
    ReplayError,
    SharedPrefix,
    TraceCall,
    TraceRun,
    build_plan,
    call_headers,
    chat_request,
    choose_runs,
    load_replay_config,
    load_trace,
    named_pattern,
    served_model,
    start_times,
    tokenize_request,
    trace_run_id,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HEADERS_SCHEMA = REPO_ROOT / "schemas" / "headers.v1.json"
RECORDED_TRACE = (
    REPO_ROOT
    / "experiments"
    / "results"
    / "traces"
    / "record-demo-e1044feb534c-d86ed436c5b2-5554fcf81b09.jsonl"
)
# The chat template around one user message on the Qwen3 tokenizer (make replay measures it).
TEMPLATE_TOKENS = 12
SHARED = SharedPrefix(all_types=8, by_type={"csv": 20, "support": 30})
STEADY = Pattern(runs=6, rate_per_second=0.05, burst=None, fan_out=None)


def run_of(run_id: str, task_type: str, prompts: list[int], completion: int = 5) -> TraceRun:
    calls = [
        TraceCall(
            step=step,
            expected_steps=2,
            gap_before_ms=0 if step == 1 else 7,
            prompt_tokens=prompt,
            completion_tokens=completion,
        )
        for step, prompt in enumerate(prompts, start=1)
    ]
    return TraceRun(run_id=run_id, task_type=task_type, calls=calls)


RUNS = [
    run_of("run-a", "csv", [60, 80]),
    run_of("run-b", "csv", [62, 84, 90]),
    run_of("run-c", "support", [70, 95]),
]


def plan_of(
    pattern: Pattern = STEADY, seed: int = 1, runs: list[TraceRun] | None = None
) -> list[PlannedRun]:
    return build_plan(RUNS if runs is None else runs, pattern, SHARED, seed, TEMPLATE_TOKENS)


def words_of(content: str) -> list[str]:
    return content.split(" ")


def shared_length(first: str, second: str) -> int:
    length = 0
    for one, other in zip(words_of(first), words_of(second), strict=False):
        if one != other:
            break
        length += 1
    return length


# Arrivals.


@pytest.mark.parametrize(
    "pattern",
    [STEADY, Pattern(runs=12, rate_per_second=0.02, burst=Burst(4, 30.0), fan_out=None)],
)
def test_the_same_seed_gives_the_same_start_times_and_another_seed_others(pattern: Pattern) -> None:
    assert start_times(pattern, seed=1) == start_times(pattern, seed=1)
    assert start_times(pattern, seed=1) != start_times(pattern, seed=2)


def test_steady_starts_at_once_and_averages_the_rate() -> None:
    times = start_times(Pattern(2000, 0.05, None, None), seed=1)
    assert times[0] == 0.0
    assert times == sorted(times)
    mean_interval = times[-1] / (len(times) - 1)
    assert mean_interval == pytest.approx(20.0, rel=0.1)


def test_steady_intervals_vary_so_the_seed_really_sets_the_times() -> None:
    times = start_times(STEADY, seed=1)
    intervals = {round(later - earlier, 6) for earlier, later in itertools.pairwise(times)}
    assert len(intervals) > 1


def test_burst_adds_n_runs_exactly_every_t_seconds() -> None:
    times = start_times(Pattern(40, 0.01, Burst(runs=5, every_seconds=60.0), None), seed=1)
    assert len(times) == 40
    assert times == sorted(times)
    assert times.count(60.0) == 5
    assert times.count(120.0) == 5


def test_a_pattern_gets_exactly_its_runs_even_when_a_burst_cuts_it_off() -> None:
    assert len(start_times(Pattern(7, 0.001, Burst(5, 10.0), None), seed=1)) == 7


# Which runs.


def test_the_same_seed_gives_the_same_run_order_and_another_seed_another() -> None:
    assert choose_runs(RUNS, 3, seed=1) == choose_runs(RUNS, 3, seed=1)
    many = [run_of(f"run-{index}", "csv", [60]) for index in range(30)]
    assert choose_runs(many, 30, seed=1) != choose_runs(many, 30, seed=2)


def test_every_run_is_used_once_before_any_repeats() -> None:
    chosen = choose_runs(RUNS, 7, seed=1)
    ids = [run.run_id for run in chosen]
    assert len(ids) == 7
    assert sorted(ids[:3]) == ["run-a", "run-b", "run-c"]
    assert sorted(ids[3:6]) == ["run-a", "run-b", "run-c"]


def test_a_smaller_pattern_takes_only_its_runs() -> None:
    plan = plan_of(Pattern(2, 0.05, None, None))
    assert len(plan) == 2


# Run ids.


def test_run_ids_are_unique_and_lead_back_to_the_traces_run_even_when_runs_repeat() -> None:
    plan = plan_of(Pattern(7, 0.05, None, None))
    ids = [run.run_id for run in plan]
    assert len(set(ids)) == 7
    assert {trace_run_id(run_id) for run_id in ids} == {"run-a", "run-b", "run-c"}


def test_the_same_seed_gives_the_same_plan() -> None:
    assert plan_of() == plan_of()


def test_another_seed_gives_other_run_text_but_the_same_shared_prefix() -> None:
    one = {trace_run_id(run.run_id): run for run in plan_of(seed=1)}
    other = {trace_run_id(run.run_id): run for run in plan_of(seed=2)}
    first, second = one["run-a"].steps[0].content, other["run-a"].steps[0].content
    assert first != second
    assert shared_length(first, second) >= SHARED.by_type["csv"]


# Prompts.


def test_step_k_is_a_prefix_of_step_k_plus_1() -> None:
    for run in plan_of():
        for earlier, later in itertools.pairwise(run.steps):
            assert later.content.startswith(earlier.content + " ")


def test_a_prompt_holds_the_traces_tokens_less_the_template_one_word_per_token() -> None:
    for run in plan_of():
        for step in run.steps:
            assert len(words_of(step.content)) == step.prompt_tokens - TEMPLATE_TOKENS


def test_runs_of_one_type_share_exactly_that_types_prefix() -> None:
    plan = {trace_run_id(run.run_id): run for run in plan_of()}
    shared = shared_length(plan["run-a"].steps[0].content, plan["run-b"].steps[0].content)
    # A random next word can match by chance, one word in 64, so the seed is fixed in plan_of.
    assert shared == SHARED.by_type["csv"]


def test_runs_of_different_types_share_exactly_the_all_types_prefix() -> None:
    plan = {trace_run_id(run.run_id): run for run in plan_of()}
    shared = shared_length(plan["run-a"].steps[0].content, plan["run-c"].steps[0].content)
    assert shared == SHARED.all_types


def test_a_task_type_without_a_prefix_is_refused() -> None:
    with pytest.raises(ReplayError, match="contracts"):
        plan_of(runs=[run_of("run-x", "contracts", [60])])


def test_a_prompt_too_short_for_the_template_is_refused() -> None:
    with pytest.raises(ReplayError, match="run-x"):
        plan_of(runs=[run_of("run-x", "csv", [TEMPLATE_TOKENS])])


def test_a_step_keeps_the_traces_lengths_gap_and_expected_steps() -> None:
    run = next(run for run in plan_of() if trace_run_id(run.run_id) == "run-a")
    assert run.task_type == "csv"
    first, second = run.steps
    assert (first.step, first.expected_steps, first.gap_before_ms) == (1, 2, 0)
    assert (second.step, second.prompt_tokens, second.completion_tokens) == (2, 80, 5)
    assert second.gap_before_ms == 7


def test_starts_follow_start_times_in_order() -> None:
    plan = plan_of()
    assert [run.start_seconds for run in plan] == start_times(STEADY, seed=1)


# Fan-out.


def test_no_fan_out_means_one_call_per_step_and_no_group() -> None:
    for run in plan_of():
        for step in run.steps:
            assert (step.calls, step.parallel_group) == (1, None)


def test_share_1_splits_every_later_step_into_k_calls_with_one_group_and_never_step_1() -> None:
    plan = plan_of(Pattern(3, 0.05, None, FanOut(share=1.0, k=3)))
    for run in plan:
        assert (run.steps[0].calls, run.steps[0].parallel_group) == (1, None)
        for step in run.steps[1:]:
            assert step.calls == 3
            assert step.parallel_group == f"step-{step.step}"


def test_the_seed_picks_the_fanned_out_steps() -> None:
    many = [run_of(f"run-{index}", "csv", [60, 70, 80, 90]) for index in range(30)]
    pattern = Pattern(30, 0.05, None, FanOut(share=0.5, k=2))

    def groups(seed: int) -> list[tuple[str, int]]:
        return [
            (run.run_id, step.step)
            for run in build_plan(many, pattern, SHARED, seed, TEMPLATE_TOKENS)
            for step in run.steps
            if step.parallel_group is not None
        ]

    assert groups(1) == groups(1)
    assert groups(1) != groups(2)
    later_steps = 30 * 3
    assert 0.3 * later_steps < len(groups(1)) < 0.7 * later_steps


def test_each_run_picks_its_own_fanned_out_steps() -> None:
    long_run = run_of("run-a", "csv", list(range(60, 90)))
    pattern = Pattern(2, 0.05, None, FanOut(share=0.5, k=2))
    first, second = build_plan([long_run], pattern, SHARED, 1, TEMPLATE_TOKENS)
    assert [step.calls for step in first.steps] != [step.calls for step in second.steps]


def test_a_repeated_run_gets_its_own_text() -> None:
    long_run = run_of("run-a", "csv", [60, 80])
    pattern = Pattern(2, 0.05, None, None)
    first, second = build_plan([long_run], pattern, SHARED, 1, TEMPLATE_TOKENS)
    assert (first.run_id, second.run_id) == ("run-a-1", "run-a-2")
    # csv runs share SHARED's csv prefix and no more, repeat or not.
    assert shared_length(first.steps[1].content, second.steps[1].content) == SHARED.by_type["csv"]


def test_turning_fan_out_on_leaves_the_arrivals_and_prompts_alone() -> None:
    without = plan_of()
    fanned = plan_of(Pattern(STEADY.runs, STEADY.rate_per_second, None, FanOut(1.0, 2)))
    assert [run.start_seconds for run in without] == [run.start_seconds for run in fanned]
    assert [run.run_id for run in without] == [run.run_id for run in fanned]
    assert [step.content for run in without for step in run.steps] == [
        step.content for run in fanned for step in run.steps
    ]


# Requests.


def test_headers_on_every_call_match_headers_v1() -> None:
    validator = Draft202012Validator(json.loads(HEADERS_SCHEMA.read_text()))
    plan = plan_of(Pattern(3, 0.05, None, FanOut(1.0, 2)))
    for run in plan:
        for step in run.steps:
            validator.validate(call_headers(run.run_id, step))


def test_headers_carry_the_run_step_and_expected_steps_and_the_group_only_when_fanned_out() -> None:
    run = plan_of(Pattern(1, 0.05, None, FanOut(1.0, 2)))[0]
    first, second = run.steps[0], run.steps[1]
    assert call_headers(run.run_id, first) == {
        "x-run-id": run.run_id,
        "x-step": "1",
        "x-expected-steps": "2",
    }
    assert call_headers(run.run_id, second)["x-parallel-group"] == "step-2"


def test_a_call_streams_with_usage_and_a_reply_of_the_traces_length() -> None:
    body = chat_request("thesis-adapter", "time year", 37)
    assert body["model"] == "thesis-adapter"
    assert body["messages"] == [{"role": "user", "content": "time year"}]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 37
    assert body["ignore_eos"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_tokenize_counts_the_same_message_with_the_same_template() -> None:
    assert tokenize_request("thesis-adapter", "time year") == {
        "model": "thesis-adapter",
        "messages": [{"role": "user", "content": "time year"}],
        "chat_template_kwargs": {"enable_thinking": False},
    }


@pytest.mark.parametrize(
    ("model", "served"),
    [
        ({"name": "Qwen/Qwen3-0.6B", "lora": {"name": "thesis-abc"}}, "thesis-abc"),
        ({"name": "Qwen/Qwen3-0.6B"}, "Qwen/Qwen3-0.6B"),
    ],
)
def test_the_model_is_the_one_the_agent_names(model: dict[str, Any], served: str) -> None:
    assert served_model(model) == served


# Traces.


def write_lines(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def line_of(run_id: str = "run-a", step: int = 1, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "task_type": "csv",
        "step": step,
        "expected_steps": 2,
        "gap_before_ms": 0,
        "prompt_tokens": 60,
        "completion_tokens": 5,
        **fields,
    }


def test_a_trace_is_read_into_runs_in_file_order(tmp_path: Path) -> None:
    trace = write_lines(
        tmp_path / "t.jsonl",
        [line_of("run-b"), line_of("run-b", 2, gap_before_ms=9), line_of("run-a")],
    )
    runs = load_trace(trace)
    assert [run.run_id for run in runs] == ["run-b", "run-a"]
    assert [call.step for call in runs[0].calls] == [1, 2]
    assert runs[0].calls[1].gap_before_ms == 9


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([line_of(schema_version=2)], "trace.v1"),
        ([line_of(step=2)], "steps"),
        ([line_of(), line_of()], "steps"),
        ([line_of(completion_tokens=0)], "completion_tokens"),
        ([line_of(parallel_group="step-1")], "parallel_group"),
    ],
)
def test_a_trace_the_replayer_cannot_reproduce_is_refused(
    tmp_path: Path, lines: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ReplayError, match=message):
        load_trace(write_lines(tmp_path / "t.jsonl", lines))


def test_an_empty_trace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="no calls"):
        load_trace(write_lines(tmp_path / "t.jsonl", []))


# Config.


def replay_section() -> dict[str, Any]:
    return {
        "seed": 3,
        "gap_tolerance_ms": 50,
        "shared_prefix_tokens": {"all_types": 8, "by_type": {"csv": 20}},
        "patterns": {
            "steady": {"runs": 20, "rate_per_second": 0.05},
            "burst": {
                "runs": 30,
                "rate_per_second": 0.02,
                "burst": {"runs": 5, "every_seconds": 120},
            },
            "steady-fan-out": {
                "runs": 5,
                "rate_per_second": 0.05,
                "fan_out": {"share": 0.5, "k": 2},
            },
        },
    }


def test_the_replay_section_is_read() -> None:
    config = load_replay_config(replay_section())
    assert config == ReplayConfig(
        seed=3,
        gap_tolerance_ms=50,
        shared_prefix=SharedPrefix(all_types=8, by_type={"csv": 20}),
        patterns={
            "steady": Pattern(20, 0.05, None, None),
            "burst": Pattern(30, 0.02, Burst(5, 120.0), None),
            "steady-fan-out": Pattern(5, 0.05, None, FanOut(0.5, 2)),
        },
    )


def test_a_type_sharing_less_than_all_types_is_refused() -> None:
    section = replay_section()
    section["shared_prefix_tokens"] = {"all_types": 30, "by_type": {"csv": 20}}
    with pytest.raises(ReplayError, match="csv"):
        load_replay_config(section)


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"runs": 0}, "runs"),
        ({"rate_per_second": 0}, "rate_per_second"),
        ({"rate_per_second": -0.05}, "rate_per_second"),
        ({"burst": {"runs": 0, "every_seconds": 120}}, "burst.runs"),
        ({"burst": {"runs": 5, "every_seconds": 0}}, "burst.every_seconds"),
        ({"fan_out": {"share": 0, "k": 2}}, "fan_out.share"),
        ({"fan_out": {"share": 1.5, "k": 2}}, "fan_out.share"),
        ({"fan_out": {"share": 0.5, "k": 1}}, "fan_out.k"),
        ({"runs": 2.5}, "runs"),
        ({"runs": True}, "runs"),
        ({"burst": {"runs": 1.5, "every_seconds": 120}}, "burst.runs"),
        ({"fan_out": {"share": 0.5, "k": 2.5}}, "fan_out.k"),
    ],
    ids=[
        "no runs",
        "no rate",
        "negative rate",
        "empty burst",
        "burst at no interval",
        "no share",
        "a share over 1",
        "one call is no fan-out",
        "half a run",
        "runs that are a yes",
        "half a burst run",
        "half a call",
    ],
)
def test_a_pattern_that_cannot_run_is_refused_by_name(change: dict[str, Any], named: str) -> None:
    section = replay_section()
    section["patterns"]["steady"].update(change)
    with pytest.raises(ReplayError, match=rf"steady.*{named}"):
        load_replay_config(section)


@pytest.mark.parametrize(
    ("shared", "named"),
    [
        ({"all_types": -1, "by_type": {"csv": 20}}, "all_types"),
        ({"all_types": 0.5, "by_type": {"csv": 20}}, "all_types"),
        ({"all_types": 0, "by_type": {"csv": 20.5}}, "csv"),
    ],
    ids=["negative all_types", "half a token across types", "half a token in a type"],
)
def test_a_shared_prefix_that_is_no_token_count_is_refused_by_name(
    shared: dict[str, Any], named: str
) -> None:
    section = replay_section()
    section["shared_prefix_tokens"] = shared
    with pytest.raises(ReplayError, match=rf"shared_prefix_tokens.*{named}"):
        load_replay_config(section)


def test_a_fan_out_of_every_later_step_is_allowed() -> None:
    section = replay_section()
    section["patterns"]["steady"]["fan_out"] = {"share": 1.0, "k": 2}
    fan_out = load_replay_config(section).patterns["steady"].fan_out
    assert fan_out == FanOut(1.0, 2)


def test_a_pattern_is_found_by_name_and_an_unknown_one_lists_the_others() -> None:
    config = load_replay_config(replay_section())
    assert named_pattern(config, "steady") == config.patterns["steady"]
    with pytest.raises(ReplayError, match=r"no-such.*burst.*steady"):
        named_pattern(config, "no-such")


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
def test_the_recorded_trace_plans_on_every_profile_and_pattern(profile: str) -> None:
    section = yaml.safe_load((REPO_ROOT / "config" / f"{profile}.yaml").read_text())["replay"]
    config = load_replay_config(section)
    assert set(config.patterns) >= {"steady", "burst", "steady-fan-out"}
    runs = load_trace(RECORDED_TRACE)
    for pattern in config.patterns.values():
        plan = build_plan(runs, pattern, config.shared_prefix, config.seed, TEMPLATE_TOKENS)
        assert len(plan) == pattern.runs


def test_the_done_whens_pattern_takes_20_of_the_recorded_runs() -> None:
    section = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())["replay"]
    config = load_replay_config(section)
    plan = build_plan(
        load_trace(RECORDED_TRACE),
        config.patterns["steady"],
        config.shared_prefix,
        config.seed,
        TEMPLATE_TOKENS,
    )
    assert len({trace_run_id(run.run_id) for run in plan}) == 20


def test_steady_fan_out_really_fans_out_on_the_recorded_trace() -> None:
    section = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())["replay"]
    config = load_replay_config(section)
    plan = build_plan(
        load_trace(RECORDED_TRACE),
        config.patterns["steady-fan-out"],
        config.shared_prefix,
        config.seed,
        TEMPLATE_TOKENS,
    )
    assert any(step.parallel_group is not None for run in plan for step in run.steps)


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
def test_runs_of_different_types_share_no_block_of_the_pickers_hash(profile: str) -> None:
    """The picker hashes 256-byte blocks of a request, tools first (llm-d-router v0.11.0): real runs
    of different types share none, so words shared across types would send every replayed run to
    one pod. A block holds at least 23 of the planned words, the longest being 10 letters."""
    section = yaml.safe_load((REPO_ROOT / "config" / f"{profile}.yaml").read_text())["replay"]
    config = load_replay_config(section)
    plan = build_plan(
        load_trace(RECORDED_TRACE),
        config.patterns["steady"],
        config.shared_prefix,
        config.seed,
        TEMPLATE_TOKENS,
    )
    first_of_type = {run.task_type: run.steps[0].content for run in plan}
    assert len(first_of_type) == 4
    for one, other in itertools.combinations(first_of_type.values(), 2):
        assert shared_length(one, other) < 23
