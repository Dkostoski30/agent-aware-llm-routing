"""make autoscale-compare (P4.5): the four autoscalers' numbers across real 1<->2 files, from
.meta.json files written by hand here, as make experiment writes them
(docs/task-cards/61-P4.5-real-autoscaling.md)."""

import csv
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from load_generator import autoscale_compare
from load_generator.autoscale_compare import (
    CSV_FIELDS,
    CompareError,
    Spread,
    VariantResult,
    check_same_replay,
    completion_holds,
    failures_hold,
    main,
    position_means,
    scale_up_holds,
    spread,
    variant_results,
    verdict_lines,
    write_csv,
)

ORDER = ("cpu", "pressure", "remaining", "queue")


def variant(
    autoscaler: str,
    started: float,
    scale_up: float | None = 100.0,
    scaled_before: bool = False,
    p50: int | None = 200_000,
    failed: int = 0,
) -> dict[str, Any]:
    """One variant of a .meta.json, as make experiment writes an autoscaled one."""
    finished = 26 - failed
    return {
        "started": started,
        "autoscale": {
            "autoscaler": autoscaler,
            "crossings": {
                "remaining_seconds": 60.0,
                "scale_up_seconds": scale_up,
                "queue_seconds": 90.0,
                "scaled_before_replay": scaled_before,
            },
            "scale_up_windows": {
                "context_seconds": 55.0,
                "scale_up_seconds": scale_up,
                "ready_seconds": None if scale_up is None else scale_up + 150.0,
                "restarts": 2,
            },
        },
        "autoscale_summary": {
            "runs": 26,
            "finished_runs": finished,
            "failed_runs": failed,
            "completion_p50_ms": p50,
            "makespan_ms": 600_000,
            "idle_pod_minutes": 0.5,
        },
    }


def meta(
    name: str = "autoscale-real-1to2-a",
    order: Sequence[str] = ORDER,
    changes: Mapping[str, Mapping[str, Any]] | None = None,
    **top: Any,
) -> dict[str, Any]:
    """A file's .meta.json: its variants named as their autoscalers, run in this order."""
    changes = changes or {}
    variants = {
        kind: variant(kind, started=1000.0 * position, **changes.get(kind, {}))
        for position, kind in enumerate(order)
    }
    return {
        "experiment": name,
        "hash": "abc123def456",
        "trace": "results/traces/convert-apb.jsonl",
        "pattern": "burst",
        "seed": 6,
        "profile_sha256": "f00d",
        "variants": variants,
        **top,
    }


def result(
    autoscaler: str,
    scale_up: float | None = 100.0,
    scaled_before: bool = False,
    p50: int | None = 200_000,
    failed: int = 0,
    experiment: str = "autoscale-real-1to2-a-abc123def456",
    position: int = 1,
) -> VariantResult:
    return VariantResult(
        experiment=experiment,
        position=position,
        variant=autoscaler,
        autoscaler=autoscaler,
        remaining_seconds=60.0,
        queue_seconds=90.0,
        context_seconds=55.0,
        scale_up_seconds=scale_up,
        scaled_before_replay=scaled_before,
        ready_seconds=None,
        runs=26,
        finished_runs=26 - failed,
        failed_runs=failed,
        completion_p50_ms=p50,
        makespan_ms=600_000,
        idle_pod_minutes=0.5,
        keda_restarts=2,
    )


def test_a_files_variants_are_read_in_the_order_they_ran() -> None:
    """The position is by each variant's start, not by the order the file lists them in."""
    shuffled = meta()
    shuffled["variants"] = dict(reversed(list(shuffled["variants"].items())))
    results = variant_results(shuffled)
    assert [(found.position, found.variant) for found in results] == [
        (1, "cpu"),
        (2, "pressure"),
        (3, "remaining"),
        (4, "queue"),
    ]


def test_a_variants_numbers_come_from_its_crossings_windows_and_summary() -> None:
    [found, *_] = variant_results(meta(changes={"cpu": {"scale_up": 35.0, "failed": 3}}))
    assert found == VariantResult(
        experiment="autoscale-real-1to2-a-abc123def456",
        position=1,
        variant="cpu",
        autoscaler="cpu",
        remaining_seconds=60.0,
        queue_seconds=90.0,
        context_seconds=55.0,
        scale_up_seconds=35.0,
        scaled_before_replay=False,
        ready_seconds=185.0,
        runs=26,
        finished_runs=23,
        failed_runs=3,
        completion_p50_ms=200_000,
        makespan_ms=600_000,
        idle_pod_minutes=0.5,
        keda_restarts=2,
    )


def test_a_variant_with_no_autoscaler_is_refused() -> None:
    fixed = meta()
    fixed["variants"]["cpu"]["autoscale"] = None
    with pytest.raises(CompareError, match="cpu"):
        variant_results(fixed)


@pytest.mark.parametrize("key", ["trace", "pattern", "seed", "profile_sha256"])
def test_files_of_differing_replays_are_refused(key: str) -> None:
    other = meta("autoscale-real-1to2-b")
    other[key] = "other"
    with pytest.raises(CompareError, match=key):
        check_same_replay([meta(), other])


def test_a_file_missing_a_variant_is_refused() -> None:
    with pytest.raises(CompareError, match="queue"):
        check_same_replay([meta(), meta("autoscale-real-1to2-b", order=ORDER[:3])])


def test_a_file_with_a_variant_the_first_lacks_is_refused() -> None:
    with pytest.raises(CompareError, match="queue"):
        check_same_replay([meta(order=ORDER[:3]), meta("autoscale-real-1to2-b")])


def test_files_of_one_replay_in_other_orders_pass() -> None:
    check_same_replay([meta(), meta("autoscale-real-1to2-c", order=tuple(reversed(ORDER)))])


def test_a_spread_is_the_mean_and_range_of_the_numbers_present() -> None:
    assert spread([3.0, None, 1.0, 5.0]) == Spread(mean=3.0, low=1.0, high=5.0, count=3)


def test_no_number_has_no_spread() -> None:
    assert spread([None, None]) is None


def rows(autoscaler: str, **per_file: Sequence[Any]) -> list[VariantResult]:
    """One result per file, each keyword a list with one value per file."""
    files = len(next(iter(per_file.values())))
    return [
        result(autoscaler, **{key: values[index] for key, values in per_file.items()})
        for index in range(files)
    ]


def test_the_scale_up_holds_when_the_candidate_rises_no_later_on_the_mean() -> None:
    assert scale_up_holds(
        rows("remaining", scale_up=[60.0, 100.0]), rows("queue", scale_up=[90.0, 70.0])
    )


def test_the_scale_up_fails_when_the_candidate_rises_later_on_the_mean() -> None:
    assert not scale_up_holds(
        rows("remaining", scale_up=[60.0, 101.0]), rows("queue", scale_up=[90.0, 70.0])
    )


def test_a_file_scaled_before_its_replay_is_left_out_not_counted_as_never_rising() -> None:
    assert scale_up_holds(
        rows("remaining", scale_up=[None, 80.0], scaled_before=[True, False]),
        rows("queue", scale_up=[90.0, 90.0]),
    )


def test_the_candidate_never_rising_in_more_files_fails_the_scale_up() -> None:
    """Never rising is later than any rise, though the mean of the rises it has is earlier."""
    assert not scale_up_holds(
        rows("remaining", scale_up=[None, 30.0]), rows("queue", scale_up=[90.0, 90.0])
    )


def test_only_the_reference_never_rising_leaves_the_scale_up_holding() -> None:
    assert scale_up_holds(rows("remaining", scale_up=[90.0]), rows("queue", scale_up=[None]))


def test_only_the_candidate_with_no_rise_time_at_all_fails_the_scale_up() -> None:
    assert not scale_up_holds(
        rows("remaining", scale_up=[None], scaled_before=[True]), rows("queue", scale_up=[90.0])
    )


def test_completion_holds_within_the_larger_of_the_two_spreads() -> None:
    """Means 200 s and 230 s, 30 s apart; the queue's own runs spread 30 s, so within."""
    assert completion_holds(
        rows("remaining", p50=[195_000, 205_000]), rows("queue", p50=[215_000, 245_000])
    )


def test_completion_fails_past_the_larger_of_the_two_spreads() -> None:
    assert not completion_holds(
        rows("remaining", p50=[195_000, 205_000]), rows("queue", p50=[215_001, 245_000])
    )


def test_completion_counts_the_candidates_spread_too() -> None:
    assert completion_holds(
        rows("remaining", p50=[170_000, 230_000]), rows("queue", p50=[255_000, 265_000])
    )


def test_completion_with_no_finished_run_on_one_side_fails() -> None:
    assert not completion_holds(rows("remaining", p50=[None]), rows("queue", p50=[200_000]))


def test_failures_hold_when_the_candidate_loses_no_more_runs_in_all() -> None:
    assert failures_hold(rows("remaining", failed=[3, 0]), rows("queue", failed=[1, 2]))


def test_failures_fail_when_the_candidate_loses_more_runs_in_all() -> None:
    assert not failures_hold(rows("remaining", failed=[3, 1]), rows("queue", failed=[1, 2]))


def four(changes: Mapping[str, Mapping[str, Any]] | None = None) -> list[VariantResult]:
    return [found for found in variant_results(meta(changes=changes))]


def test_the_verdict_is_the_same_ranking_when_all_three_lines_hold() -> None:
    lines = verdict_lines(four({"remaining": {"scale_up": 60.0}, "queue": {"scale_up": 90.0}}))
    assert lines[-1] == "verdict: same ranking as on the simulator"


def test_the_verdict_names_each_line_that_failed() -> None:
    lines = verdict_lines(
        four(
            {
                "remaining": {"scale_up": 120.0, "failed": 2},
                "queue": {"scale_up": 90.0, "failed": 1},
            }
        )
    )
    assert lines[-1] == "verdict: different (failed: scale-up, failures)"


def test_the_verdict_counts_the_files_scaled_before_their_replay() -> None:
    lines = verdict_lines(four({"remaining": {"scale_up": None, "scaled_before": True}}))
    assert any("remaining scaled before its replay in 1 file" in line for line in lines)


def test_a_comparison_without_both_verdict_variants_is_refused() -> None:
    with pytest.raises(CompareError, match="queue"):
        verdict_lines([result("remaining")])


def test_position_means_are_over_whichever_variant_ran_there() -> None:
    first = variant_results(meta(changes={"cpu": {"p50": 100_000, "failed": 2}}))
    second = variant_results(
        meta(
            "autoscale-real-1to2-c",
            order=("queue", "remaining", "pressure", "cpu"),
            changes={"queue": {"p50": 300_000}},
        )
    )
    means = position_means([*first, *second])
    assert means[1] == (Spread(mean=200_000, low=100_000, high=300_000, count=2), 1.0)
    assert means[4] == (Spread(mean=200_000, low=200_000, high=200_000, count=2), 0.0)


def test_the_csv_has_one_row_per_variant_per_file(tmp_path: Path) -> None:
    path = tmp_path / "compare.csv"
    write_csv(path, four())
    with path.open() as file:
        written = list(csv.DictReader(file))
    assert list(written[0]) == list(CSV_FIELDS)
    assert [row["variant"] for row in written] == list(ORDER)
    assert written[0]["ready_seconds"] == "250.0"


def write_metas(directory: Path) -> list[Path]:
    orders = {
        "a": ("cpu", "pressure", "remaining", "queue"),
        "c": ("queue", "remaining", "pressure", "cpu"),
    }
    paths = []
    for letter, order in orders.items():
        path = directory / f"autoscale-real-1to2-{letter}-abc123def456.meta.json"
        path.write_text(json.dumps(meta(f"autoscale-real-1to2-{letter}", order=order)))
        paths.append(path)
    return paths


def test_the_command_prints_the_verdict_and_writes_the_same_csv_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    metas = [str(path) for path in write_metas(tmp_path)]
    out = tmp_path / "compare.csv"
    main([*metas, "--out", str(out)])
    first = out.read_bytes()
    printed = capsys.readouterr().out
    assert "verdict: same ranking as on the simulator" in printed
    assert f"autoscale-compare: wrote {out}" in printed
    main([*metas, "--out", str(out)])
    assert out.read_bytes() == first


def test_the_module_runs_as_make_autoscale_compare_calls_it(tmp_path: Path) -> None:
    """python -m, as the Makefile runs it: the module itself calls main."""
    out = tmp_path / "compare.csv"
    metas = [str(path) for path in write_metas(tmp_path)]
    # loadgen/, which pytest puts on its own path but a child process does not inherit.
    package_root = Path(autoscale_compare.__file__).resolve().parents[1]
    finished = subprocess.run(
        [sys.executable, "-m", "load_generator.autoscale_compare", *metas, "--out", str(out)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(package_root)},
    )
    assert "verdict: same ranking as on the simulator" in finished.stdout
    assert out.exists()
