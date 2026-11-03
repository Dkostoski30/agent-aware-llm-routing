"""An experiment's numbers from its replay lines (P2.5): rows, run times, hit rates, the KV split.

Times are rebuilt from a run's start, its gaps and its durations, which come from one monotonic
clock; ts_start is the wall clock, which moved seconds against it under WSL2 (P2.2a), so every line
here has a ts_start that would give other answers if anything read it.
"""

from pathlib import Path
from typing import Any

import pytest
from load_generator.results import (
    CallRow,
    call_rows,
    calls_per_pod,
    percentile,
    pod_switch_share,
    read_csv,
    run_completion_ms,
    summarise,
    ttft_stats,
    write_csv,
)

# Starts of runs 1, 2, 3 in seconds.
STARTS = [0.0, 10.0, 1000.0]
# Far above every test's tokens, except in the tests of the limit.
NO_LIMIT = 1_000_000


def line(
    run: str,
    step: int,
    *,
    gap: int = 0,
    duration: int | None = 1000,
    ttft: int | None = 100,
    pod: str | None = "pod-a",
    prompt: int = 100,
    completion: int = 10,
    cached: int | None = 0,
    task_type: str = "csv",
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "run_id": run,
        "task_type": task_type,
        "step": step,
        # Backwards on purpose: a result that read this would come out wrong.
        "ts_start": 9_000_000 - step,
        "gap_before_ms": gap,
        "expected_steps": 2,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }
    if duration is not None:
        fields["duration_ms"] = duration
    if ttft is not None:
        fields["ttft_ms"] = ttft
    if pod is not None:
        fields["pod"] = pod
    if cached is not None:
        fields["cached_tokens"] = cached
    return fields


def rows_of(lines: list[dict[str, Any]], capacity: int = NO_LIMIT, discard: int = 0) -> list[Any]:
    return call_rows("v", lines, STARTS, capacity, discard)


def row(**fields: Any) -> CallRow:
    defaults: dict[str, Any] = {
        "variant": "v",
        "run": "r-1",
        "task_type": "csv",
        "step": 1,
        "pod": "pod-a",
        "ttft_ms": 100,
        "duration_ms": 1000,
        "gap_before_ms": 0,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cached_tokens": 0,
        "moved": None,
        "tokens_on_pod": 110,
        "over_kv_limit": False,
    }
    return CallRow(**{**defaults, **fields})


def test_a_known_replay_gives_one_row_per_call_with_its_measurements() -> None:
    rows = rows_of(
        [
            line("r-1", 1, duration=2000, ttft=300, prompt=100, completion=10, cached=0),
            line("r-1", 2, gap=5, duration=1500, ttft=200, prompt=150, completion=20, cached=96),
        ]
    )
    assert rows == [
        row(
            run="r-1",
            step=1,
            ttft_ms=300,
            duration_ms=2000,
            moved=None,
            tokens_on_pod=110,
        ),
        row(
            run="r-1",
            step=2,
            ttft_ms=200,
            duration_ms=1500,
            gap_before_ms=5,
            prompt_tokens=150,
            completion_tokens=20,
            cached_tokens=96,
            moved=False,
            tokens_on_pod=170,
        ),
    ]


def test_rows_keep_the_order_of_the_lines() -> None:
    rows = rows_of([line("r-2", 1), line("r-1", 1), line("r-1", 2)])
    assert [(one.run, one.step) for one in rows] == [("r-2", 1), ("r-1", 1), ("r-1", 2)]


def test_a_run_id_with_hyphens_of_its_own_is_placed_by_its_last_part() -> None:
    """Converted traces have runs like bfcl-624: bfcl-624-2 is the plan's second run, at 10 s."""
    rows = rows_of(
        [
            line("bfcl-624-1", 1, duration=9000, prompt=500, completion=50),
            line("bfcl-624-2", 1),
        ],
        discard=1,
    )
    assert [(one.run, one.tokens_on_pod) for one in rows] == [("bfcl-624-2", 110)]


def test_the_first_runs_are_dropped() -> None:
    rows = rows_of([line("r-1", 1), line("r-2", 1), line("r-3", 1)], discard=2)
    assert [one.run for one in rows] == ["r-3"]


def test_the_runs_dropped_are_chosen_by_start_order_not_by_line_order() -> None:
    rows = rows_of([line("x-3", 1), line("x-1", 1)], discard=1)
    assert [one.run for one in rows] == ["x-3"]


def test_a_dropped_run_still_counts_as_load_on_a_call_it_overlaps() -> None:
    """A warm-up run's calls do fill the pod, so dropping its rows must not empty the pod."""
    rows = rows_of(
        [
            line("r-1", 1, duration=20_000, ttft=100, prompt=500, completion=50),
            line("r-2", 1, duration=1000, ttft=100, prompt=100, completion=10),
        ],
        discard=1,
    )
    assert [(one.run, one.tokens_on_pod) for one in rows] == [("r-2", 110 + 550)]


def test_another_pods_call_is_not_load_on_this_pod() -> None:
    rows = rows_of(
        [
            line("r-1", 1, duration=20_000, pod="pod-b", prompt=500, completion=50),
            line("r-2", 1, duration=1000, pod="pod-a"),
        ]
    )
    assert rows[1].tokens_on_pod == 110


def test_a_call_that_ended_before_this_one_started_is_not_load() -> None:
    # r-1 ends at 9 s, r-2 starts at 10 s.
    rows = rows_of(
        [line("r-1", 1, duration=9000, prompt=500, completion=50), line("r-2", 1, prompt=100)]
    )
    assert rows[1].tokens_on_pod == 110


def test_a_call_that_starts_after_this_ones_first_token_is_not_load_on_it() -> None:
    """The split is about what a call found until its first token: what comes later cannot have
    delayed it. r-1 has its first token at 300 ms, r-1's own later step does not overlap, and r-2
    starts at 10 s."""
    rows = rows_of(
        [
            line("r-1", 1, duration=20_000, ttft=300, prompt=100, completion=10),
            line("r-2", 1, duration=1000, prompt=500, completion=50),
        ]
    )
    assert rows[0].tokens_on_pod == 110
    assert rows[1].tokens_on_pod == 660


def test_a_later_step_starts_after_the_gap_and_duration_before_it() -> None:
    """r-1's step 2 starts at 1 s + 9 s gap = 10 s, as r-2's step 1 does, and both run 1 s."""
    rows = rows_of(
        [
            line("r-1", 1, duration=1000),
            line("r-1", 2, gap=9000, duration=1000, prompt=200, completion=20),
            line("r-2", 1, duration=1000),
        ]
    )
    assert [one.tokens_on_pod for one in rows] == [110, 330, 330]


def test_a_call_over_the_pods_capacity_is_over_and_one_exactly_at_it_is_not() -> None:
    at_capacity = rows_of([line("r-1", 1, prompt=100, completion=10)], capacity=110)
    over = rows_of([line("r-1", 1, prompt=100, completion=10)], capacity=109)
    assert at_capacity[0].over_kv_limit is False
    assert over[0].over_kv_limit is True


def test_a_later_step_on_another_pod_moved_and_one_on_the_same_pod_stayed() -> None:
    rows = rows_of(
        [line("r-1", 1, pod="pod-a"), line("r-1", 2, pod="pod-a"), line("r-1", 3, pod="pod-b")]
    )
    assert [one.moved for one in rows] == [None, False, True]


def test_a_fanned_out_step_stays_if_its_pod_served_any_call_of_the_step_before() -> None:
    rows = rows_of(
        [
            line("r-1", 1, pod="pod-a"),
            line("r-1", 2, pod="pod-a"),
            line("r-1", 2, pod="pod-b"),
            line("r-1", 3, pod="pod-b"),
        ]
    )
    assert [one.moved for one in rows] == [None, False, True, False]


def test_a_fanned_out_step_ends_with_its_last_reply() -> None:
    """Step 3 starts at the end of step 2's slower call, 5 s, so the short call of 1 s is over."""
    rows = rows_of(
        [
            line("r-1", 2, duration=1000),
            line("r-1", 2, duration=5000, pod="pod-b"),
            line("r-1", 3, ttft=100, duration=1000, pod="pod-b"),
        ]
    )
    assert rows[2].tokens_on_pod == 110


def test_a_failed_call_has_blank_measurements_and_no_load() -> None:
    rows = rows_of(
        [
            line("r-1", 1),
            line("r-1", 2, duration=None, ttft=None, pod=None, cached=None),
        ]
    )
    assert rows[1] == row(
        run="r-1",
        step=2,
        pod=None,
        ttft_ms=None,
        duration_ms=None,
        cached_tokens=None,
        moved=None,
        tokens_on_pod=None,
        over_kv_limit=None,
    )


def test_a_reply_without_a_first_token_counts_load_until_it_ended() -> None:
    rows = rows_of(
        [
            line("r-1", 1, ttft=None, duration=20_000),
            line("r-2", 1, duration=1000, prompt=500, completion=50),
        ]
    )
    assert rows[0].tokens_on_pod == 660


def test_the_completion_time_is_every_steps_gap_and_duration() -> None:
    rows = [
        row(step=1, gap_before_ms=0, duration_ms=1000),
        row(step=2, gap_before_ms=5, duration_ms=2000),
        row(step=3, gap_before_ms=7, duration_ms=500),
    ]
    assert run_completion_ms(rows) == 3512


def test_a_fanned_out_step_takes_as_long_as_its_slowest_call() -> None:
    rows = [
        row(step=1, duration_ms=1000),
        row(step=2, gap_before_ms=5, duration_ms=2000),
        row(step=2, gap_before_ms=6, duration_ms=9000),
    ]
    assert run_completion_ms(rows) == 1000 + 6 + 9000


def test_a_run_with_a_failed_call_has_no_completion_time() -> None:
    assert run_completion_ms([row(step=1), row(step=2, duration_ms=None)]) is None


@pytest.mark.parametrize(
    ("share", "expected"),
    [(0.5, 2), (0.51, 3), (0.95, 4), (1.0, 4), (0.01, 1)],
)
def test_a_percentile_is_the_nearest_rank(share: float, expected: int) -> None:
    assert percentile([4, 1, 3, 2], share) == expected


def test_the_summary_counts_runs_calls_and_failures_per_variant() -> None:
    summary = summarise(
        [
            row(variant="a", run="r-1", step=1, duration_ms=1000),
            row(variant="a", run="r-1", step=2, gap_before_ms=5, duration_ms=1000, moved=True),
            row(variant="a", run="r-2", step=1, duration_ms=500),
            row(variant="a", run="r-2", step=2, duration_ms=None, moved=None),
            row(variant="b", run="r-1", step=1, duration_ms=800),
            row(variant="b", run="r-1", step=2, duration_ms=800, moved=False),
        ]
    )
    assert list(summary) == ["a", "b"]
    assert summary["a"].runs == 2
    assert summary["a"].failed_runs == 1
    assert summary["a"].calls == 4
    assert summary["a"].failed_calls == 1
    assert summary["a"].later_steps == 1
    assert summary["a"].moved == 1
    assert summary["a"].completions_ms == [2005]
    assert summary["b"].failed_runs == 0
    assert summary["b"].moved == 0


def test_the_hit_rate_is_cached_over_prompt_tokens_of_calls_that_report_them() -> None:
    summary = summarise(
        [
            row(prompt_tokens=100, cached_tokens=50),
            row(step=2, prompt_tokens=300, cached_tokens=100),
            # A call with no count is unknown, not a miss.
            row(step=3, prompt_tokens=600, cached_tokens=None),
        ]
    )
    assert summary["v"].hit_rate == pytest.approx(150 / 400)


def test_a_variant_with_no_counts_has_no_hit_rate() -> None:
    assert summarise([row(cached_tokens=None)])["v"].hit_rate is None


def test_generation_speed_is_the_time_after_the_first_token_over_the_tokens_after_it() -> None:
    summary = summarise(
        [
            row(step=1, ttft_ms=500, duration_ms=1400, completion_tokens=10),
            row(step=2, ttft_ms=9000, duration_ms=11_000, completion_tokens=11),
            row(step=3, ttft_ms=100, duration_ms=3100, completion_tokens=11),
        ]
    )
    # 900 / 9, 2000 / 10, 3000 / 10: a long queue before the first token does not count.
    assert summary["v"].generation_ms_per_token == pytest.approx(200.0)


def test_generation_speed_leaves_out_short_replies_and_failed_calls() -> None:
    summary = summarise(
        [
            row(step=1, ttft_ms=100, duration_ms=9000, completion_tokens=4),
            row(step=2, ttft_ms=100, duration_ms=500, completion_tokens=5),
            row(step=3, ttft_ms=None, duration_ms=None, completion_tokens=50),
        ]
    )
    assert summary["v"].generation_ms_per_token == pytest.approx(100.0)


def test_with_no_reply_long_enough_there_is_no_generation_speed() -> None:
    assert summarise([row(completion_tokens=4)])["v"].generation_ms_per_token is None


def test_time_to_first_token_is_split_by_the_kv_limit() -> None:
    rows = [
        row(step=1, ttft_ms=100, over_kv_limit=False),
        row(step=2, ttft_ms=200, over_kv_limit=False),
        row(step=3, ttft_ms=9000, over_kv_limit=True),
        row(step=4, ttft_ms=None, over_kv_limit=None, duration_ms=None),
    ]
    under = ttft_stats(rows, over_limit=False)
    over = ttft_stats(rows, over_limit=True)
    assert (under.calls, under.p50_ms, under.p95_ms) == (2, 100, 200)
    assert (over.calls, over.p50_ms, over.p95_ms) == (1, 9000, 9000)


def test_time_to_first_token_can_be_split_by_moved_and_stayed_too() -> None:
    """P2.4's finding for P3.3: a capacity effect must not read as a cache effect."""
    rows = [
        row(step=2, ttft_ms=100, over_kv_limit=False, moved=False),
        row(step=3, ttft_ms=200, over_kv_limit=False, moved=True),
        row(step=4, ttft_ms=9000, over_kv_limit=True, moved=True),
    ]
    assert ttft_stats(rows, over_limit=False, moved=True).p50_ms == 200
    assert ttft_stats(rows, over_limit=False, moved=False).p50_ms == 100
    assert ttft_stats(rows, over_limit=True, moved=True).p50_ms == 9000
    assert ttft_stats(rows, over_limit=True, moved=False).calls == 0


def test_no_calls_in_a_split_have_no_time() -> None:
    stats = ttft_stats([], over_limit=False)
    assert (stats.calls, stats.p50_ms, stats.p95_ms) == (0, None, None)


def test_the_share_of_picks_that_switch_pod_follows_arrival_order() -> None:
    """Order of arrival at the gateway, not of the lines: r-2 starts between r-1's two steps."""
    lines = [
        line("r-1", 1, pod="pod-a", duration=5000),
        line("r-1", 2, gap=6000, pod="pod-a", duration=1000),
        line("r-2", 1, pod="pod-b", duration=1000),
    ]
    # By arrival: r-1 step 1 at 0 s on pod-a, r-2 at 10 s on pod-b, r-1 step 2 at 11 s on pod-a.
    assert pod_switch_share(lines, STARTS) == pytest.approx(1.0)


def test_a_failed_call_is_no_pick_of_a_known_pod() -> None:
    lines = [
        line("r-1", 1, pod="pod-a"),
        line("r-1", 2, duration=None, ttft=None, pod=None),
        line("r-2", 1, pod="pod-a"),
    ]
    assert pod_switch_share(lines, STARTS) == 0.0


def test_a_replay_of_one_call_has_no_switch_share() -> None:
    assert pod_switch_share([line("r-1", 1)], STARTS) is None


def test_calls_per_pod_counts_the_calls_each_pod_answered() -> None:
    lines = [
        line("r-1", 1, pod="pod-b"),
        line("r-2", 1, pod="pod-a"),
        line("r-3", 1, pod="pod-b"),
        line("r-3", 2, duration=None, ttft=None, pod=None),
    ]
    assert calls_per_pod(lines) == {"pod-a": 1, "pod-b": 2}


def test_a_csv_gives_back_the_rows_it_was_written_from(tmp_path: Path) -> None:
    rows = [
        row(moved=True, over_kv_limit=True, tokens_on_pod=5000),
        row(step=2, moved=False, over_kv_limit=False),
        row(
            step=3,
            pod=None,
            ttft_ms=None,
            duration_ms=None,
            cached_tokens=None,
            moved=None,
            tokens_on_pod=None,
            over_kv_limit=None,
        ),
    ]
    path = tmp_path / "out.csv"
    write_csv(path, rows)
    assert read_csv(path) == rows


def test_the_csv_has_the_columns_the_card_names(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    write_csv(path, [row()])
    header = path.read_text().splitlines()[0].split(",")
    assert {"variant", "run", "step", "pod", "ttft_ms", "duration_ms", "cached_tokens"} <= set(
        header
    )


def test_a_csv_is_never_written_over(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    write_csv(path, [row()])
    with pytest.raises(FileExistsError):
        write_csv(path, [row()])
