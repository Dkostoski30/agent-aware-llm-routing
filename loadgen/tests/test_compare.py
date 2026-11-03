"""The done-when's check of two replays of one trace (P2.3)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from load_generator.compare import differences, main, max_gap_deviation, plan_differences
from load_generator.plan import (
    FanOut,
    Pattern,
    PlannedRun,
    SharedPrefix,
    TraceCall,
    TraceRun,
    build_plan,
)

TOLERANCE = 50


def line_of(run: str = "run-a-1", step: int = 1, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run,
        "task_type": "csv",
        "step": step,
        "expected_steps": 2,
        "gap_before_ms": 0 if step == 1 else 10,
        "prompt_tokens": 60 + step,
        "completion_tokens": 5,
        "duration_ms": 1000,
        "ttft_ms": 100,
        **fields,
    }


def traced(run: str = "run-a", step: int = 1, **fields: Any) -> dict[str, Any]:
    line = line_of(run, step, **fields)
    del line["duration_ms"], line["ttft_ms"]
    return line


TRACE = [traced(step=1), traced(step=2)]
REPLAY = [line_of(step=1), line_of(step=2)]
# The same calls, measured by another run: two real replays are never equal line for line.
ANOTHER_RUN = [line_of(step=1, duration_ms=1100), line_of(step=2, duration_ms=900)]


def test_two_equal_replays_of_the_trace_have_no_differences() -> None:
    assert differences(TRACE, REPLAY, REPLAY, TOLERANCE) == []


def test_measurements_may_differ_between_replays() -> None:
    other = [line_of(step=1, duration_ms=7000, pod="p2"), line_of(step=2, cached_tokens=16)]
    assert differences(TRACE, REPLAY, other, TOLERANCE) == []


@pytest.mark.parametrize(
    "other",
    [
        [line_of(step=1), line_of(step=2, prompt_tokens=99)],
        [line_of(step=1), line_of(step=2, completion_tokens=99)],
        [line_of(step=1), line_of(step=2, parallel_group="step-2")],
        [line_of(run="run-a-2", step=1), line_of(run="run-a-2", step=2)],
        [line_of(step=1)],
    ],
    ids=["prompt tokens", "completion tokens", "group", "run id", "a line missing"],
)
def test_another_call_sequence_is_a_difference(other: list[dict[str, Any]]) -> None:
    found = differences(TRACE, REPLAY, other, TOLERANCE)
    assert any("sequence" in text for text in found)


def test_the_first_differing_call_is_named() -> None:
    other = [line_of(step=1), line_of(step=2, prompt_tokens=99)]
    found = differences(TRACE, REPLAY, other, TOLERANCE)
    assert any("line 2" in text for text in found)


@pytest.mark.parametrize("name", ["prompt_tokens", "completion_tokens"])
def test_tokens_off_the_trace_are_a_difference_even_when_both_replays_agree(name: str) -> None:
    off = [line_of(step=1), {**line_of(step=2), name: 99}]
    found = differences(TRACE, off, off, TOLERANCE)
    assert any(name in text and "run-a-1" in text for text in found)


def test_tokens_off_the_trace_in_the_first_replay_alone_are_a_difference() -> None:
    off = [line_of(step=1), line_of(step=2, prompt_tokens=99)]
    found = differences(TRACE, off, REPLAY, TOLERANCE)
    assert any("prompt_tokens" in text and "the trace has" in text for text in found)


@pytest.mark.parametrize(
    ("gap", "over"),
    [(10 + TOLERANCE, False), (10 - TOLERANCE + 40, False), (10 + TOLERANCE + 1, True)],
    ids=["at the tolerance", "under", "just over"],
)
def test_a_gap_may_differ_from_the_traces_by_the_tolerance(gap: int, over: bool) -> None:
    other = [line_of(step=1), line_of(step=2, gap_before_ms=gap)]
    found = differences(TRACE, REPLAY, other, TOLERANCE)
    assert any("gap" in text for text in found) == over


def test_a_gap_off_the_trace_in_the_first_replay_alone_is_a_difference() -> None:
    other = [line_of(step=1), line_of(step=2, gap_before_ms=10 + TOLERANCE + 1)]
    found = differences(TRACE, other, REPLAY, TOLERANCE)
    assert any("gap" in text for text in found)


def test_a_gap_shorter_than_the_traces_counts_like_a_longer_one() -> None:
    other = [line_of(step=1), line_of(step=2, gap_before_ms=4)]
    assert any("gap" in text for text in differences(TRACE, REPLAY, other, 5))
    assert not any("gap" in text for text in differences(TRACE, REPLAY, other, 6))
    assert max_gap_deviation(TRACE, other) == 6


def test_a_failed_call_is_a_difference() -> None:
    failed = line_of(step=2)
    del failed["duration_ms"]
    found = differences(TRACE, REPLAY, [line_of(step=1), failed], TOLERANCE)
    assert any("no reply" in text for text in found)


def test_a_reply_that_took_no_time_is_no_reply() -> None:
    found = differences(TRACE, REPLAY, [line_of(step=1), line_of(step=2, duration_ms=0)], TOLERANCE)
    assert any("no reply" in text for text in found)


def test_a_reply_without_a_ttft_is_a_difference() -> None:
    other = line_of(step=2)
    del other["ttft_ms"]
    found = differences(TRACE, REPLAY, [line_of(step=1), other], TOLERANCE)
    assert any("no TTFT" in text for text in found)


def test_a_failed_call_is_not_also_reported_for_its_ttft() -> None:
    failed = line_of(step=2)
    del failed["duration_ms"], failed["ttft_ms"]
    found = differences(TRACE, REPLAY, [line_of(step=1), failed], TOLERANCE)
    assert not any("TTFT" in text for text in found)


def test_another_task_type_or_expected_steps_between_replays_is_a_difference() -> None:
    for other in (line_of(step=2, task_type="support"), line_of(step=2, expected_steps=9)):
        found = differences(TRACE, REPLAY, [line_of(step=1), other], TOLERANCE)
        assert any("sequence" in text for text in found)


def test_a_line_that_is_not_in_the_trace_is_a_difference() -> None:
    stray = [*REPLAY, line_of(run="run-z-3", step=1)]
    found = differences(TRACE, stray, stray, TOLERANCE)
    assert any("run-z-3" in text for text in found)


def test_fanned_out_calls_each_compare_to_their_steps_trace_line() -> None:
    fanned = [
        line_of(step=1),
        line_of(step=2, parallel_group="step-2"),
        line_of(step=2, parallel_group="step-2"),
    ]
    assert differences(TRACE, fanned, fanned, TOLERANCE) == []


def test_the_largest_gap_deviation_is_reported() -> None:
    other = [line_of(step=1), line_of(step=2, gap_before_ms=33)]
    assert max_gap_deviation(TRACE, other) == 23


# Against the plan: two replays that lost the same calls agree, so only the plan can show it.


def plan_of(fan_out: FanOut | None = None) -> list[PlannedRun]:
    calls = [
        TraceCall(
            step=step,
            expected_steps=2,
            gap_before_ms=0 if step == 1 else 10,
            prompt_tokens=60 + step,
            completion_tokens=5,
        )
        for step in (1, 2)
    ]
    return build_plan(
        [TraceRun("run-a", "csv", calls)],
        Pattern(1, 0.05, None, fan_out),
        SharedPrefix(all_types=0, by_type={"csv": 20}),
        1,
        0,
    )


def test_a_replay_of_the_whole_plan_has_no_differences() -> None:
    assert plan_differences(plan_of(), REPLAY) == []


def test_each_fanned_out_call_is_expected_with_its_group() -> None:
    fanned = [
        line_of(step=1),
        line_of(step=2, parallel_group="step-2"),
        line_of(step=2, parallel_group="step-2"),
    ]
    assert plan_differences(plan_of(FanOut(1.0, 2)), fanned) == []
    assert plan_differences(plan_of(FanOut(1.0, 2)), REPLAY) != []


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [line_of(step=1)],
        [*REPLAY, line_of(step=2)],
        [line_of(step=2), line_of(step=1)],
        [line_of(step=1), line_of(step=2, task_type="support")],
        [line_of(step=1), line_of(step=2, expected_steps=9)],
        [line_of(step=1), line_of(step=2, prompt_tokens=99)],
        [line_of(step=1), line_of(step=2, completion_tokens=99)],
        [line_of(step=1), line_of(step=2, parallel_group="step-2")],
        [line_of(run="run-a-2", step=1), line_of(run="run-a-2", step=2)],
    ],
    ids=[
        "no calls",
        "a call missing",
        "a call too many",
        "another order",
        "another task type",
        "other expected steps",
        "other prompt tokens",
        "other completion tokens",
        "a group the plan does not have",
        "another run id",
    ],
)
def test_a_replay_that_is_not_the_plan_is_a_difference(lines: list[dict[str, Any]]) -> None:
    assert any("plan" in text for text in plan_differences(plan_of(), lines))


def write_lines(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


# main reads a profile of its own, so that its one run and its seed are known here.
PROFILE_SEED = 1


def use_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runs: int = 1) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    profile = {
        "replay": {
            "seed": PROFILE_SEED,
            "gap_tolerance_ms": TOLERANCE,
            "shared_prefix_tokens": {"all_types": 0, "by_type": {"csv": 20}},
            "patterns": {"one": {"runs": runs, "rate_per_second": 0.05}},
        }
    }
    (tmp_path / "config" / "tiny.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setattr("load_generator.compare.REPO_ROOT", tmp_path)


def argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    second: list[dict[str, Any]],
    first: list[dict[str, Any]] | None = None,
) -> list[str]:
    use_profile(monkeypatch, tmp_path)
    return [
        "compare",
        str(write_lines(tmp_path / "trace.jsonl", TRACE)),
        "one",
        str(write_lines(tmp_path / "a.jsonl", REPLAY if first is None else first)),
        str(write_lines(tmp_path / "b.jsonl", second)),
        "--profile",
        "tiny",
    ]


def test_main_prints_the_counts_and_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", argv(monkeypatch, tmp_path, ANOTHER_RUN))
    main()
    out = capsys.readouterr().out
    assert "same sequence, 2 calls" in out
    assert "max gap deviation" in out


def test_main_says_what_a_pass_does_not_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", argv(monkeypatch, tmp_path, ANOTHER_RUN))
    main()
    out = capsys.readouterr().out
    assert "as planned" not in out
    assert "Not checked: arrival times, prompt text, pods" in out


def test_main_refuses_one_replay_given_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", argv(monkeypatch, tmp_path, REPLAY))
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "the same replay" in out
    assert "same sequence" not in out


def test_main_refuses_one_file_named_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = argv(monkeypatch, tmp_path, ANOTHER_RUN)
    command[4] = command[3]
    monkeypatch.setattr("sys.argv", command)
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    assert "the same replay" in capsys.readouterr().out


def test_main_exits_1_and_lists_the_differences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = [line_of(step=1), line_of(step=2, prompt_tokens=99)]
    monkeypatch.setattr("sys.argv", argv(monkeypatch, tmp_path, other))
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    assert "prompt_tokens" in capsys.readouterr().out


def test_main_refuses_a_line_that_is_not_trace_v1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = [line_of(step=1), {**line_of(step=2), "unknown_field": 1}]
    monkeypatch.setattr("sys.argv", argv(monkeypatch, tmp_path, other))
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    assert "trace.v1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "lines",
    [[], [line_of(step=1)]],
    ids=["two empty files", "the same call lost in both"],
)
def test_main_fails_two_replays_that_agree_on_too_few_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    lines: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr("sys.argv", argv(monkeypatch, tmp_path, lines, first=lines))
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert f"first: {len(lines)} calls, the plan has 2" in out
    assert f"second: {len(lines)} calls, the plan has 2" in out
    assert "same sequence" not in out


def test_main_refuses_a_pattern_the_profile_does_not_have(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = argv(monkeypatch, tmp_path, REPLAY)
    command[2] = "no-such"
    monkeypatch.setattr("sys.argv", command)
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    assert "no-such" in capsys.readouterr().out


def two_run_trace() -> list[dict[str, Any]]:
    return [traced("run-a"), traced("run-b")]


def order_at(seed: int) -> list[str]:
    calls = [TraceCall(1, 2, 0, 61, 5)]
    runs = [TraceRun("run-a", "csv", calls), TraceRun("run-b", "csv", calls)]
    pattern = Pattern(2, 0.05, None, None)
    shared = SharedPrefix(all_types=0, by_type={"csv": 20})
    return [run.run_id for run in build_plan(runs, pattern, shared, seed, 0)]


def test_main_checks_against_the_plan_of_the_seed_it_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other_seed = next(seed for seed in range(2, 50) if order_at(seed) != order_at(PROFILE_SEED))
    lines = [line_of(run_id, step=1) for run_id in order_at(other_seed)]
    measured_again = [{**line, "duration_ms": 1100} for line in lines]
    use_profile(monkeypatch, tmp_path, runs=2)
    command = [
        "compare",
        str(write_lines(tmp_path / "trace.jsonl", two_run_trace())),
        "one",
        str(write_lines(tmp_path / "a.jsonl", lines)),
        str(write_lines(tmp_path / "b.jsonl", measured_again)),
        "--profile",
        "tiny",
    ]
    monkeypatch.setattr("sys.argv", [*command, "--seed", str(other_seed)])
    main()
    monkeypatch.setattr("sys.argv", command)
    with pytest.raises(SystemExit):
        main()
