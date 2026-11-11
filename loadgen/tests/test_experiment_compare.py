"""make experiment-compare (P2.5): numbers across an experiment's CSVs, from the CSVs alone."""

from pathlib import Path

import pytest
from load_generator.experiment_compare import (
    HitRateCounts,
    hit_rate_counts,
    main,
    moved_runs,
    paired_differences,
    report_lines,
    run_differences_summary,
    run_hit_rates,
    without_largest,
)
from load_generator.results import (
    CallRow,
    read_csv,
    run_times_ms,
    slow_first_tokens,
    variant_totals,
    write_csv,
)


def row(variant: str, run: str, step: int = 1, **fields: object) -> CallRow:
    values: dict[str, object] = {
        "variant": variant,
        "run": run,
        "task_type": "csv",
        "step": step,
        "pod": "pod-a",
        "ttft_ms": 500,
        "duration_ms": 2000,
        "gap_before_ms": 0,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cached_tokens": 40,
        "moved": None,
        "tokens_on_pod": 110,
        "over_kv_limit": False,
        **fields,
    }
    return CallRow(**values)  # type: ignore[arg-type]


def a_failed_call(variant: str, run: str, step: int = 1) -> CallRow:
    return row(
        variant,
        run,
        step,
        pod=None,
        ttft_ms=None,
        duration_ms=None,
        cached_tokens=None,
        tokens_on_pod=None,
        over_kv_limit=None,
    )


def test_totals_add_up_each_measure_and_split_by_the_kv_limit() -> None:
    totals = variant_totals(
        [
            row("a", "r-1", step=1, ttft_ms=500, duration_ms=2000, cached_tokens=40),
            row(
                "a",
                "r-1",
                step=2,
                gap_before_ms=7,
                ttft_ms=9000,
                duration_ms=10000,
                cached_tokens=10,
                over_kv_limit=True,
            ),
        ]
    )
    assert totals.run_ms == 12007
    assert totals.ttft_ms == 9500
    assert totals.generation_ms == 2500
    assert totals.uncached_tokens == 60 + 90
    assert totals.under_limit_calls == 1
    assert totals.under_limit_ttft_ms == 500
    assert totals.under_limit_uncached_tokens == 60
    assert totals.over_limit_calls == 1
    assert totals.over_limit_ttft_ms == 9000


def test_a_failed_call_adds_no_time_or_tokens_and_its_run_no_run_time() -> None:
    totals = variant_totals(
        [
            row("a", "r-1"),
            a_failed_call("a", "r-2"),
            row("a", "r-3", ttft_ms=100, duration_ms=1000),
            a_failed_call("a", "r-3", step=2),
        ]
    )
    assert totals.run_ms == 2000
    assert totals.ttft_ms == 600
    assert totals.generation_ms == 2400
    assert totals.uncached_tokens == 120


def test_a_call_with_no_cached_count_still_counts_its_times() -> None:
    """A replay may lack the count (replay.py), and that is unknown tokens, not an absent call."""
    totals = variant_totals(
        [row("a", "r-1", cached_tokens=None, ttft_ms=9000, duration_ms=10000), row("a", "r-2")]
    )
    assert totals.ttft_ms == 9500
    assert totals.generation_ms == 2500
    assert totals.under_limit_calls == 2
    assert totals.under_limit_ttft_ms == 9500
    assert totals.uncached_tokens == 60
    assert totals.under_limit_uncached_tokens == 60
    assert totals.calls_without_cached_count == 1


def test_a_call_on_no_known_pod_is_in_neither_band_as_in_the_ttft_stats() -> None:
    totals = variant_totals([row("a", "r-1", pod=None, tokens_on_pod=None, over_kv_limit=None)])
    assert totals.ttft_ms == 500
    assert totals.under_limit_calls == 0
    assert totals.over_limit_calls == 0


def test_totals_count_the_runs_their_time_covers() -> None:
    totals = variant_totals([row("a", "r-1"), a_failed_call("a", "r-2")])
    assert totals.finished_runs == 1
    assert totals.runs == 2


def test_run_times_are_each_runs_and_none_for_a_failed_one() -> None:
    rows = [
        row("a", "r-1", step=1, duration_ms=2000),
        row("a", "r-1", step=2, gap_before_ms=5, duration_ms=1000),
        a_failed_call("a", "r-2"),
    ]
    assert run_times_ms(rows) == {"r-1": 3005, "r-2": None}


def test_slow_first_tokens_are_those_at_or_over_the_threshold_slowest_first() -> None:
    rows = [
        row("a", "r-1", ttft_ms=4999),
        row("a", "r-2", ttft_ms=5000),
        row("a", "r-3", ttft_ms=9000),
        a_failed_call("a", "r-4"),
    ]
    assert [call.run for call in slow_first_tokens(rows, 5000)] == ["r-3", "r-2"]


def test_differences_leave_out_a_run_that_failed_on_either_side() -> None:
    assert paired_differences(
        {"r-1": 100, "r-2": None, "r-3": 400, "r-4": 50},
        {"r-1": 300, "r-2": 200, "r-3": None, "r-4": 20},
    ) == {"r-1": -200, "r-4": 30}


def test_the_summary_counts_each_side_and_gives_the_median() -> None:
    summary = run_differences_summary({"r-1": -200, "r-2": 30, "r-3": -10, "r-4": 0})
    assert summary.first_faster == 2
    assert summary.second_faster == 1
    assert summary.median_ms == -5
    assert summary.total_ms == -180


def test_the_summary_of_no_runs_has_no_median() -> None:
    summary = run_differences_summary({})
    assert summary.median_ms is None
    assert summary.total_ms == 0


def test_leaving_out_the_largest_goes_with_the_totals_direction() -> None:
    first = {"r-1": 100, "r-2": 200, "r-3": 300, "r-4": 400}
    second = {"r-1": 600, "r-2": 300, "r-3": 200, "r-4": 450}
    assert without_largest(first, second, 1) == (900, 950)
    assert without_largest(second, first, 1) == (950, 900)


def test_each_runs_hit_rate_is_over_its_calls_with_a_count() -> None:
    rates = run_hit_rates(
        [
            row("a", "r-1", prompt_tokens=100, cached_tokens=40),
            row("a", "r-1", step=2, prompt_tokens=300, cached_tokens=260),
            row("a", "r-1", step=3, prompt_tokens=500, cached_tokens=None),
            row("a", "r-2", cached_tokens=None),
        ]
    )
    assert rates == {"r-1": 0.75, "r-2": None}


def test_a_run_counts_as_moved_by_any_later_step_that_moved() -> None:
    rows = [
        row("a", "r-1", moved=None),
        row("a", "r-1", step=2, moved=False),
        row("a", "r-1", step=3, moved=True),
        row("a", "r-2", moved=None),
        row("a", "r-2", step=2, moved=False),
    ]
    assert moved_runs(rows) == {"r-1"}


def test_hit_rates_are_paired_over_the_named_runs_with_a_count_on_both_sides() -> None:
    first = {"r-1": 0.9, "r-2": 0.5, "r-3": 0.7, "r-4": 0.8, "r-5": None}
    second = {"r-1": 0.6, "r-2": 0.8, "r-3": 0.7, "r-4": 0.1, "r-5": 0.4}
    assert hit_rate_counts(first, second, ["r-1", "r-2", "r-3", "r-5"]) == HitRateCounts(
        first_higher=1, second_higher=1, same=1, without_count=1
    )


def test_hit_rates_differing_at_all_are_not_the_same() -> None:
    """No tolerance: a run nothing changed for gives an equal count, so equal rates."""
    counts = hit_rate_counts({"r-1": 0.8836}, {"r-1": 0.8834}, ["r-1"])
    assert counts == HitRateCounts(first_higher=1, second_higher=0, same=0, without_count=0)


def write_chain(path: Path, order: tuple[str, str], slow: int) -> Path:
    write_csv(
        path,
        [
            row(variant, run, duration_ms=2000 + (slow if variant == "round-robin" else 0))
            for variant in order
            for run in ("r-1", "r-2", "r-3")
        ],
    )
    return path


def test_the_report_gives_each_chain_and_their_average(tmp_path: Path) -> None:
    one = write_chain(tmp_path / "one.csv", ("round-robin", "default"), slow=1000)
    other = write_chain(tmp_path / "other.csv", ("default", "round-robin"), slow=3000)
    report = "\n".join(report_lines([one, other], stall_ms=5000))
    assert "one.csv, in order round-robin, default" in report
    assert "other.csv, in order default, round-robin" in report
    assert "default faster in 3 of 3 runs, round-robin in 0" in report
    # Round-robin's runs average 4 s and default's 2 s, over 3 runs.
    assert "over 2 files: default 6.0 s, round-robin 12.0 s" in report
    assert "default minus round-robin -6.0 s, -50.0% of round-robin" in report


def test_the_report_says_how_far_one_variant_moves_between_files(tmp_path: Path) -> None:
    one = write_chain(tmp_path / "one.csv", ("round-robin", "default"), slow=1000)
    other = write_chain(tmp_path / "other.csv", ("default", "round-robin"), slow=3000)
    report = "\n".join(report_lines([one, other], stall_ms=5000))
    between = "between other.csv and one.csv"
    assert f"round-robin {between}: total 6.0 s, median size per run 2.00 s" in report
    assert f"default {between}: total 0.0 s, median size per run 0.00 s" in report


def test_the_report_lists_the_slow_calls_with_their_pods(tmp_path: Path) -> None:
    csv = tmp_path / "one.csv"
    write_csv(
        csv,
        [
            row("default", "r-1"),
            row("round-robin", "r-1", ttft_ms=7000, duration_ms=9000, pod="pod-b", step=3),
        ],
    )
    report = "\n".join(report_lines([csv], stall_ms=5000))
    assert "round-robin: 1 calls of 5.0 s or more to first token, 7.0 s in all" in report
    assert "r-1 step 3 on pod-b: 7.0 s" in report


def test_the_report_gives_the_share_left_without_the_runs_that_carry_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-robin is slower by 4 s in r-1 and r-2, 2 s in r-3 to r-5, and level in r-6 to r-8."""
    monkeypatch.setattr("load_generator.experiment_compare.RUNS_LEFT_OUT", (2, 5))
    extra = {"r-1": 4000, "r-2": 4000, "r-3": 2000, "r-4": 2000, "r-5": 2000}
    runs = [f"r-{number}" for number in range(1, 9)]
    csv = tmp_path / "one.csv"
    write_csv(
        csv,
        [row("default", run, duration_ms=1000) for run in runs]
        + [row("round-robin", run, duration_ms=1000 + extra.get(run, 0)) for run in runs],
    )
    report = "\n".join(report_lines([csv], stall_ms=50000))
    # Default 6 s against round-robin 12 s of the 6 runs left, then 3 s against 3 s of the 3 left.
    assert "without the 2 runs that favour that side most: -50.0% of round-robin" in report
    assert "without the 5 runs that favour that side most: 0.0% of round-robin" in report


def test_each_files_totals_say_how_many_runs_they_cover(tmp_path: Path) -> None:
    """Otherwise a variant whose slow run failed reads as the fast one."""
    csv = tmp_path / "one.csv"
    write_csv(
        csv,
        [
            row("a", "r-1", duration_ms=100000),
            row("a", "r-2"),
            row("b", "r-1"),
            a_failed_call("b", "r-2"),
        ],
    )
    report = "\n".join(report_lines([csv], stall_ms=500000))
    assert "a: runs 102.0 s over 2 of 2 finished" in report
    assert "b: runs 2.0 s over 1 of 2 finished" in report


def test_calls_with_no_cached_count_are_said(tmp_path: Path) -> None:
    csv = tmp_path / "one.csv"
    write_csv(csv, [row("a", "r-1", cached_tokens=None), row("b", "r-1")])
    report = "\n".join(report_lines([csv], stall_ms=5000))
    assert "uncached tokens 0, and 1 calls with no count" in report


def hit_rate_rows(variant: str, cached: dict[str, int], moved: set[str]) -> list[CallRow]:
    """Each run as two steps of 100 prompt tokens, the first with nothing cached."""
    return [
        call
        for run, tokens in cached.items()
        for call in (
            row(variant, run, cached_tokens=0),
            row(variant, run, step=2, cached_tokens=tokens, moved=run in moved),
        )
    ]


def test_the_report_pairs_each_runs_hit_rate_and_again_over_the_runs_each_side_moved(
    tmp_path: Path,
) -> None:
    """b moves r-1 and r-2; a is higher on r-1 and r-3, b on r-2, and r-4 is the same."""
    csv = tmp_path / "one.csv"
    write_csv(
        csv,
        hit_rate_rows("a", {"r-1": 90, "r-2": 50, "r-3": 80, "r-4": 70}, moved=set())
        + hit_rate_rows("b", {"r-1": 20, "r-2": 60, "r-3": 60, "r-4": 70}, moved={"r-1", "r-2"}),
    )
    report = report_lines([csv], stall_ms=5000)
    assert "  hit rate per run: a higher in 2 of 4 runs, b in 1, the same in 1" in report
    assert "    of the 0 runs a moved a step in: a higher in 0, b in 0, the same in 0" in report
    assert "    of the 2 runs b moved a step in: a higher in 1, b in 1, the same in 0" in report


def test_the_report_says_the_runs_whose_hit_rate_it_could_not_pair(tmp_path: Path) -> None:
    csv = tmp_path / "one.csv"
    write_csv(
        csv,
        [
            *hit_rate_rows("a", {"r-1": 90}, moved=set()),
            row("a", "r-2", cached_tokens=None),
            *hit_rate_rows("b", {"r-1": 20, "r-2": 60}, moved=set()),
        ],
    )
    report = report_lines([csv], stall_ms=5000)
    assert (
        "  hit rate per run: a higher in 1 of 1 runs, b in 0, the same in 0; "
        "1 runs with no count on a side left out"
    ) in report


def test_a_variant_with_no_finished_run_still_gets_a_report(tmp_path: Path) -> None:
    """An overload experiment may end this way, and that is a result."""
    one = tmp_path / "one.csv"
    other = tmp_path / "other.csv"
    for path in (one, other):
        write_csv(path, [row("a", "r-1"), a_failed_call("b", "r-1")])
    report = "\n".join(report_lines([one, other], stall_ms=5000))
    assert "a faster in 0 of 0 runs, b in 0; a minus b per run: median -" in report
    assert "over 2 files: no run finished in every file on both sides" in report
    assert "b between other.csv and one.csv: no run finished in both" in report


def test_files_of_one_name_are_told_apart_by_their_paths(tmp_path: Path) -> None:
    """The first run and its rerun have one name, since the name holds the file's hash."""
    (tmp_path / "before").mkdir()
    (tmp_path / "after").mkdir()
    one = write_chain(tmp_path / "before" / "one.csv", ("round-robin", "default"), slow=1000)
    other = write_chain(tmp_path / "after" / "one.csv", ("round-robin", "default"), slow=1000)
    report = "\n".join(report_lines([one, other], stall_ms=5000))
    assert f"{one}, in order round-robin, default" in report
    assert f"default between {other} and {one}" in report


def test_a_file_named_twice_is_refused(tmp_path: Path) -> None:
    """It would count as a second run that happens to agree exactly."""
    csv = write_chain(tmp_path / "one.csv", ("round-robin", "default"), slow=1000)
    with pytest.raises(ValueError, match="twice"):
        report_lines([csv, csv], stall_ms=5000)


def test_other_variants_are_refused(tmp_path: Path) -> None:
    one = tmp_path / "one.csv"
    other = tmp_path / "other.csv"
    write_csv(one, [row("a", "r-1"), row("b", "r-1")])
    write_csv(other, [row("a", "r-1"), row("c", "r-1")])
    with pytest.raises(ValueError, match="other variants"):
        report_lines([one, other], stall_ms=5000)


def test_other_than_two_variants_are_refused(tmp_path: Path) -> None:
    csv = tmp_path / "one.csv"
    write_csv(csv, [row("a", "r-1")])
    with pytest.raises(ValueError, match="2 variants"):
        report_lines([csv], stall_ms=5000)


def test_other_runs_are_refused_since_the_files_would_not_be_one_load(tmp_path: Path) -> None:
    one = tmp_path / "one.csv"
    other = tmp_path / "other.csv"
    write_csv(one, [row("a", "r-1"), row("b", "r-1")])
    write_csv(other, [row("a", "r-2"), row("b", "r-2")])
    with pytest.raises(ValueError, match="other runs"):
        report_lines([one, other], stall_ms=5000)


def test_runs_that_failed_are_said_not_hidden(tmp_path: Path) -> None:
    csv = tmp_path / "one.csv"
    write_csv(
        csv,
        [
            row("a", "r-1"),
            row("b", "r-1"),
            row("a", "r-2"),
            a_failed_call("b", "r-2"),
        ],
    )
    assert any("left out 1 runs" in line for line in report_lines([csv], stall_ms=5000))


def test_main_prints_the_report_of_the_named_csvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = write_chain(tmp_path / "one.csv", ("round-robin", "default"), slow=1000)
    monkeypatch.setattr("sys.argv", ["compare", "--stall-seconds", "5", str(csv)])
    main()
    assert "one.csv, in order round-robin, default" in capsys.readouterr().out


def test_the_csv_round_trip_keeps_what_the_report_reads(tmp_path: Path) -> None:
    csv = tmp_path / "one.csv"
    rows = [row("a", "r-1", over_kv_limit=True, ttft_ms=9000, duration_ms=9500)]
    write_csv(csv, rows)
    assert variant_totals(read_csv(csv)) == variant_totals(rows)
